"""Model-facing context which exposes interface rules, never world mechanics."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .domain import Action, Observation, VisibleDeathCause

TEMPERAMENTS = (
    "careful and observant",
    "curious and cooperative",
    "skeptical and methodical",
    "bold but attentive",
    "patient and pragmatic",
    "independent and precise",
)

_WORLD_INSTRUCTIONS = (
    "You are a resident of this world. Decide from what you personally observe and from "
    "fallible ancestral records. Return exactly one available game action as structured "
    "data. Do not request or use files, commands, tools, networks, or external knowledge."
)

_DEATHBED_INSTRUCTIONS = (
    "Write only what a future resident should know. Distinguish observation from guesswork "
    "when you can. The record is limited by the provided token budget."
)

_ACTION_INTERFACE: dict[str, dict[str, object]] = {
    "noop": {},
    "move": {"destination": "a neighboring location ID"},
    "forage": {"resource": ["red_berry", "root"]},
    "eat": {"item": ["red_berry", "root"]},
    "dig": {"depth": "integer 1 through 10"},
}

_CONTEXT_SCHEMA_VERSION = 2


def prompt_template_sha256() -> str:
    """Seal the model-visible instruction/interface template in the manifest."""

    payload = {
        "context_schema_version": _CONTEXT_SCHEMA_VERSION,
        "world_instructions": _WORLD_INSTRUCTIONS,
        "deathbed_instructions": _DEATHBED_INSTRUCTIONS,
        "action_interface": _ACTION_INTERFACE,
        "memory_format": {
            "initial_observation": "dry Observation",
            "memory": [{"action": "validated Action", "result": "Observation|TerminalOutcome"}],
        },
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class Persona(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
    temperament: str = Field(min_length=1, max_length=128)


class TerminalOutcome(BaseModel):
    """The only terminal fact an agent may remember or pass to its deathbed.

    True mechanical causes never fit this schema.  The world engine's sanitized
    ``cause_visible`` is the sole causal label crossing into model context.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    tick: Annotated[int, Field(strict=True, ge=1)]
    type: Literal["death"] = "death"
    cause_visible: VisibleDeathCause


