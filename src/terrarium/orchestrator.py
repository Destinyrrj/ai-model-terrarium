"""Deterministic, crash-safe experiment orchestration.

The orchestrator owns inference concurrency and agent lifecycle, but it never
implements world mechanics.  Every model response is treated as untrusted data,
validated into the closed :class:`~terrarium.domain.Action` schema, and handed to
``WorldEngine`` in canonical agent-ID order.  Lifecycle randomness is drawn through
the engine's checkpointed PCG64 state as well, so a checkpoint contains the only RNG
stream used by the experiment.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from pydantic import ValidationError

from .budget import BudgetExhausted, BudgetGovernor, BudgetUsage
from .config import RunConfig
from .domain import Action, Location, Observation, SelfObservation, WorldState
from .inheritance import (
    InheritanceManager,
    Legacy,
    LegacyChannel,
    TiktokenCodec,
)
from .manifest import RunManifest
from .prompting import TEMPERAMENTS, AgentContext, Persona, TerminalOutcome
from .runtime import (
    AdapterResult,
    AdapterStatus,
    AgentAdapter,
    DeterministicMockAdapter,
)
from .storage import EventStore, RawResponseError
from .world import WorldConfig as EngineWorldConfig
from .world import WorldEngine

AdapterFactory = Callable[[AgentContext], AgentAdapter]
RunReason = Literal["target_generation", "max_ticks"]

_CHECKPOINT_SCHEMA_VERSION = 2
_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SEALED_ACTION_INTERFACE: dict[str, dict[str, object]] = {
    "noop": {},
    "move": {"destination": "a neighboring location ID"},
    "forage": {"resource": ["red_berry", "root"]},
    "eat": {"item": ["red_berry", "root"]},
    "dig": {"depth": "integer 1 through 10"},
}


class ExperimentComplete(RuntimeError):
    """Raised when ``step`` is requested after the configured target is reached."""


@dataclass(frozen=True, slots=True)
class LineageState:
    """Small, explicit lifecycle checkpoint for one population slot."""

    lineage_id: str
    slot: int
    generation: int
    current_agent_id: str
    legacy_ids: tuple[str, ...] = ()

    def to_checkpoint(self) -> dict[str, Any]:
        return {
            "lineage_id": self.lineage_id,
            "slot": self.slot,
            "generation": self.generation,
            "current_agent_id": self.current_agent_id,
            "legacy_ids": list(self.legacy_ids),
        }

    @classmethod
    def from_checkpoint(cls, value: object) -> LineageState:
        if not isinstance(value, dict) or set(value) != {
            "lineage_id",
            "slot",
            "generation",
            "current_agent_id",
            "legacy_ids",
        }:
            raise ValueError("lineage checkpoint keys do not match schema")
        lineage_id = value["lineage_id"]
        agent_id = value["current_agent_id"]
        slot = value["slot"]
        generation = value["generation"]
        legacy_ids = value["legacy_ids"]
        if not isinstance(lineage_id, str) or not lineage_id:
            raise ValueError("lineage_id must be a non-empty string")
        if not isinstance(agent_id, str) or not agent_id:
            raise ValueError("current_agent_id must be a non-empty string")
        if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
            raise ValueError("lineage slot must be a non-negative integer")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("lineage generation must be a non-negative integer")
        if not isinstance(legacy_ids, list) or any(
            not isinstance(item, str) for item in legacy_ids
        ):
            raise ValueError("lineage legacy_ids must be a string list")
        if len(legacy_ids) != len(set(legacy_ids)):
            raise ValueError("lineage legacy_ids must be unique")
        return cls(
            lineage_id=lineage_id,
            slot=slot,
            generation=generation,
            current_agent_id=agent_id,
            legacy_ids=tuple(legacy_ids),
        )


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Outcome of one ``run`` invocation."""

    run_id: str
    completed: bool
    reason: RunReason
    ticks_run: int
    world_tick: int
    state_hash: str
    target_generation: int
    live_agents: tuple[str, ...]


