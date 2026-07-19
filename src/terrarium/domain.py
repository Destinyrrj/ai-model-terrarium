"""Strict, JSON-safe domain contracts for the Terrarium world engine.

This module deliberately contains no model-provider or subprocess concepts.  It is
the trust boundary between untrusted agent output and deterministic world code:
``Action`` is the only accepted intent schema and it has no free-text or command
field.  All models reject unknown fields and coercive input.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal

import numpy as np
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

AgentId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
LocationId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=96,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_./:-]*$",
    ),
]
LegacyId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=96,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
ActionKind = Literal["noop", "move", "forage", "eat", "dig"]
ResourceKind = Literal["red_berry", "root"]
WeatherKind = Literal["clear", "rain"]
DeathCause = Literal["poison", "collapsed_tunnel", "starvation", "old_age"]
VisibleDeathCause = Literal["sudden_illness", "collapsed_tunnel", "wasting", "natural_death"]


class StrictModel(BaseModel):
    """Base contract: deeply frozen containers, strict values, no unknown fields."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        validate_default=True,
    )

    def model_post_init(self, __context: Any) -> None:
        """Freeze containers after validation so a checkpoint cannot drift in place.

        Pydantic's ``frozen=True`` protects model attributes but deliberately does not
        freeze nested ``dict``/``list`` values.  Mechanical state is hash-addressed,
        so allowing either container to mutate after validation would invalidate an
        already-checked checkpoint.  ``FrozenDict`` remains JSON-serializable because
        it is a ``dict`` subclass; sequences are declared as tuples below.
        """

        for field_name in type(self).model_fields:
            value = object.__getattribute__(self, field_name)
            frozen_value = _deep_freeze(value)
            if frozen_value is not value:
                object.__setattr__(self, field_name, frozen_value)