class AgentTransition(BaseModel):
    """One validated own action and its dry, immediately resulting outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    action: Action
    result: Observation | TerminalOutcome


@dataclass(slots=True)
class AgentContext:
    """Explicit cultural/runtime memory, checkpointed outside model sessions."""

    agent_id: str
    lineage_id: str
    persona: Persona
    inherited_legacy_ids: tuple[str, ...] = ()
    inherited_legacy_texts: tuple[str, ...] = ()
    history_limit: int = 128
    initial_observation: dict[str, Any] | None = None
    transitions: list[dict[str, Any]] = field(default_factory=list)
    current_observation: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.agent_id != self.persona.name:
            raise ValueError("persona name must equal agent_id")
        if len(self.inherited_legacy_ids) != len(self.inherited_legacy_texts):
            raise ValueError("legacy IDs and texts must have equal length")
        if len(set(self.inherited_legacy_ids)) != len(self.inherited_legacy_ids):
            raise ValueError("inherited legacy IDs must be unique")
        if (
            isinstance(self.history_limit, bool)
            or not isinstance(self.history_limit, int)
            or not 1 <= self.history_limit <= 1_000_000
        ):
            raise ValueError("history_limit must be an integer from 1 to 1,000,000")
        if self.initial_observation is not None:
            self.initial_observation = self._validate_observation(self.initial_observation)
        if self.current_observation is not None:
            self.current_observation = self._validate_observation(self.current_observation)
        if self.initial_observation is None and self.current_observation is not None:
            self.initial_observation = deepcopy(self.current_observation)
        if self.initial_observation is None:
            raise ValueError("agent context requires an initial observation")
        self.transitions = [self._validate_transition(item) for item in self.transitions]
        if len(self.transitions) > self.history_limit:
            raise ValueError("agent transition history exceeds history_limit")
        self._validate_history_sequence()

    @staticmethod
    def _validate_observation(observation: Observation | dict[str, Any]) -> dict[str, Any]:
        validated = (
            observation
            if isinstance(observation, Observation)
            else Observation.model_validate(observation)
        )
        return validated.model_dump(mode="json")

    @staticmethod
    def _validate_transition(
        transition: AgentTransition | dict[str, Any],
    ) -> dict[str, Any]:
        validated = (
            transition
            if isinstance(transition, AgentTransition)
            else AgentTransition.model_validate(transition)
        )
        # Action optionals are intentionally omitted from the closed wire variant,
        # while Observation optionals must remain present.  Dropping ``None`` from
        # an ObservationEvent would make its checkpoint differ from the identical
        # current Observation after round-trip validation.
        return {
            "action": validated.action.model_dump(mode="json", exclude_none=True),
            "result": validated.result.model_dump(mode="json"),
        }

    def _validate_history_sequence(self) -> None:
        previous_tick = self.initial_observation["tick"]
        terminal_seen = False
        for transition in self.transitions:
            action = transition["action"]
            result = transition["result"]
            if action["agent_id"] != self.agent_id:
                raise ValueError("transition action belongs to another agent")
            if result["tick"] <= previous_tick:
                raise ValueError("transition result ticks must increase strictly")
            if terminal_seen:
                raise ValueError("a terminal transition must be the final transition")
            terminal_seen = result.get("type") == "death"
            previous_tick = result["tick"]
        if terminal_seen:
            if self.current_observation is not None:
                raise ValueError("terminal context cannot retain a current observation")
        elif self.transitions:
            latest = self.transitions[-1]["result"]
            if self.current_observation != latest:
                raise ValueError("current observation must equal the latest transition result")

    def record_transition(
        self,
        action: Action | dict[str, Any],
        result: Observation | TerminalOutcome | dict[str, Any],
    ) -> None:
        """Append one complete lifetime transition without lossy windowing."""

        if self.current_observation is None:
            raise RuntimeError("terminal agent cannot record another transition")
        if len(self.transitions) >= self.history_limit:
            raise RuntimeError("agent transition history reached its sealed lifespan bound")
        action_model = action if isinstance(action, Action) else Action.model_validate(action)
        if action_model.agent_id != self.agent_id:
            raise ValueError("transition action belongs to another agent")
        if isinstance(result, (Observation, TerminalOutcome)):
            result_model = result
        else:
            result_model = (
                TerminalOutcome.model_validate(result)
                if result.get("type") == "death"
                else Observation.model_validate(result)
            )
        transition = AgentTransition(action=action_model, result=result_model)
        transition_json = self._validate_transition(transition)
        if transition_json["result"]["tick"] <= self.current_observation["tick"]:
            raise ValueError("transition result must follow the current observation")
        self.transitions.append(transition_json)
        if isinstance(result_model, TerminalOutcome):
            self.current_observation = None
        else:
            self.current_observation = result_model.model_dump(mode="json")

    def act_envelope(self) -> dict[str, Any]:
        if self.current_observation is None:
            raise RuntimeError("agent has no current observation")
        # Every outward value is a detached snapshot.  In particular, never return
        # the module-level interface mapping or the context's checkpoint dictionaries:
        # adapters are untrusted and may mutate objects they receive in-process.
        return deepcopy(
            {
                "instructions": _WORLD_INSTRUCTIONS,
                "persona": self.persona.model_dump(mode="json"),
                "ancestral_records": [
                    {"record_id": legacy_id, "text": text}
                    for legacy_id, text in zip(
                        self.inherited_legacy_ids,
                        self.inherited_legacy_texts,
                        strict=True,
                    )
                ],
                "initial_observation": self.initial_observation,
                "memory": self.transitions,
                "turn": self.current_observation,
                "available_actions": _ACTION_INTERFACE,
            }
        )

    def deathbed_context(self) -> dict[str, Any]:
        return deepcopy(
            {
                "instructions": _DEATHBED_INSTRUCTIONS,
                "persona": self.persona.model_dump(mode="json"),
                "ancestral_records": [
                    {"record_id": legacy_id, "text": text}
                    for legacy_id, text in zip(
                        self.inherited_legacy_ids,
                        self.inherited_legacy_texts,
                        strict=True,
                    )
                ],
                "initial_observation": self.initial_observation,
                "observed_life": self.transitions,
            }
        )

    def to_checkpoint(self) -> dict[str, Any]:
        return deepcopy(
            {
                "agent_id": self.agent_id,
                "lineage_id": self.lineage_id,
                "persona": self.persona.model_dump(mode="json"),
                "inherited_legacy_ids": list(self.inherited_legacy_ids),
                "inherited_legacy_texts": list(self.inherited_legacy_texts),
                "history_limit": self.history_limit,
                "initial_observation": self.initial_observation,
                "transitions": self.transitions,
                "current_observation": self.current_observation,
            }
        )

    @classmethod
    def from_checkpoint(cls, value: dict[str, Any]) -> AgentContext:
        required = {
            "agent_id",
            "lineage_id",
            "persona",
            "inherited_legacy_ids",
            "inherited_legacy_texts",
            "history_limit",
            "initial_observation",
            "transitions",
            "current_observation",
        }
        if set(value) != required:
            raise ValueError("agent context checkpoint keys do not match schema")
        snapshot = deepcopy(value)
        return cls(
            agent_id=str(snapshot["agent_id"]),
            lineage_id=str(snapshot["lineage_id"]),
            persona=Persona.model_validate(snapshot["persona"]),
            inherited_legacy_ids=tuple(snapshot["inherited_legacy_ids"]),
            inherited_legacy_texts=tuple(snapshot["inherited_legacy_texts"]),
            history_limit=snapshot["history_limit"],
            initial_observation=snapshot["initial_observation"],
            transitions=list(snapshot["transitions"]),
            current_observation=snapshot["current_observation"],
        )


__all__ = [
    "TEMPERAMENTS",
    "AgentContext",
    "AgentTransition",
    "Persona",
    "TerminalOutcome",
    "prompt_template_sha256",
]