def _detached_json(value: Any) -> Any:
    """Canonical JSON round-trip used at every mutable adapter/storage boundary."""

    return json.loads(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _json_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _agent_id(run_id: str, slot: int, generation: int) -> str:
    """Return an opaque deterministic ID which does not reveal generation."""

    material = f"{run_id}\x00{slot}\x00{generation}".encode()
    return f"agent_{hashlib.sha256(material).hexdigest()[:24]}"


def _lineage_id(slot: int) -> str:
    return f"lineage_{slot:03d}"


def _stage_decoy_coincidences(
    engine: WorldEngine,
    world: WorldState,
    *,
    birth_tick: int,
    lifespan_ticks: int,
) -> tuple[WorldState, list[dict[str, Any]]]:
    """Deterministically schedule one or two observable generation-zero coincidences.

    The architecture asks the first cohort to contain 1--2 deliberately arranged
    raven/death coincidences.  Victims are sampled without replacement through the
    world's sole checkpointed PCG64 stream and die by the ordinary old-age rule.
    At least one population member is left unstaged as a witness.  The checked MVP
    has a two-node valley; world observations treat the neighboring node as nearby.
    """

    candidates = sorted(agent_id for agent_id, agent in world.agents.items() if agent.alive)
    possible = min(2, len(candidates) - 1, lifespan_ticks - birth_tick + 1)
    if possible < 1:
        return world, []

    scheduled = world
    scheduled, count_index = engine.rng_draw_index(scheduled, possible)
    count = count_index + 1
    chosen: list[tuple[str, int]] = []
    remaining = list(candidates)
    for offset in range(count):
        scheduled, victim_index = engine.rng_draw_index(scheduled, len(remaining))
        victim = remaining.pop(victim_index)
        chosen.append((victim, birth_tick + offset))

    data = scheduled.model_dump(mode="json")
    for victim, death_age in chosen:
        data["agents"][victim]["max_age"] = death_age
    scheduled = WorldState.model_validate(data)
    records = [
        {
            "type": "decoy_coincidence_scheduled",
            "payload": {
                "agent_id": victim,
                "cohort_generation": 0,
                "scheduled_age": death_age,
            },
        }
        for victim, death_age in chosen
    ]
    return scheduled, records


class ExperimentRunner:
    """Run or resume one sealed Terrarium experiment.

    Construction opens the single-writer event store, verifies any committed log,
    and either restores its latest composite checkpoint or writes tick-zero genesis.
    Adapter processes are created lazily in ``run``/``step`` and are always closed
    when ``run`` returns or raises.
    """

    def __init__(
        self,
        config: RunConfig,
        run_dir: str | Path,
        adapter_factory: AdapterFactory | None = None,
    ) -> None:
        self.config = config
        self.run_dir = Path(run_dir).absolute()
        self.target_generation = config.population.generations - 1
        self._closed = False
        self._run_lock = asyncio.Lock()
        self._adapters: dict[str, AgentAdapter] = {}

        if adapter_factory is None:
            if config.runtime.adapter != "mock":
                raise ValueError(
                    "the default adapter factory supports only runtime.adapter='mock'; "
                    "inject an explicit pinned factory for real runtimes"
                )
            self._adapter_factory = self._default_mock_factory
        else:
            self._adapter_factory = adapter_factory

        mechanics = EngineWorldConfig(
            max_age=config.population.lifespan_ticks,
            hunger_per_tick=config.world.hunger_per_tick,
            starvation_damage=config.world.starvation_damage,
            starvation_lethal=config.world.starvation_lethal,
            poison_damage=config.world.poison_damage,
            collapse_probability=config.world.collapse_probability,
            collapse_lethal=config.world.collapse_lethal,
            rain_probability=config.world.rain_probability,
        )
        self.engine = WorldEngine(mechanics)
        self.codec = TiktokenCodec(config.population.tokenizer)
        self.inheritance = InheritanceManager(self.codec)
        self.budget = BudgetGovernor(config.budget)
        self._world: WorldState
        self._contexts: dict[str, AgentContext]
        self._lineages: dict[str, LineageState]

        manifest = RunManifest.from_config(config)
        self.manifest = manifest
        manifest.write_new(self.run_dir / "manifest.json")

        self.store: EventStore | None = None
        try:
            self.store = EventStore(
                self.run_dir,
                config.run_id,
                max_raw_bytes=max(1, config.storage.max_raw_bytes),
            )
            # A resume trusts neither the replaceable SQLite projection nor merely
            # the latest row.  Verify the complete hash chain/projection first.
            self.store.verify()
            checkpoint = self.store.load_latest_checkpoint()
            if checkpoint is None:
                self._initialize_new_run()
            else:
                self._restore(checkpoint)
        except BaseException:
            if self.store is not None:
                self.store.close()
            self._closed = True
            raise

    @property
    def world_state(self) -> WorldState:
        return self._world

    @property
    def contexts(self) -> Mapping[str, AgentContext]:
        # MappingProxyType alone would still expose each mutable AgentContext and
        # let library callers alter future prompts without a committed event.
        snapshots = {
            agent_id: AgentContext.from_checkpoint(context.to_checkpoint())
            for agent_id, context in self._contexts.items()
        }
        return MappingProxyType(snapshots)

    @property
    def lineages(self) -> tuple[LineageState, ...]:
        return tuple(self._lineages[key] for key in sorted(self._lineages))

    @property
    def completed(self) -> bool:
        return all(
            lineage.generation >= self.target_generation for lineage in self._lineages.values()
        )

    def _default_mock_factory(self, context: AgentContext) -> AgentAdapter:
        return DeterministicMockAdapter(
            context.agent_id,
            context.inherited_legacy_texts,
            seed=self.config.seed,
        )

    def _initialize_new_run(self) -> None:
        valley = self.config.world.valley
        grove = f"{valley}/grove"
        cave = f"{valley}/cave"
        initial_location = self.config.world.initial_location
        if "/" not in initial_location:
            initial_location = f"{valley}/{initial_location}"
        if initial_location not in {grove, cave}:
            raise ValueError("one-valley MVP initial_location must be its grove or cave")

        locations = {
            cave: Location(id=cave, neighbors=[grove]),
            grove: Location(id=grove, neighbors=[cave]),
        }
        food = self.config.world.initial_food_per_location
        berries = (food + 1) // 2
        roots = food // 2
        resources = {
            cave: {"red_berry": berries, "root": roots},
            grove: {"red_berry": berries, "root": roots},
        }
        agent_ids = [
            _agent_id(self.config.run_id, slot, 0) for slot in range(self.config.population.size)
        ]
        slots_by_agent = {agent_id: slot for slot, agent_id in enumerate(agent_ids)}
        positions = {agent_id: initial_location for agent_id in agent_ids}
        world = self.engine.initial_state(
            self.config.seed,
            agent_ids,
            starting_positions=positions,
            locations=locations,
            resources=resources,
            enable_false_decoy=True,
            false_decoy_birth_tick=self.config.world.decoy_birth_tick,
        )
        world, decoy_schedule_events = _stage_decoy_coincidences(
            self.engine,
            world,
            birth_tick=self.config.world.decoy_birth_tick,
            lifespan_ticks=self.config.population.lifespan_ticks,
        )

        contexts: dict[str, AgentContext] = {}
        lineages: dict[str, LineageState] = {}
        for agent_id in sorted(agent_ids):
            slot = slots_by_agent[agent_id]
            world, temperament_index = self.engine.rng_draw_index(world, len(TEMPERAMENTS))
            lineage_id = _lineage_id(slot)
            context = AgentContext(
                agent_id=agent_id,
                lineage_id=lineage_id,
                persona=Persona(
                    name=agent_id,
                    temperament=TEMPERAMENTS[temperament_index],
                ),
                history_limit=self.config.population.lifespan_ticks,
                current_observation=self._dry_self_observation(world, agent_id),
            )
            contexts[agent_id] = context
            lineages[lineage_id] = LineageState(
                lineage_id=lineage_id,
                slot=slot,
                generation=0,
                current_agent_id=agent_id,
            )

        events: list[dict[str, Any]] = [
            {
                "type": "run_started",
                "payload": {
                    "initial_world": world.model_dump(mode="json"),
                    "manifest_sha256": hashlib.sha256(self.manifest.canonical_bytes()).hexdigest(),
                },
            }
        ]
        events.extend(decoy_schedule_events)
        for lineage in sorted(lineages.values(), key=lambda item: item.current_agent_id):
            events.append(
                {
                    "type": "agent_spawned",
                    "payload": {
                        "agent_id": lineage.current_agent_id,
                        "generation": 0,
                        "generation_id": "generation_00000",
                        "lineage_id": lineage.lineage_id,
                        "location": initial_location,
                        "inherited_legacy_ids": [],
                        "provider": self.config.runtime.provider,
                        "model": self.config.runtime.model_id,
                        "valley": valley,
                    },
                }
            )
            events.append(
                self._observation_event(
                    lineage.current_agent_id,
                    contexts[lineage.current_agent_id].current_observation,
                )
            )

        checkpoint = self._checkpoint_for(world, contexts, lineages, self.inheritance)
        assert self.store is not None
        self.store.commit_tick(0, events, checkpoint)
        self._world = world
        self._contexts = contexts
        self._lineages = lineages

    def _restore(self, checkpoint: dict[str, Any]) -> None:
        state = checkpoint.get("state")
        if not isinstance(state, dict) or set(state) != {
            "schema_version",
            "config_sha256",
            "world",
            "rng_state",
            "contexts",
            "lineages",
            "legacies",
            "budget",
        }:
            raise ValueError("orchestrator checkpoint keys do not match schema")
        if state["schema_version"] != _CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("unsupported orchestrator checkpoint schema")
        if state["config_sha256"] != self.config.digest():
            raise RuntimeError("checkpoint config digest differs from the sealed manifest")

        world = WorldState.model_validate(state["world"])
        if checkpoint.get("tick") != world.tick:
            raise RuntimeError("checkpoint tick differs from WorldState.tick")
        rng = world.rng_state.model_dump(mode="json")
        if state["rng_state"] != rng or checkpoint.get("rng_state") != rng:
            raise RuntimeError("checkpoint RNG copies are inconsistent")

        raw_contexts = state["contexts"]
        if not isinstance(raw_contexts, dict):
            raise ValueError("checkpoint contexts must be an object")
        contexts: dict[str, AgentContext] = {}
        for agent_id in sorted(raw_contexts):
            if not isinstance(agent_id, str) or not isinstance(raw_contexts[agent_id], dict):
                raise ValueError("invalid context checkpoint entry")
            context = AgentContext.from_checkpoint(raw_contexts[agent_id])
            if context.agent_id != agent_id:
                raise ValueError("context mapping key differs from agent_id")
            contexts[agent_id] = context

        raw_lineages = state["lineages"]
        if not isinstance(raw_lineages, dict):
            raise ValueError("checkpoint lineages must be an object")
        lineages: dict[str, LineageState] = {}
        for lineage_id in sorted(raw_lineages):
            lineage = LineageState.from_checkpoint(raw_lineages[lineage_id])
            if lineage.lineage_id != lineage_id:
                raise ValueError("lineage mapping key differs from lineage_id")
            lineages[lineage_id] = lineage

        raw_legacies = state["legacies"]
        if not isinstance(raw_legacies, list):
            raise ValueError("checkpoint legacies must be a list")
        # Legacy is strict and contains an enum plus tuple.  ``model_validate_json``
        # intentionally uses Pydantic's JSON-mode representation written by the
        # checkpoint, while still rejecting unknown fields and invalid values.
        legacies = [
            Legacy.model_validate_json(
                json.dumps(
                    item,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            for item in raw_legacies
        ]
        inheritance = InheritanceManager.from_records(self.codec, legacies)

        budget_data = state["budget"]
        if not isinstance(budget_data, dict) or set(budget_data) != {
            "calls",
            "input_tokens",
            "output_tokens",
            "failures",
        }:
            raise ValueError("checkpoint budget keys do not match schema")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in budget_data.values()
        ):
            raise ValueError("checkpoint budget values must be non-negative integers")

        self._validate_restored_graph(world, contexts, lineages, inheritance)
        self._world = world
        self._contexts = contexts
        self._lineages = lineages
        self.inheritance = inheritance
        self.budget = BudgetGovernor(
            self.config.budget,
            BudgetUsage(**budget_data),
        )

    def _validate_restored_graph(
        self,
        world: WorldState,
        contexts: Mapping[str, AgentContext],
        lineages: Mapping[str, LineageState],
        inheritance: InheritanceManager,
    ) -> None:
        if len(lineages) != self.config.population.size:
            raise RuntimeError("checkpoint lineage count differs from target population")
        slots = [lineage.slot for lineage in lineages.values()]
        if sorted(slots) != list(range(self.config.population.size)):
            raise RuntimeError("checkpoint lineage slots are not canonical")
        live_ids = {agent_id for agent_id, agent in world.agents.items() if agent.alive}
        current_ids = {lineage.current_agent_id for lineage in lineages.values()}
        if live_ids != current_ids or live_ids != set(contexts):
            raise RuntimeError("world, context and lineage live-agent sets differ")
        records = {legacy.id: legacy for legacy in inheritance.records}
        for lineage in lineages.values():
            context = contexts[lineage.current_agent_id]
            agent = world.agents[lineage.current_agent_id]
            if context.lineage_id != lineage.lineage_id:
                raise RuntimeError("context belongs to the wrong lineage")
            if agent.generation != lineage.generation:
                raise RuntimeError("world and lineage generations differ")
            if tuple(agent.inherited_legacy_ids) != context.inherited_legacy_ids:
                raise RuntimeError("world and context inherited legacy IDs differ")
            if any(legacy_id not in records for legacy_id in context.inherited_legacy_ids):
                raise RuntimeError("context references an unknown inherited legacy")
            expected_texts = tuple(records[item].text for item in context.inherited_legacy_ids)
            if expected_texts != context.inherited_legacy_texts:
                raise RuntimeError("context inherited texts differ from immutable records")
            if any(legacy_id not in records for legacy_id in lineage.legacy_ids):
                raise RuntimeError("lineage history references an unknown legacy")

    async def _ensure_adapters(self) -> None:
        live_ids = sorted(agent_id for agent_id, agent in self._world.agents.items() if agent.alive)
        stale_ids = sorted(set(self._adapters) - set(live_ids))
        for agent_id in stale_ids:
            await self._close_one_adapter(agent_id)
        try:
            for agent_id in live_ids:
                if agent_id in self._adapters:
                    continue
                # The factory receives a detached context copy.  It cannot mutate
                # the authoritative rolling memory or prompt template by aliasing.
                context_copy = AgentContext.from_checkpoint(
                    _detached_json(self._contexts[agent_id].to_checkpoint())
                )
                adapter = self._adapter_factory(context_copy)
                if inspect.isawaitable(adapter):
                    if inspect.iscoroutine(adapter):
                        adapter.close()
                    raise TypeError("adapter_factory must be synchronous")
                required = ("act", "write_legacy", "retell", "answer_survey", "close")
                if any(not callable(getattr(adapter, name, None)) for name in required):
                    raise TypeError("adapter_factory returned an incompatible adapter")
                self._adapters[agent_id] = adapter
        except BaseException:
            await self._close_all_adapters()
            raise

    async def _reserve_batch(self, count: int) -> None:
        if count == 0:
            return
        snapshot = self.budget.snapshot()
        if snapshot["calls"] + count > self.config.budget.max_calls:
            raise BudgetExhausted("model call budget cannot cover the canonical batch")
        if snapshot["failures"] > self.config.budget.max_failures:
            raise BudgetExhausted("model failure budget exhausted")
        for _ in range(count):
            await self.budget.reserve_call()

    async def _call_act(self, agent_id: str, envelope: dict[str, Any]) -> AdapterResult:
        try:
            result = await asyncio.wait_for(
                self._adapters[agent_id].act(_detached_json(envelope)),
                timeout=self.config.runtime.timeout_seconds,
            )
        except TimeoutError:
            return AdapterResult.failure(
                AdapterStatus.TIMEOUT,
                "orchestrator_timeout",
                "adapter exceeded the sealed timeout",
                usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return AdapterResult.failure(
                AdapterStatus.INTERNAL_ERROR,
                "adapter_exception",
                "adapter raised an exception",
                usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            )
        if not isinstance(result, AdapterResult):
            return AdapterResult.failure(
                AdapterStatus.INVALID_OUTPUT,
                "wrong_result_type",
                "adapter did not return AdapterResult",
                usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            )
        return result

    async def _call_deathbed(self, agent_id: str, context: dict[str, Any]) -> AdapterResult:
        try:
            result = await asyncio.wait_for(
                self._adapters[agent_id].write_legacy(
                    self.config.population.legacy_tokens,
                    _detached_json(context),
                ),
                timeout=self.config.runtime.timeout_seconds,
            )
        except TimeoutError:
            return AdapterResult.failure(
                AdapterStatus.TIMEOUT,
                "orchestrator_timeout",
                "deathbed call exceeded the sealed timeout",
                usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return AdapterResult.failure(
                AdapterStatus.INTERNAL_ERROR,
                "adapter_exception",
                "deathbed adapter raised an exception",
                usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            )
        if not isinstance(result, AdapterResult):
            return AdapterResult.failure(
                AdapterStatus.INVALID_OUTPUT,
                "wrong_result_type",
                "deathbed adapter did not return AdapterResult",
                usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            )
        return result

    def _payload_within_limit(self, result: AdapterResult) -> bool:
        try:
            size = len(
                json.dumps(
                    result.payload,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            )
        except (TypeError, ValueError, RecursionError, UnicodeError):
            return False
        return size <= self.config.runtime.max_output_bytes

    async def _account_result(
        self,
        *,
        agent_id: str,
        operation: str,
        result: AdapterResult,
        success: bool,
    ) -> dict[str, Any]:
        usage = result.usage
        input_tokens = usage.get("input_tokens") if usage is not None else None
        output_tokens = usage.get("output_tokens") if usage is not None else None
        await self.budget.record(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            success=success,
        )
        assert input_tokens is not None and output_tokens is not None
        reasoning_tokens = usage.get("reasoning_tokens", 0) if usage is not None else 0
        return {
            "type": "token_usage",
            "payload": {
                "agent_id": agent_id,
                "operation": operation,
                "provider": self.config.runtime.provider,
                "model": self.config.runtime.model_id,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "total_tokens": input_tokens + output_tokens + reasoning_tokens,
                "success": success,
                "status": result.status.value,
            },
        }

    def _capture_raw(
        self,
        *,
        agent_id: str,
        generation: int,
        tick: int,
        channel: str,
        result: AdapterResult,
    ) -> dict[str, Any] | None:
        if not self.config.storage.raw_responses:
            return None
        assert self.store is not None
        record = {
            "status": result.status.value,
            "payload": result.payload,
            "raw_text": result.raw_text,
            "error_code": self._safe_error_code(result),
        }
        try:
            self.store.append_raw(
                generation,
                agent_id,
                tick,
                record,
                channel=channel,
            )
        except RawResponseError:
            return {
                "type": "raw_response_dropped",
                "payload": {
                    "agent_id": agent_id,
                    "channel": channel,
                    "reason": "raw_record_rejected",
                },
            }
        return {
            "type": "raw_response_stored",
            "payload": {"agent_id": agent_id, "channel": channel},
        }

    @staticmethod
    def _safe_error_code(result: AdapterResult) -> str | None:
        if result.error is None:
            return None
        code = result.error.code
        return code if _SAFE_ERROR_CODE.fullmatch(code) is not None else "adapter_error"

    def _validate_action(
        self, agent_id: str, result: AdapterResult
    ) -> tuple[Action, bool, str | None]:
        if not result.ok:
            return Action(agent_id=agent_id, type="noop"), False, result.status.value
        if not self._payload_within_limit(result):
            return Action(agent_id=agent_id, type="noop"), False, "output_limit"
        try:
            action = Action.model_validate(result.payload)
        except (ValidationError, TypeError, ValueError):
            return Action(agent_id=agent_id, type="noop"), False, "schema_invalid"
        if action.agent_id != agent_id:
            return Action(agent_id=agent_id, type="noop"), False, "agent_id_mismatch"
        return action, True, None

    async def step(self) -> int:
        """Advance and durably commit one complete world/lifecycle tick."""

        async with self._run_lock:
            return await self._step_unlocked()

    async def _step_unlocked(self) -> int:
        self._ensure_open()
        if self.completed:
            raise ExperimentComplete("all lineages already reached the target generation")
        await self._ensure_adapters()

        live_ids = sorted(agent_id for agent_id, agent in self._world.agents.items() if agent.alive)
        if len(live_ids) != self.config.population.size:
            raise RuntimeError("target population invariant is broken")
        await self._reserve_batch(len(live_ids))

        envelopes: dict[str, dict[str, Any]] = {}
        for agent_id in live_ids:
            envelope = self._contexts[agent_id].act_envelope()
            # AgentContext intentionally returns a lightweight envelope.  Replace
            # its module-level mutable interface object with our sealed copy before
            # crossing into adapter code, then detach the complete request.
            envelope["available_actions"] = _SEALED_ACTION_INTERFACE
            envelopes[agent_id] = _detached_json(envelope)
        # gather preserves the canonical input order even when calls complete in a
        # different wall-clock order.  No result is applied as it arrives.
        results = await asyncio.gather(
            *(self._call_act(agent_id, envelopes[agent_id]) for agent_id in live_ids)
        )

        events: list[dict[str, Any]] = []
        accepted: dict[str, Action] = {}
        for agent_id, result in zip(live_ids, results, strict=True):
            action, valid, reason = self._validate_action(agent_id, result)
            accepted[agent_id] = action
            raw_event = self._capture_raw(
                agent_id=agent_id,
                generation=self._world.agents[agent_id].generation,
                tick=self._world.tick + 1,
                channel="act",
                result=result,
            )
            if raw_event is not None:
                events.append(raw_event)
            events.append(
                await self._account_result(
                    agent_id=agent_id,
                    operation="act",
                    result=result,
                    success=valid,
                )
            )
            action_payload = action.model_dump(mode="json", exclude_none=True)
            if valid:
                events.append(
                    {
                        "type": "intent",
                        "payload": {
                            "action_id": f"{self._world.tick + 1}:{agent_id}",
                            "agent_id": agent_id,
                            "action": action_payload,
                            "valid": True,
                        },
                    }
                )
            else:
                events.append(
                    {
                        "type": "invalid_action",
                        "payload": {
                            "action_id": f"{self._world.tick + 1}:{agent_id}",
                            "agent_id": agent_id,
                            "action": action_payload,
                            "valid": False,
                            "reason": reason or "invalid_output",
                            "error_code": self._safe_error_code(result),
                            "replacement": "noop",
                        },
                    }
                )

        before_world = self._world
        before_hash = before_world.state_hash()
        step_result = self.engine.step(before_world, accepted)
        lifecycle_world = step_result.state
        intermediate_hash = lifecycle_world.state_hash()
        accepted_actions = [
            accepted[agent_id].model_dump(mode="json", exclude_none=True) for agent_id in live_ids
        ]
        events.append(
            {
                "type": "world_step",
                "payload": {
                    "before_state_hash": before_hash,
                    "after_state_hash": intermediate_hash,
                    "accepted_actions": accepted_actions,
                },
            }
        )
        world_outputs = {
            "events": _detached_json(step_result.events),
            "effects": _detached_json(step_result.effects),
            "observations": {
                agent_id: observation.model_dump(mode="json")
                for agent_id, observation in sorted(step_result.observations.items())
            },
        }
        events.append(
            {
                "type": "world_outputs",
                "payload": {
                    "events_sha256": _json_hash(world_outputs["events"]),
                    "effects_sha256": _json_hash(world_outputs["effects"]),
                    "observations_sha256": _json_hash(world_outputs["observations"]),
                },
            }
        )
        events.extend(world_outputs["events"])
        events.extend(world_outputs["effects"])

        contexts = {
            agent_id: AgentContext.from_checkpoint(_detached_json(context.to_checkpoint()))
            for agent_id, context in self._contexts.items()
        }
        lineages = dict(self._lineages)
        inheritance = InheritanceManager.from_records(self.codec, self.inheritance.records)
        dead_ids = sorted(
            agent_id for agent_id in live_ids if not lifecycle_world.agents[agent_id].alive
        )
        for agent_id, observation in sorted(step_result.observations.items()):
            contexts[agent_id].record_transition(accepted[agent_id], observation)
            events.append(self._observation_event(agent_id, observation.model_dump(mode="json")))

        # Dead agents are absent from normal observations.  Preserve the lethal
        # own action and only the engine's sanitized visible outcome before the
        # deathbed call; the true mechanical cause never enters AgentContext.
        terminal_outcomes: dict[str, TerminalOutcome] = {}
        for record in step_result.events:
            if record.get("type") != "death":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                raise RuntimeError("world death event payload is not an object")
            agent_id = payload.get("agent_id")
            if agent_id not in dead_ids or agent_id in terminal_outcomes:
                raise RuntimeError("world emitted an unexpected or duplicate death event")
            terminal_outcomes[agent_id] = TerminalOutcome(
                tick=lifecycle_world.tick,
                cause_visible=payload.get("cause_visible"),
            )
        if set(terminal_outcomes) != set(dead_ids):
            raise RuntimeError("every dead agent requires one sanitized terminal outcome")
        for agent_id in dead_ids:
            contexts[agent_id].record_transition(accepted[agent_id], terminal_outcomes[agent_id])

        death_results: list[AdapterResult] = []
        if dead_ids:
            await self._reserve_batch(len(dead_ids))
            death_results = list(
                await asyncio.gather(
                    *(
                        self._call_deathbed(
                            agent_id,
                            _detached_json(contexts[agent_id].deathbed_context()),
                        )
                        for agent_id in dead_ids
                    )
                )
            )

        lineage_by_agent = {lineage.current_agent_id: lineage for lineage in lineages.values()}
        for agent_id, result in zip(dead_ids, death_results, strict=True):
            lineage = lineage_by_agent[agent_id]
            context = contexts[agent_id]
            dead_agent = lifecycle_world.agents[agent_id]
            if tuple(dead_agent.inherited_legacy_ids) != context.inherited_legacy_ids:
                raise RuntimeError("deathbed provenance differs from exact records read")
            payload = result.payload
            valid_text = (
                result.ok
                and self._payload_within_limit(result)
                and isinstance(payload, dict)
                and set(payload) == {"text"}
                and isinstance(payload["text"], str)
                and bool(payload["text"].strip())
            )
            raw_event = self._capture_raw(
                agent_id=agent_id,
                generation=lineage.generation,
                tick=lifecycle_world.tick,
                channel="deathbed",
                result=result,
            )
            if raw_event is not None:
                events.append(raw_event)
            legacy: Legacy | None = None
            if valid_text:
                assert isinstance(payload, dict) and isinstance(payload["text"], str)
                try:
                    # Creation runs before accounting: model-controlled text can
                    # still fail here (whitespace-only after token truncation),
                    # and that must count against the fail-closed failure budget
                    # instead of being logged as a successful call.
                    legacy = inheritance.create(
                        author=agent_id,
                        generation=lineage.generation,
                        valley=self.config.world.valley,
                        channel=LegacyChannel.WRITTEN,
                        text=payload["text"],
                        parent_legacy_ids=context.inherited_legacy_ids,
                        max_tokens=self.config.population.legacy_tokens,
                    )
                except (ValidationError, TypeError, ValueError):
                    valid_text = False
            events.append(
                await self._account_result(
                    agent_id=agent_id,
                    operation="deathbed",
                    result=result,
                    success=valid_text,
                )
            )
            if legacy is not None:
                lineages[lineage.lineage_id] = LineageState(
                    lineage_id=lineage.lineage_id,
                    slot=lineage.slot,
                    generation=lineage.generation,
                    current_agent_id=lineage.current_agent_id,
                    legacy_ids=(*lineage.legacy_ids, legacy.id),
                )
                lineage_by_agent[agent_id] = lineages[lineage.lineage_id]
                events.append(
                    {
                        "type": "legacy_written",
                        "payload": {
                            "legacy_id": legacy.id,
                            "author_agent_id": legacy.author,
                            "generation": legacy.generation,
                            "generation_id": f"generation_{legacy.generation:05d}",
                            "valley": legacy.valley,
                            "channel": legacy.channel.value,
                            "text": legacy.text,
                            "parent_legacy_ids": list(legacy.parent_legacy_ids),
                        },
                    }
                )
            if not valid_text:
                events.append(
                    {
                        "type": "deathbed_failed",
                        "payload": {
                            "agent_id": agent_id,
                            "generation": lineage.generation,
                            "reason": (result.status.value if not result.ok else "invalid_legacy"),
                        },
                    }
                )

            events.append(
                {
                    "type": "death_recorded",
                    "payload": {
                        "agent_id": agent_id,
                        "lineage_id": lineage.lineage_id,
                        "generation": lineage.generation,
                        "generation_id": f"generation_{lineage.generation:05d}",
                        "cause": dead_agent.death_cause,
                    },
                }
            )

        for agent_id in dead_ids:
            await self._close_one_adapter(agent_id, events=events)

        lifecycle_operations: list[dict[str, Any]] = []
        records_by_id = {legacy.id: legacy for legacy in inheritance.records}
        for agent_id in dead_ids:
            previous = lineage_by_agent[agent_id]
            next_generation = previous.generation + 1
            child_id = _agent_id(self.config.run_id, previous.slot, next_generation)

            def draw_parent_index(upper: int, *, subject: str = child_id) -> int:
                nonlocal lifecycle_world
                lifecycle_world, index = self.engine.rng_draw_index(lifecycle_world, upper)
                lifecycle_operations.append(
                    {
                        "type": "rng_draw",
                        "purpose": "parent_legacy",
                        "subject": subject,
                        "upper": upper,
                        "index": index,
                    }
                )
                return index

            inherited_ids = inheritance.select_parent_ids(
                preferred_ids=previous.legacy_ids,
                candidate_ids=records_by_id,
                count=self.config.population.inherited_legacies,
                draw_index=draw_parent_index,
            )
            inherited_texts = tuple(records_by_id[item].text for item in inherited_ids)
            lifecycle_world, temperament_index = self.engine.rng_draw_index(
                lifecycle_world, len(TEMPERAMENTS)
            )
            lifecycle_operations.append(
                {
                    "type": "rng_draw",
                    "purpose": "temperament",
                    "subject": child_id,
                    "upper": len(TEMPERAMENTS),
                    "index": temperament_index,
                }
            )
            spawn_location = f"{self.config.world.valley}/grove"
            lifecycle_world, spawn_event = self.engine.spawn_agent(
                lifecycle_world,
                child_id,
                next_generation,
                inherited_ids,
                location=spawn_location,
            )
            lifecycle_operations.append(
                {
                    "type": "spawn",
                    "agent_id": child_id,
                    "generation": next_generation,
                    "inherited_legacy_ids": list(inherited_ids),
                    "location": spawn_location,
                }
            )
            spawn_payload = _detached_json(spawn_event["payload"])
            spawn_payload.update(
                {
                    "lineage_id": previous.lineage_id,
                    "generation_id": f"generation_{next_generation:05d}",
                    "provider": self.config.runtime.provider,
                    "model": self.config.runtime.model_id,
                    "valley": self.config.world.valley,
                }
            )
            events.append({"type": "agent_spawned", "payload": spawn_payload})

            child_context = AgentContext(
                agent_id=child_id,
                lineage_id=previous.lineage_id,
                persona=Persona(
                    name=child_id,
                    temperament=TEMPERAMENTS[temperament_index],
                ),
                inherited_legacy_ids=inherited_ids,
                inherited_legacy_texts=inherited_texts,
                history_limit=self.config.population.lifespan_ticks,
                current_observation=self._dry_self_observation(lifecycle_world, child_id),
            )
            del contexts[agent_id]
            contexts[child_id] = child_context
            lineages[previous.lineage_id] = LineageState(
                lineage_id=previous.lineage_id,
                slot=previous.slot,
                generation=next_generation,
                current_agent_id=child_id,
                legacy_ids=previous.legacy_ids,
            )
            events.append(self._observation_event(child_id, child_context.current_observation))

        events.append(
            {
                "type": "lifecycle_step",
                "payload": {
                    "before_state_hash": intermediate_hash,
                    "after_state_hash": lifecycle_world.state_hash(),
                    "operations": lifecycle_operations,
                },
            }
        )

        checkpoint_state = self._checkpoint_for(
            lifecycle_world,
            contexts,
            lineages,
            inheritance,
        )
        assert self.store is not None
        self.store.commit_tick(lifecycle_world.tick, events, checkpoint_state)
        self._world = lifecycle_world
        self._contexts = contexts
        self._lineages = lineages
        self.inheritance = inheritance
        return lifecycle_world.tick

    async def run(self, max_ticks: int | None = None) -> RunSummary:
        """Run until every lineage reaches the target or the safety cap fires.

        ``max_ticks`` counts additional commits in this invocation.  When omitted,
        a conservative age-bound-derived cap is used; old age guarantees progress
        for the written-channel MVP even when every action is a no-op.
        """

        if max_ticks is not None and (
            isinstance(max_ticks, bool) or not isinstance(max_ticks, int) or max_ticks < 0
        ):
            raise ValueError("max_ticks must be a non-negative integer or None")
        async with self._run_lock:
            self._ensure_open()
            if max_ticks is None:
                minimum_generation = min(lineage.generation for lineage in self._lineages.values())
                remaining = max(0, self.target_generation - minimum_generation)
                max_ticks = max(
                    1,
                    self.config.population.lifespan_ticks * max(1, remaining) * 2,
                )
            ticks_run = 0
            try:
                while not self.completed and ticks_run < max_ticks:
                    await self._step_unlocked()
                    ticks_run += 1
            except BaseException:
                await asyncio.shield(self._close_all_adapters())
                raise
            await self._close_all_adapters()
            reason: RunReason = "target_generation" if self.completed else "max_ticks"
            return RunSummary(
                run_id=self.config.run_id,
                completed=self.completed,
                reason=reason,
                ticks_run=ticks_run,
                world_tick=self._world.tick,
                state_hash=self._world.state_hash(),
                target_generation=self.target_generation,
                live_agents=tuple(
                    sorted(
                        agent_id for agent_id, agent in self._world.agents.items() if agent.alive
                    )
                ),
            )

    def _checkpoint_for(
        self,
        world: WorldState,
        contexts: Mapping[str, AgentContext],
        lineages: Mapping[str, LineageState],
        inheritance: InheritanceManager,
    ) -> dict[str, Any]:
        checkpoint = {
            "schema_version": _CHECKPOINT_SCHEMA_VERSION,
            "config_sha256": self.config.digest(),
            "world": world.model_dump(mode="json"),
            # EventStore independently extracts this top-level copy.  Resume checks
            # it against both the event envelope and WorldState.
            "rng_state": world.rng_state.model_dump(mode="json"),
            "contexts": {
                agent_id: context.to_checkpoint() for agent_id, context in sorted(contexts.items())
            },
            "lineages": {
                lineage_id: lineage.to_checkpoint()
                for lineage_id, lineage in sorted(lineages.items())
            },
            "legacies": [legacy.model_dump(mode="json") for legacy in inheritance.records],
            "budget": self.budget.snapshot(),
        }
        return _detached_json(checkpoint)

    @staticmethod
    def _dry_self_observation(world: WorldState, agent_id: str) -> dict[str, Any]:
        agent = world.agents[agent_id]
        observation = Observation(
            tick=world.tick,
            weather=world.weather.current,
            you=SelfObservation(
                hp=agent.hp,
                hunger=agent.hunger,
                age=agent.age,
                loc=agent.loc,
                neighbors=tuple(world.locations[agent.loc].neighbors),
                inventory=dict(agent.inventory),
            ),
            visible=[],
            events=[],
        )
        return observation.model_dump(mode="json")

    @staticmethod
    def _observation_event(
        agent_id: str,
        observation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if observation is None:
            raise RuntimeError("live agent is missing its current observation")
        return {
            "type": "observation",
            "payload": {
                "agent_id": agent_id,
                "observation": _detached_json(observation),
            },
        }

    async def _close_one_adapter(
        self,
        agent_id: str,
        *,
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        adapter = self._adapters.pop(agent_id, None)
        if adapter is None:
            return
        try:
            await asyncio.wait_for(
                adapter.close(),
                timeout=self.config.runtime.timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            if events is not None:
                events.append(
                    {
                        "type": "adapter_close_failed",
                        "payload": {"agent_id": agent_id},
                    }
                )

    async def _close_all_adapters(self) -> None:
        for agent_id in sorted(tuple(self._adapters)):
            await self._close_one_adapter(agent_id)

    def _ensure_open(self) -> None:
        if self._closed or self.store is None:
            raise RuntimeError("ExperimentRunner is closed")

    async def close(self) -> None:
        """Close every adapter, durable handle, and writer lock (idempotently)."""

        async with self._run_lock:
            if self._closed:
                return
            await self._close_all_adapters()
            if self.store is not None:
                self.store.close()
                self.store = None
            self._closed = True

    async def __aenter__(self) -> ExperimentRunner:
        self._ensure_open()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()


# Concise compatibility name used by architecture prose.
Orchestrator = ExperimentRunner


async def run_experiment(
    config: RunConfig,
    run_dir: str | Path,
    *,
    adapter_factory: AdapterFactory | None = None,
    max_ticks: int | None = None,
) -> RunSummary:
    """Open, run, and close one experiment in a single safe convenience call."""

    async with ExperimentRunner(config, run_dir, adapter_factory) as runner:
        return await runner.run(max_ticks=max_ticks)


__all__ = [
    "AdapterFactory",
    "ExperimentComplete",
    "ExperimentRunner",
    "LineageState",
    "Orchestrator",
    "RunSummary",
    "run_experiment",
]