class FrozenDict(dict[Any, Any]):
    """A JSON-serializable mapping which rejects every in-place mutation API."""

    @staticmethod
    def _immutable(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("validated domain mappings are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable

    def __ior__(self, _other: object) -> FrozenDict:
        self._immutable()
        return self


def _freeze_mapping(value: Mapping[Any, Any]) -> FrozenDict:
    return FrozenDict({key: _deep_freeze(item) for key, item in value.items()})


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


class Action(StrictModel):
    """One closed-form game intent.

    Exact wire variants are::

        {"agent_id": "A", "type": "noop"}
        {"agent_id": "A", "type": "move", "destination": "valley_a/cave"}
        {"agent_id": "A", "type": "forage", "resource": "red_berry"}
        {"agent_id": "A", "type": "eat", "item": "red_berry"}
        {"agent_id": "A", "type": "dig", "depth": 4}

    A single model is used instead of a permissive command envelope so adapters can
    call ``Action.model_validate(payload)`` directly.  The post-validator makes the
    five variants mutually exclusive.  Irrelevant fields may only have JSON ``null``
    values, which preserves ordinary Pydantic dump/validate round trips.  There is
    intentionally nowhere to put prose, shell commands, paths, or tool arguments.
    """

    agent_id: AgentId
    type: ActionKind
    destination: LocationId | None = None
    resource: ResourceKind | None = None
    item: ResourceKind | None = None
    depth: Annotated[int, Field(strict=True, ge=1, le=10)] | None = None

    @model_validator(mode="after")
    def _validate_variant(self) -> Action:
        variant_field: dict[str, str | None] = {
            "noop": None,
            "move": "destination",
            "forage": "resource",
            "eat": "item",
            "dig": "depth",
        }
        required = variant_field[self.type]
        optional_fields = {"destination", "resource", "item", "depth"}
        supplied = {
            field_name for field_name in optional_fields if getattr(self, field_name) is not None
        }
        expected = set() if required is None else {required}
        if supplied != expected:
            raise ValueError(
                f"action type {self.type!r} requires exactly fields {sorted(expected)!r}"
            )
        if required is not None and getattr(self, required) is None:
            raise ValueError(f"action field {required!r} cannot be null")
        return self

    def public_payload(self) -> dict[str, str | int]:
        """Return the dry, agent-visible portion of this action."""

        dumped = self.model_dump(mode="json", exclude_none=True)
        return {key: value for key, value in dumped.items() if key != "type"}


class Location(StrictModel):
    """A graph node.  WorldState validates references and symmetric edges."""

    id: LocationId
    neighbors: tuple[LocationId, ...] = Field(default_factory=tuple, max_length=16)

    @field_validator("neighbors", mode="before")
    @classmethod
    def _unique_neighbors(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("location neighbors must be an array")
        value = tuple(value)
        if len(value) != len(set(value)):
            raise ValueError("location neighbors must be unique")
        return value


class WeatherState(StrictModel):
    """Current weather plus the last at most three tick values, current included."""

    current: WeatherKind = "clear"
    recent: tuple[WeatherKind, ...] = Field(
        default_factory=lambda: ("clear",), min_length=1, max_length=3
    )

    @field_validator("recent", mode="before")
    @classmethod
    def _freeze_recent(cls, value: object) -> tuple[object, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("weather.recent must be an array")
        return tuple(value)

    @model_validator(mode="after")
    def _current_is_latest(self) -> WeatherState:
        if self.recent[-1] != self.current:
            raise ValueError("weather.current must be the last entry in weather.recent")
        return self

    def rained_last(self, ticks: int) -> bool:
        """Whether rain occurred in the bounded, most-recent ``ticks`` entries."""

        if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 1:
            raise ValueError("ticks must be a positive integer")
        return "rain" in self.recent[-ticks:]

    def advance(self, current: WeatherKind | str) -> WeatherState:
        """Return the next immutable weather window with an explicit weather value."""

        # Validation is intentional here: callers cannot sneak arbitrary strings into
        # state through Pydantic's non-validating ``model_copy(update=...)`` API.
        data = {"current": current, "recent": [*self.recent, current][-3:]}
        return WeatherState.model_validate(data)


class PCG64CoreState(StrictModel):
    """The 128-bit internal state of NumPy's PCG64 bit generator."""

    state: Annotated[int, Field(strict=True, ge=0)]
    inc: Annotated[int, Field(strict=True, ge=0)]


class PCG64State(StrictModel):
    """JSON representation of ``np.random.Generator(PCG64).bit_generator.state``.

    WorldState stores this value rather than a live generator, making checkpoints
    JSON serializable.  ``to_generator`` is the only supported way world code obtains
    randomness; every draw must be followed by checkpointing the returned state.
    """

    bit_generator: Literal["PCG64"] = "PCG64"
    state: PCG64CoreState
    has_uint32: Literal[0, 1]
    uinteger: Annotated[int, Field(strict=True, ge=0, le=4_294_967_295)]

    @classmethod
    def from_generator(cls, generator: np.random.Generator) -> PCG64State:
        """Capture a PCG64 generator, rejecting other bit generators."""

        if not isinstance(generator, np.random.Generator):
            raise TypeError("generator must be numpy.random.Generator")
        if not isinstance(generator.bit_generator, np.random.PCG64):
            raise TypeError("Terrarium requires the PCG64 bit generator")
        return cls.model_validate(generator.bit_generator.state)

    def to_generator(self) -> np.random.Generator:
        """Restore an independent PCG64 generator at exactly this checkpoint."""

        generator = np.random.Generator(np.random.PCG64())
        generator.bit_generator.state = self.model_dump(mode="python")
        return generator


class FalseDecoyState(StrictModel):
    """Tracked birth and emissions of a harmless environmental cue.

    The internal name is never included in observations.  Emitting the cue changes
    only its own tracking fields and the structured event log: it consumes no RNG and
    has no health, hunger, inventory, resource, or movement effect.  One of at most two
    emissions can be deliberately colocated with an already-resolved death, creating
    correlation without a causal mechanic.
    """

    decoy_id: Annotated[
        str,
        StringConstraints(
            strict=True,
            min_length=1,
            max_length=48,
            pattern=r"^[a-z][a-z0-9_]*$",
        ),
    ] = "raven_death_omen"
    location: LocationId = "valley_a/grove"
    birth_tick: Annotated[int, Field(strict=True, ge=0)] = 1
    emitted_ticks: tuple[Annotated[int, Field(strict=True, ge=0)], ...] = Field(
        default_factory=tuple
    )

    @field_validator("emitted_ticks", mode="before")
    @classmethod
    def _emissions_are_unique(cls, value: object) -> tuple[int, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("emitted_ticks must be an array")
        value = tuple(value)
        if value != tuple(sorted(set(value))):
            raise ValueError("emitted_ticks must be sorted and unique")
        return value


class AgentState(StrictModel):
    """Mechanical agent state only; no prompt, memory, or model-generated text."""

    agent_id: AgentId
    loc: LocationId
    hp: Annotated[int, Field(strict=True, ge=0, le=100)] = 100
    hunger: Annotated[int, Field(strict=True, ge=0, le=100)] = 0
    age: Annotated[int, Field(strict=True, ge=0)] = 0
    max_age: Annotated[int, Field(strict=True, ge=1)] = 100
    generation: Annotated[int, Field(strict=True, ge=0)] = 0
    birth_tick: Annotated[int, Field(strict=True, ge=0)] = 0
    inherited_legacy_ids: tuple[LegacyId, ...] = Field(default_factory=tuple, max_length=32)
    inventory: dict[ResourceKind, Annotated[int, Field(strict=True, ge=0)]] = Field(
        default_factory=dict
    )
    alive: bool = True
    death_cause: DeathCause | None = None

    @field_validator("inherited_legacy_ids", mode="before")
    @classmethod
    def _freeze_legacy_ids(cls, value: object) -> tuple[object, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("inherited_legacy_ids must be an array")
        return tuple(value)

    @model_validator(mode="after")
    def _lifecycle_consistency(self) -> AgentState:
        if len(self.inherited_legacy_ids) != len(set(self.inherited_legacy_ids)):
            raise ValueError("inherited_legacy_ids must be unique")
        if self.alive:
            if self.death_cause is not None or self.hp == 0:
                raise ValueError("a live agent cannot have zero hp or a death cause")
        elif self.death_cause is None:
            raise ValueError("a dead agent must have a death cause")
        return self


class WorldState(StrictModel):
    """Complete deterministic, JSON-round-trippable mechanical checkpoint.

    ``model_dump(mode='json')`` may be passed back to ``WorldState.model_validate``.
    The SHA-256 helper covers the canonical JSON including the RNG checkpoint, so it
    can be stored alongside action/event logs for replay verification.
    """

    tick: Annotated[int, Field(strict=True, ge=0)]
    locations: dict[LocationId, Location]
    agents: dict[AgentId, AgentState]
    resources: dict[
        LocationId,
        dict[ResourceKind, Annotated[int, Field(strict=True, ge=0)]],
    ]
    weather: WeatherState
    rng_state: PCG64State
    false_decoy: FalseDecoyState | None = None

    @model_validator(mode="after")
    def _referential_integrity(self) -> WorldState:
        if not self.locations:
            raise ValueError("world must contain at least one location")
        if set(self.locations) != {location.id for location in self.locations.values()}:
            raise ValueError("location mapping keys must match Location.id")
        for location_id, location in self.locations.items():
            for neighbor in location.neighbors:
                if neighbor not in self.locations:
                    raise ValueError(f"unknown neighbor {neighbor!r}")
                if location_id not in self.locations[neighbor].neighbors:
                    raise ValueError("location graph edges must be symmetric")
        if set(self.resources) != set(self.locations):
            raise ValueError("resources must contain exactly every location")
        if set(self.agents) != {agent.agent_id for agent in self.agents.values()}:
            raise ValueError("agent mapping keys must match AgentState.agent_id")
        for agent in self.agents.values():
            if agent.loc not in self.locations:
                raise ValueError(f"agent {agent.agent_id!r} has an unknown location")
            if agent.birth_tick > self.tick:
                raise ValueError("agent birth_tick cannot be in the future")
        if self.false_decoy is not None:
            if self.false_decoy.location not in self.locations:
                raise ValueError("false decoy has an unknown location")
            if any(tick > self.tick for tick in self.false_decoy.emitted_ticks):
                raise ValueError("false decoy emission cannot be in the future")
        return self

    @property
    def agents_pos(self) -> dict[str, str]:
        """Compatibility projection matching the architecture document."""

        return {agent_id: agent.loc for agent_id, agent in self.agents.items()}

    def canonical_json(self) -> str:
        """Canonical, whitespace-free JSON suitable for exact replay comparison."""

        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def state_hash(self) -> str:
        """SHA-256 hex digest of ``canonical_json`` (including RNG state)."""

        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def with_weather(self, current: WeatherKind | str) -> WorldState:
        """Return a fully revalidated checkpoint with explicit next-tick weather."""

        data = self.model_dump(mode="json")
        data["weather"] = self.weather.advance(current).model_dump(mode="json")
        return WorldState.model_validate(data)


class SelfObservation(StrictModel):
    """Dry mechanical facts an agent receives about itself."""

    hp: Annotated[int, Field(strict=True, ge=1, le=100)]
    hunger: Annotated[int, Field(strict=True, ge=0, le=100)]
    age: Annotated[int, Field(strict=True, ge=0)]
    loc: LocationId
    neighbors: tuple[LocationId, ...] = Field(default_factory=tuple, max_length=16)
    inventory: dict[ResourceKind, Annotated[int, Field(strict=True, ge=0)]]

    @field_validator("neighbors", mode="before")
    @classmethod
    def _freeze_neighbors(cls, value: object) -> tuple[object, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("observation neighbors must be an array")
        return tuple(value)


class VisibleAction(StrictModel):
    """Another agent's public action, without hidden validation/rule metadata."""

    agent_id: AgentId
    action: ActionKind
    destination: LocationId | None = None
    resource: ResourceKind | None = None
    item: ResourceKind | None = None
    depth: Annotated[int, Field(strict=True, ge=1, le=10)] | None = None

    @classmethod
    def from_action(cls, action: Action) -> VisibleAction:
        payload = action.model_dump(mode="python", exclude_none=True)
        payload["action"] = payload.pop("type")
        return cls.model_validate(payload)


class ObservationEvent(StrictModel):
    """Sanitized observable consequence; true mechanical causes are impossible here."""

    type: Literal["death", "environmental_cue"]
    agent_id: AgentId | None = None
    cause_visible: VisibleDeathCause | None = None
    cue: Literal["raven"] | None = None

    @model_validator(mode="after")
    def _validate_variant(self) -> ObservationEvent:
        if self.type == "death":
            if self.agent_id is None or self.cause_visible is None or self.cue is not None:
                raise ValueError("death observation requires agent_id and cause_visible")
        elif self.cue is None or self.agent_id is not None or self.cause_visible is not None:
            raise ValueError("environmental cue observation requires only cue")
        return self


class Observation(StrictModel):
    """Personal, non-narrative view returned after a tick to each living agent."""

    tick: Annotated[int, Field(strict=True, ge=0)]
    weather: WeatherKind = "clear"
    you: SelfObservation
    visible: tuple[VisibleAction, ...] = Field(default_factory=tuple)
    events: tuple[ObservationEvent, ...] = Field(default_factory=tuple)

    @field_validator("visible", "events", mode="before")
    @classmethod
    def _freeze_observation_sequences(cls, value: object) -> tuple[object, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("observation collections must be arrays")
        return tuple(value)


_RECORD_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _assert_json_value(value: Any, *, depth: int = 0) -> None:
    """Reject non-JSON, non-finite, or pathologically deep structured records."""

    if depth > 8:
        raise ValueError("world record exceeds maximum JSON depth")
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("world records cannot contain NaN or infinity")
        return
    if isinstance(value, list):
        for item in value:
            _assert_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("world record keys must be strings")
            _assert_json_value(item, depth=depth + 1)
        return
    raise ValueError(f"world record contains non-JSON value {type(value).__name__}")


class StepResult(StrictModel):
    """Atomic result: next checkpoint, canonical records, and per-agent views.

    ``events`` and ``effects`` are immutable sequences with the exact record shape
    ``{"type": <fixed token>, "payload": <JSON object>}``.  They intentionally carry
    no wall-clock timestamps.  The data layer may timestamp commits separately.
    """

    state: WorldState
    events: tuple[dict[str, object], ...] = Field(default_factory=tuple)
    effects: tuple[dict[str, object], ...] = Field(default_factory=tuple)
    observations: dict[AgentId, Observation] = Field(default_factory=dict)

    @field_validator("events", "effects", mode="before")
    @classmethod
    def _structured_records(cls, records: object) -> tuple[dict[str, object], ...]:
        if not isinstance(records, (list, tuple)):
            raise ValueError("world records must be arrays")
        for record in records:
            if set(record) != {"type", "payload"}:
                raise ValueError("world records require exactly type and payload")
            record_type = record["type"]
            payload = record["payload"]
            if not isinstance(record_type, str) or _RECORD_TYPE.fullmatch(record_type) is None:
                raise ValueError("world record type must be a fixed snake_case token")
            if not isinstance(payload, dict):
                raise ValueError("world record payload must be an object")
            _assert_json_value(payload)
        return tuple(records)
