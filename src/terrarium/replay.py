"""Deterministic verification of committed world transitions.

Replay is deliberately narrower than recovery.  The event store first verifies
the durable hash chain and SQLite projection; this module then re-executes only
world mechanics from the recorded, already-validated actions.  Lifecycle work
(legacy selection and replacement agents) is represented by the following
committed checkpoint and becomes the boundary for the next mechanical step.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .config import RunConfig
from .domain import Action, WorldState
from .events import HASH_RE, json_sha256
from .manifest import RunManifest
from .measurement import iter_committed_events
from .storage import EVENT_LOG_NAME, EventStore, StorageError
from .world import WorldConfig as MechanicsWorldConfig
from .world import WorldEngine

MANIFEST_NAME = "manifest.json"
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_LIFECYCLE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class ReplayError(RuntimeError):
    """A sealed run cannot be reproduced exactly."""


class ReplayIntegrityError(ReplayError):
    """Committed replay records are missing, duplicated, or inconsistent."""


@dataclass(frozen=True, slots=True)
class ReplaySummary:
    run_id: str
    initial_tick: int
    last_tick: int
    ticks_replayed: int
    final_state_hash: str
    status: str = "ok"

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


def load_manifest_config(run_dir: str | Path) -> RunConfig:
    """Load and authenticate the pinned ``RunConfig`` from a run manifest.

    This intentionally checks the manifest's embedded configuration digest
    without rebuilding a host-dependent manifest (which would make historical
    replay depend on the machine doing the verification).
    """

    _manifest, config = _load_manifest_and_config(run_dir)
    return config


def _load_manifest_and_config(run_dir: str | Path) -> tuple[RunManifest, RunConfig]:
    root = _existing_run_directory(run_dir)
    try:
        raw = _read_manifest(root / MANIFEST_NAME)
        manifest = RunManifest.model_validate_json(raw)
        config = RunConfig.model_validate(manifest.config)
    except (OSError, UnicodeError, ValueError, ValidationError) as exc:
        raise ReplayIntegrityError("run manifest is invalid") from exc
    if config.digest() != manifest.config_sha256:
        raise ReplayIntegrityError("manifest configuration digest mismatch")
    return manifest, config


def mechanics_config(config: RunConfig) -> MechanicsWorldConfig:
    """Project the experiment config onto deterministic world constants."""

    try:
        return MechanicsWorldConfig(
            max_age=config.population.lifespan_ticks,
            hunger_per_tick=config.world.hunger_per_tick,
            starvation_damage=config.world.starvation_damage,
            starvation_lethal=config.world.starvation_lethal,
            poison_damage=config.world.poison_damage,
            collapse_probability=config.world.collapse_probability,
            collapse_lethal=config.world.collapse_lethal,
            rain_probability=config.world.rain_probability,
        )
    except ValidationError as exc:
        raise ReplayIntegrityError("manifest contains an invalid mechanics configuration") from exc


def replay_run(
    run_dir: str | Path,
    config: RunConfig | None = None,
) -> ReplaySummary:
    """Verify storage and deterministically reproduce every recorded world step.

    ``run_started`` establishes the initial checkpoint.  Every later committed
    transaction must contain exactly one ``world_step`` and one checkpoint.  A
    replayed intermediate state is checked against ``after_state_hash``; the
    committed checkpoint is then adopted as the next lifecycle boundary.
    """

    root = _existing_run_directory(run_dir)
    manifest, pinned = _load_manifest_and_config(root)
    try:
        manifest.assert_current_replay_environment()
    except RuntimeError as exc:
        raise ReplayIntegrityError("current replay environment differs from the manifest") from exc
    if config is not None and config.canonical_bytes() != pinned.canonical_bytes():
        raise ReplayIntegrityError("supplied configuration differs from the sealed manifest")
    config = pinned
    engine = WorldEngine(mechanics_config(config))

    try:
        with EventStore(
            root,
            config.run_id,
            max_raw_bytes=max(1, config.storage.max_raw_bytes),
        ) as store:
            before_report = store.verify()
            raw_events = list(iter_committed_events(root / EVENT_LOG_NAME))
            _check_read_matches_verification(raw_events, before_report)
            summary = _replay_events(
                raw_events,
                config=config,
                manifest_sha256=manifest.sha256(),
                engine=engine,
            )
            after_report = store.verify()
            if _verification_identity(before_report) != _verification_identity(after_report):
                raise ReplayIntegrityError("durable log changed while it was being replayed")
    except StorageError as exc:
        raise ReplayIntegrityError("event store integrity verification failed") from exc
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, ReplayError):
            raise
        raise ReplayIntegrityError("committed event stream is invalid") from exc
    return summary


def _replay_events(
    events: Sequence[Mapping[str, object]],
    *,
    config: RunConfig,
    manifest_sha256: str,
    engine: WorldEngine,
) -> ReplaySummary:
    by_tick: dict[int, list[Mapping[str, object]]] = {}
    for event in events:
        tick = event.get("tick")
        if type(tick) is not int or tick < 0:
            raise ReplayIntegrityError("committed event has an invalid tick")
        by_tick.setdefault(tick, []).append(event)
    if not by_tick:
        raise ReplayIntegrityError("run has no committed events")

    started = [event for event in events if event.get("type") == "run_started"]
    if len(started) != 1:
        raise ReplayIntegrityError("run must contain exactly one run_started event")
    start_event = started[0]
    start_tick = _event_tick(start_event, "run_started")
    if start_tick != min(by_tick):
        raise ReplayIntegrityError("run_started must occur in the first committed tick")
    initial_payload = _payload(start_event, "run_started")
    if set(initial_payload) != {"initial_world", "manifest_sha256"}:
        raise ReplayIntegrityError("run_started payload does not match the replay schema")
    if initial_payload["manifest_sha256"] != manifest_sha256:
        raise ReplayIntegrityError("run_started manifest digest mismatch")
    initial_world = _world_state(initial_payload["initial_world"], "run_started.initial_world")
    if initial_world.tick != start_tick:
        raise ReplayIntegrityError("initial world tick differs from run_started tick")

    first_checkpoint = _checkpoint_world(
        by_tick[start_tick], start_tick, config_sha256=config.digest()
    )
    if first_checkpoint.canonical_json() != initial_world.canonical_json():
        raise ReplayIntegrityError("initial world differs from its committed checkpoint")
    if any(event.get("type") == "world_step" for event in by_tick[start_tick]):
        raise ReplayIntegrityError("initial transaction must not contain a world_step")

    previous = first_checkpoint
    ticks_replayed = 0
    ordered_ticks = sorted(by_tick)
    if ordered_ticks != list(range(start_tick, ordered_ticks[-1] + 1)):
        raise ReplayIntegrityError("committed replay ticks are not contiguous")

    for tick in ordered_ticks[1:]:
        records = by_tick[tick]
        steps = [event for event in records if event.get("type") == "world_step"]
        if len(steps) != 1:
            raise ReplayIntegrityError(f"tick {tick} must contain exactly one world_step")
        step_payload = _payload(steps[0], f"world_step at tick {tick}")
        required = {"before_state_hash", "after_state_hash", "accepted_actions"}
        if set(step_payload) != required:
            raise ReplayIntegrityError(f"tick {tick} world_step payload does not match schema")
        before_hash = _digest(step_payload["before_state_hash"], "before_state_hash", tick)
        after_hash = _digest(step_payload["after_state_hash"], "after_state_hash", tick)
        if previous.state_hash() != before_hash:
            raise ReplayIntegrityError(f"tick {tick} before-state hash mismatch")
        actions = _accepted_actions(step_payload["accepted_actions"], previous, tick)
        result = engine.step(previous, actions)
        if result.state.tick != tick:
            raise ReplayIntegrityError(f"tick {tick} replay produced the wrong world tick")
        if result.state.state_hash() != after_hash:
            raise ReplayIntegrityError(f"tick {tick} after-state hash mismatch")

        outputs = _single_event(records, "world_outputs", tick)
        output_payload = _payload(outputs, f"world_outputs at tick {tick}")
        output_keys = {"events_sha256", "effects_sha256", "observations_sha256"}
        if set(output_payload) != output_keys:
            raise ReplayIntegrityError(f"tick {tick} world_outputs payload schema mismatch")
        result_json = result.model_dump(mode="json")
        expected_outputs = {
            "events_sha256": json_sha256(result_json["events"]),
            "effects_sha256": json_sha256(result_json["effects"]),
            "observations_sha256": json_sha256(result_json["observations"]),
        }
        if output_payload != expected_outputs:
            raise ReplayIntegrityError(f"tick {tick} world output hash mismatch")

        lifecycle = _single_event(records, "lifecycle_step", tick)
        lifecycle_payload = _payload(lifecycle, f"lifecycle_step at tick {tick}")
        lifecycle_keys = {"before_state_hash", "after_state_hash", "operations"}
        if set(lifecycle_payload) != lifecycle_keys:
            raise ReplayIntegrityError(f"tick {tick} lifecycle payload schema mismatch")
        lifecycle_before = _digest(
            lifecycle_payload["before_state_hash"], "lifecycle before_state_hash", tick
        )
        lifecycle_after = _digest(
            lifecycle_payload["after_state_hash"], "lifecycle after_state_hash", tick
        )
        if lifecycle_before != result.state.state_hash():
            raise ReplayIntegrityError(f"tick {tick} lifecycle before-state hash mismatch")
        lifecycle_state = _apply_lifecycle_operations(
            engine,
            result.state,
            lifecycle_payload["operations"],
            tick,
        )
        if lifecycle_state.state_hash() != lifecycle_after:
            raise ReplayIntegrityError(f"tick {tick} lifecycle after-state hash mismatch")

        checkpoint = _checkpoint_world(records, tick, config_sha256=config.digest())
        if checkpoint.tick != tick:
            raise ReplayIntegrityError(f"tick {tick} checkpoint embeds the wrong world tick")
        if checkpoint.canonical_json() != lifecycle_state.canonical_json():
            raise ReplayIntegrityError(
                f"tick {tick} checkpoint world differs from deterministic lifecycle result"
            )
        previous = lifecycle_state
        ticks_replayed += 1

    return ReplaySummary(
        run_id=config.run_id,
        initial_tick=start_tick,
        last_tick=ordered_ticks[-1],
        ticks_replayed=ticks_replayed,
        final_state_hash=previous.state_hash(),
    )


def _accepted_actions(
    value: object,
    state: WorldState,
    tick: int,
) -> dict[str, Action]:
    if not isinstance(value, list):
        raise ReplayIntegrityError(f"tick {tick} accepted_actions must be a list")
    actions: list[Action] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ReplayIntegrityError(f"tick {tick} accepted_actions[{index}] must be an object")
        try:
            actions.append(Action.model_validate(item))
        except ValidationError as exc:
            raise ReplayIntegrityError(f"tick {tick} accepted_actions[{index}] is invalid") from exc

    agent_ids = [action.agent_id for action in actions]
    if agent_ids != sorted(agent_ids) or len(agent_ids) != len(set(agent_ids)):
        raise ReplayIntegrityError(f"tick {tick} accepted_actions are not uniquely sorted")
    living_ids = sorted(agent_id for agent_id, agent in state.agents.items() if agent.alive)
    if agent_ids != living_ids:
        raise ReplayIntegrityError(
            f"tick {tick} accepted_actions do not cover every living agent exactly once"
        )
    return {action.agent_id: action for action in actions}


def _apply_lifecycle_operations(
    engine: WorldEngine,
    state: WorldState,
    value: object,
    tick: int,
) -> WorldState:
    if not isinstance(value, list):
        raise ReplayIntegrityError(f"tick {tick} lifecycle operations must be an array")
    current = state
    for position, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise ReplayIntegrityError(
                f"tick {tick} lifecycle operation {position} must be an object"
            )
        operation_type = raw.get("type")
        if operation_type == "rng_draw":
            required = {"type", "purpose", "upper", "index", "subject"}
            if set(raw) != required:
                raise ReplayIntegrityError(
                    f"tick {tick} lifecycle RNG operation {position} schema mismatch"
                )
            purpose = _lifecycle_token(raw["purpose"], "purpose", tick, position)
            subject = _lifecycle_token(raw["subject"], "subject", tick, position)
            if not purpose or not subject:  # explicit for type narrowing and audit clarity
                raise ReplayIntegrityError(f"tick {tick} lifecycle metadata is empty")
            upper = raw["upper"]
            index = raw["index"]
            if type(upper) is not int or not 1 <= upper <= 1_000_000:
                raise ReplayIntegrityError(f"tick {tick} lifecycle RNG upper is invalid")
            if type(index) is not int or not 0 <= index < upper:
                raise ReplayIntegrityError(f"tick {tick} lifecycle RNG index is invalid")
            current, replayed_index = engine.rng_draw_index(current, upper)
            if replayed_index != index:
                raise ReplayIntegrityError(f"tick {tick} lifecycle RNG draw mismatch")
            continue
        if operation_type == "spawn":
            required = {
                "type",
                "agent_id",
                "generation",
                "inherited_legacy_ids",
                "location",
            }
            if set(raw) != required:
                raise ReplayIntegrityError(
                    f"tick {tick} lifecycle spawn operation {position} schema mismatch"
                )
            agent_id = _lifecycle_token(raw["agent_id"], "agent_id", tick, position)
            location = raw["location"]
            generation = raw["generation"]
            inherited = raw["inherited_legacy_ids"]
            if not isinstance(location, str):
                raise ReplayIntegrityError(f"tick {tick} lifecycle spawn location is invalid")
            if type(generation) is not int or generation < 0:
                raise ReplayIntegrityError(f"tick {tick} lifecycle spawn generation is invalid")
            if not isinstance(inherited, list) or not all(
                isinstance(item, str) for item in inherited
            ):
                raise ReplayIntegrityError(
                    f"tick {tick} lifecycle inherited legacy IDs are invalid"
                )
            current, _spawned_event = engine.spawn_agent(
                current,
                agent_id,
                generation,
                inherited_legacy_ids=inherited,
                location=location,
            )
            continue
        raise ReplayIntegrityError(
            f"tick {tick} lifecycle operation {position} has an unknown type"
        )
    return current


def _lifecycle_token(value: object, name: str, tick: int, position: int) -> str:
    if not isinstance(value, str) or _LIFECYCLE_TOKEN.fullmatch(value) is None:
        raise ReplayIntegrityError(f"tick {tick} lifecycle operation {position} {name} is invalid")
    return value


def _single_event(
    events: Sequence[Mapping[str, object]], event_type: str, tick: int
) -> Mapping[str, object]:
    matches = [event for event in events if event.get("type") == event_type]
    if len(matches) != 1:
        raise ReplayIntegrityError(f"tick {tick} must contain exactly one {event_type}")
    return matches[0]


def _checkpoint_world(
    events: Sequence[Mapping[str, object]],
    tick: int,
    *,
    config_sha256: str,
) -> WorldState:
    checkpoints = [event for event in events if event.get("type") == "state_checkpoint"]
    if len(checkpoints) != 1:
        raise ReplayIntegrityError(f"tick {tick} must contain exactly one state_checkpoint")
    payload = _payload(checkpoints[0], f"state_checkpoint at tick {tick}")
    if set(payload) != {"state", "rng_state", "state_hash"}:
        raise ReplayIntegrityError(f"tick {tick} checkpoint payload does not match schema")
    state = payload["state"]
    if not isinstance(state, Mapping):
        raise ReplayIntegrityError(f"tick {tick} checkpoint state must be an object")
    checkpoint_keys = {
        "schema_version",
        "config_sha256",
        "world",
        "rng_state",
        "contexts",
        "lineages",
        "legacies",
        "budget",
    }
    if set(state) != checkpoint_keys:
        raise ReplayIntegrityError(f"tick {tick} orchestrator checkpoint schema mismatch")
    if state["schema_version"] != 2 or type(state["schema_version"]) is not int:
        raise ReplayIntegrityError(f"tick {tick} checkpoint schema version is invalid")
    if state["config_sha256"] != config_sha256:
        raise ReplayIntegrityError(f"tick {tick} checkpoint configuration digest mismatch")
    if not isinstance(state["contexts"], Mapping):
        raise ReplayIntegrityError(f"tick {tick} checkpoint contexts must be an object")
    if not isinstance(state["lineages"], Mapping):
        raise ReplayIntegrityError(f"tick {tick} checkpoint lineages must be an object")
    if not isinstance(state["legacies"], list):
        raise ReplayIntegrityError(f"tick {tick} checkpoint legacies must be an array")
    if not isinstance(state["budget"], Mapping):
        raise ReplayIntegrityError(f"tick {tick} checkpoint budget must be an object")
    world = _world_state(state["world"], f"state_checkpoint at tick {tick}")
    digest = _digest(payload["state_hash"], "checkpoint state_hash", tick)
    if json_sha256(dict(state)) != digest:
        raise ReplayIntegrityError(f"tick {tick} checkpoint state hash mismatch")
    world_rng = world.rng_state.model_dump(mode="json")
    if state["rng_state"] != world_rng or payload["rng_state"] != world_rng:
        raise ReplayIntegrityError(f"tick {tick} checkpoint RNG state mismatch")
    return world


def _world_state(value: object, name: str) -> WorldState:
    if not isinstance(value, Mapping):
        raise ReplayIntegrityError(f"{name} must be an object")
    try:
        return WorldState.model_validate(value)
    except ValidationError as exc:
        raise ReplayIntegrityError(f"{name} is not a valid world state") from exc


def _payload(event: Mapping[str, object], name: str) -> Mapping[str, object]:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise ReplayIntegrityError(f"{name} payload must be an object")
    return payload


def _event_tick(event: Mapping[str, object], name: str) -> int:
    tick = event.get("tick")
    if type(tick) is not int or tick < 0:
        raise ReplayIntegrityError(f"{name} has an invalid tick")
    return tick


def _digest(value: object, name: str, tick: int) -> str:
    if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
        raise ReplayIntegrityError(f"tick {tick} {name} is not a SHA-256 digest")
    return value


def _check_read_matches_verification(
    events: Sequence[Mapping[str, object]], report: Mapping[str, Any]
) -> None:
    if len(events) != report.get("committed_events"):
        raise ReplayIntegrityError("event read does not match verified committed length")
    if events:
        last = events[-1]
        if last.get("seq") != report.get("last_seq") or last.get("hash") != report.get("last_hash"):
            raise ReplayIntegrityError("event read does not match verified log identity")


def _verification_identity(report: Mapping[str, Any]) -> tuple[object, ...]:
    return (
        report.get("committed_ticks"),
        report.get("committed_events"),
        report.get("last_tick"),
        report.get("last_seq"),
        report.get("last_hash"),
    )


def _existing_run_directory(path: str | Path) -> Path:
    candidate = Path(path).absolute()
    try:
        info = candidate.lstat()
    except FileNotFoundError as exc:
        raise ReplayIntegrityError("run directory does not exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ReplayIntegrityError("run path is not a real directory")
    return candidate.resolve(strict=True)


def _read_manifest(path: Path) -> bytes:
    flags = os.O_RDONLY | int(getattr(os, "O_CLOEXEC", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise ReplayIntegrityError("run manifest is missing") from exc
    except OSError as exc:
        raise ReplayIntegrityError("run manifest cannot be opened safely") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ReplayIntegrityError("run manifest is not a regular file")
        if info.st_nlink != 1:
            raise ReplayIntegrityError("run manifest must not be multiply linked")
        if info.st_size > MAX_MANIFEST_BYTES:
            raise ReplayIntegrityError("run manifest exceeds the safety limit")
        chunks: list[bytes] = []
        remaining = MAX_MANIFEST_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ReplayIntegrityError("run manifest exceeds the safety limit")
        return raw
    finally:
        os.close(fd)


__all__ = [
    "ReplayError",
    "ReplayIntegrityError",
    "ReplaySummary",
    "load_manifest_config",
    "mechanics_config",
    "replay_run",
]
