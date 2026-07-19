"""Read-only MVP knowledge-survival and behavioral-adoption measurements.

This baseline is deliberately offline and deterministic.  It exposes a
classifier protocol so an embedding+NLI implementation can replace the lexical
baseline without changing the world or its logs.

Textual survival alone cannot distinguish a lineage that *repeats* a rule from
one that *obeys* it.  The behavioral curve therefore reads the same committed
event log and reports, per generation, how often agents performed the risky
acts the hidden rules punish: eating a red berry inside the post-rain poison
window and digging past the collapse depth.  Falling risk rates across
generations alongside surviving legacy text is the selection signal the MVP
gate asks for; surviving text with flat risk rates is parroting.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import stat
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .config import KnowledgeConfig
from .domain import WeatherState
from .events import strict_json_loads

_MAX_LEGACY_TEXT_CHARS = 1_000_000
LEXICAL_BASELINE_ID = "lexical-keyword-baseline-v1"

# Mirrors of the engine's hidden-rule constants used only for read-only replayed
# classification of committed events; changing WorldEngine semantics requires a
# schema version and a matching update here.
DEFAULT_RAIN_WINDOW_TICKS = 3
DEFAULT_SAFE_DIG_DEPTH = 3


class Stance(StrEnum):
    ENTAILS = "entails"
    CONTRADICTS = "contradicts"
    SILENT = "silent"


class MeasurementBasis(StrEnum):
    """Which cultural population a curve point describes."""

    AUTHORED = "authored"
    INHERITED = "inherited"


class KnowledgeClassifier(Protocol):
    def classify(self, text: str, fact: KnowledgeConfig) -> tuple[Stance, float]: ...


_TOKEN = re.compile(r"[\w'-]+", re.UNICODE)
_NEGATION = {"not", "never", "no", "isn't", "aren't", "не", "нет", "никогда"}


class LexicalKnowledgeClassifier:
    """Auditable baseline; not a substitute for the planned NLI model."""

    identifier = LEXICAL_BASELINE_ID

    def __init__(self, entail_threshold: float = 0.75) -> None:
        if not 0.0 < entail_threshold <= 1.0:
            raise ValueError("entail_threshold must be in (0, 1]")
        self.entail_threshold = entail_threshold

    def classify(self, text: str, fact: KnowledgeConfig) -> tuple[Stance, float]:
        tokens = {token.casefold() for token in _TOKEN.findall(text)}
        keywords = {token.casefold() for token in fact.keywords}
        score = len(tokens & keywords) / len(keywords)
        if score < self.entail_threshold:
            return Stance.SILENT, score
        stance = Stance.CONTRADICTS if tokens & _NEGATION else Stance.ENTAILS
        return stance, score


@dataclass(frozen=True, slots=True)
class SurvivalPoint:
    fact_id: str
    kind: str
    basis: str
    generation: int
    channel: str
    total_legacies: int
    entails: int
    contradicts: int
    silent: int
    survival_rate: float


@dataclass(frozen=True, slots=True)
class BehaviorPoint:
    """Per-generation realized behavior against the two testable hidden rules.

    ``risky_berry_eats`` counts red berries eaten while the tick-start weather
    satisfied the engine's post-rain poison window; ``deep_digs`` counts digs
    past the safe depth.  Rates divide by that generation's own attempts, so a
    generation that never eats berries reports a rate of 0.0 with a zero
    denominator visible in the counts.
    """

    generation: int
    agents: int
    berry_eats: int
    risky_berry_eats: int
    risky_eat_rate: float
    poison_damage_events: int
    digs: int
    deep_digs: int
    deep_dig_rate: float
    collapse_events: int


def iter_committed_events(path: str | Path) -> Iterable[dict[str, object]]:
    """Yield envelope dictionaries after basic bounds/type checks.

    Cryptographic chain verification remains the EventStore's responsibility;
    callers should run `terrarium verify` first.
    """

    event_path = Path(path)
    pending: list[dict[str, object]] = []
    pending_tick: int | None = None
    flags = (
        os.O_RDONLY
        | int(getattr(os, "O_CLOEXEC", 0))
        | int(getattr(os, "O_NOFOLLOW", 0))
        | int(getattr(os, "O_NONBLOCK", 0))
    )
    fd = os.open(event_path, flags)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise ValueError("event log must be a private regular file")
    with os.fdopen(fd, "rb") as handle:
        for line_no, line in enumerate(handle, start=1):
            if len(line) > 16 * 1024 * 1024:
                raise ValueError(f"event line {line_no} exceeds safety limit")
            if not line.endswith(b"\n"):
                break  # only an incomplete final tail is ignored
            item = strict_json_loads(line)
            if not isinstance(item, dict):
                raise ValueError(f"event line {line_no} is not an object")
            event_type = item.get("type")
            event_tick = item.get("tick")
            if event_type == "tick_begin":
                # A new begin after a fully written but uncommitted batch means
                # the prior batch is not durable.  Keep only the newest batch;
                # EventStore.verify remains responsible for detecting invalid
                # history rather than this read-only projection helper.
                pending = [item]
                pending_tick = event_tick if isinstance(event_tick, int) else None
                continue
            if not pending:
                continue
            if event_tick != pending_tick:
                pending = []
                pending_tick = None
                continue
            pending.append(item)
            if event_type == "tick_commit":
                yield from pending
                pending = []
                pending_tick = None


def extract_legacies(events: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for event in events:
        event_type = event.get("type")
        if event_type not in {"legacy_created", "legacy", "legacy_written"}:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        legacy_id = payload.get("legacy_id" if event_type == "legacy_written" else "id")
        generation = payload.get("generation")
        channel = payload.get("channel")
        text = payload.get("text")
        if (
            not isinstance(legacy_id, str)
            or not 1 <= len(legacy_id) <= 96
            or type(generation) is not int
            or generation < 0
            or channel not in {"written", "oral"}
            or not isinstance(text, str)
            or not 1 <= len(text) <= _MAX_LEGACY_TEXT_CHARS
        ):
            continue
        records.append(
            {
                "id": legacy_id,
                "generation": generation,
                "channel": channel,
                "text": text,
            }
        )
    return records


def extract_inherited_exposures(
    events: Iterable[Mapping[str, object]],
    legacies: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Join spawn-time legacy IDs to immutable texts.

    An authored legacy is indexed by the generation which wrote it.  An inherited
    exposure is instead indexed by the generation of the receiving agent.  This
    distinction makes the final live cohort measurable even though it has not yet
    reached its own deathbed.

    The input event stream has already passed :class:`EventStore` verification in
    CLI use.  Unknown or duplicate inherited IDs are nevertheless rejected here so
    a malformed semantic projection cannot silently under-count cultural exposure.
    """

    by_id: dict[str, Mapping[str, object]] = {}
    for legacy in legacies:
        legacy_id = legacy.get("id")
        if not isinstance(legacy_id, str):
            continue
        previous = by_id.get(legacy_id)
        if previous is not None and dict(previous) != dict(legacy):
            raise ValueError("legacy ID has conflicting measurement records")
        by_id[legacy_id] = legacy

    exposures: list[dict[str, object]] = []
    spawned_agents: set[str] = set()
    for event in events:
        if event.get("type") != "agent_spawned":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("agent_spawned payload must be an object")
        agent_id = payload.get("agent_id")
        generation = payload.get("generation")
        inherited_ids = payload.get("inherited_legacy_ids")
        if (
            not isinstance(agent_id, str)
            or not 1 <= len(agent_id) <= 96
            or type(generation) is not int
            or generation < 0
            or not isinstance(inherited_ids, list)
            or any(not isinstance(item, str) for item in inherited_ids)
            or len(inherited_ids) != len(set(inherited_ids))
        ):
            raise ValueError("agent_spawned inheritance fields are invalid")
        if agent_id in spawned_agents:
            raise ValueError("agent_spawned redefines an existing agent")
        spawned_agents.add(agent_id)
        lineage_id = payload.get("lineage_id")
        if lineage_id is not None and not isinstance(lineage_id, str):
            raise ValueError("agent_spawned lineage_id must be a string")

        for legacy_id in inherited_ids:
            legacy = by_id.get(legacy_id)
            if legacy is None:
                raise ValueError("agent_spawned references an unknown legacy")
            channel = legacy.get("channel")
            text = legacy.get("text")
            if channel not in {"written", "oral"} or not isinstance(text, str):
                raise ValueError("inherited legacy is not measurable")
            exposure: dict[str, object] = {
                "id": f"{agent_id}:{legacy_id}",
                "legacy_id": legacy_id,
                "agent_id": agent_id,
                "generation": generation,
                "channel": channel,
                "text": text,
                "basis": MeasurementBasis.INHERITED.value,
            }
            if lineage_id is not None:
                exposure["lineage_id"] = lineage_id
            exposures.append(exposure)
    return exposures


def behavior_adoption_curve(
    events: Iterable[Mapping[str, object]],
    *,
    rain_window_ticks: int = DEFAULT_RAIN_WINDOW_TICKS,
    safe_dig_depth: int = DEFAULT_SAFE_DIG_DEPTH,
    expected_generations: Iterable[int] | None = None,
) -> list[BehaviorPoint]:
    """Compute per-generation risk-taking rates from committed event envelopes.

    The stream must contain the run's ``agent_spawned`` and ``state_checkpoint``
    envelopes: generations come from spawn events and the poison window for an
    ``ate`` envelope at tick N is judged against the tick-start weather, i.e.
    the checkpoint committed for tick N-1.  Like the inheritance extractor,
    this is fail-closed: an actor without a spawn record or an eat without a
    prior weather checkpoint raises instead of silently under-counting risk.
    """

    if isinstance(rain_window_ticks, bool) or type(rain_window_ticks) is not int:
        raise ValueError("rain_window_ticks must be an integer")
    if rain_window_ticks < 1:
        raise ValueError("rain_window_ticks must be positive")
    if isinstance(safe_dig_depth, bool) or type(safe_dig_depth) is not int:
        raise ValueError("safe_dig_depth must be an integer")
    if safe_dig_depth < 0:
        raise ValueError("safe_dig_depth cannot be negative")

    generation_of: dict[str, int] = {}
    agents_per_generation: dict[int, int] = defaultdict(int)
    weather_by_tick: dict[int, WeatherState] = {}
    counters: dict[int, dict[str, int]] = defaultdict(
        lambda: {
            "berry_eats": 0,
            "risky_berry_eats": 0,
            "poison_damage_events": 0,
            "digs": 0,
            "deep_digs": 0,
            "collapse_events": 0,
        }
    )

    def actor_generation(payload: Mapping[str, object], event_type: str) -> int:
        agent_id = payload.get("agent_id")
        if not isinstance(agent_id, str):
            raise ValueError(f"{event_type} payload has no valid agent_id")
        generation = generation_of.get(agent_id)
        if generation is None:
            raise ValueError(f"{event_type} references an agent without a spawn record")
        return generation

    for event in events:
        event_type = event.get("type")
        payload = event.get("payload")
        if event_type == "agent_spawned":
            if not isinstance(payload, Mapping):
                raise ValueError("agent_spawned payload must be an object")
            agent_id = payload.get("agent_id")
            generation = payload.get("generation")
            if (
                not isinstance(agent_id, str)
                or not 1 <= len(agent_id) <= 96
                or type(generation) is not int
                or generation < 0
            ):
                raise ValueError("agent_spawned identity fields are invalid")
            if agent_id in generation_of:
                raise ValueError("agent_spawned redefines an existing agent")
            generation_of[agent_id] = generation
            agents_per_generation[generation] += 1
            continue

        if event_type == "state_checkpoint":
            tick = event.get("tick")
            if type(tick) is not int or tick < 0:
                raise ValueError("state_checkpoint envelope has no valid tick")
            if not isinstance(payload, Mapping):
                raise ValueError("state_checkpoint payload must be an object")
            state = payload.get("state")
            world = state.get("world") if isinstance(state, Mapping) else None
            weather = world.get("weather") if isinstance(world, Mapping) else None
            if weather is None:
                raise ValueError("state_checkpoint does not contain world weather")
            weather_by_tick[tick] = WeatherState.model_validate(weather)
            continue

        if not isinstance(payload, Mapping):
            continue

        if event_type == "ate":
            if payload.get("item") != "red_berry":
                continue
            generation = actor_generation(payload, "ate")
            tick = event.get("tick")
            if type(tick) is not int or tick < 1:
                raise ValueError("ate envelope has no valid tick")
            tick_start_weather = weather_by_tick.get(tick - 1)
            if tick_start_weather is None:
                raise ValueError("ate event precedes any tick-start weather checkpoint")
            counters[generation]["berry_eats"] += 1
            if tick_start_weather.rained_last(rain_window_ticks):
                counters[generation]["risky_berry_eats"] += 1
        elif event_type == "dug":
            generation = actor_generation(payload, "dug")
            depth = payload.get("depth")
            if type(depth) is not int or depth < 1:
                raise ValueError("dug payload has no valid depth")
            counters[generation]["digs"] += 1
            if depth > safe_dig_depth:
                counters[generation]["deep_digs"] += 1
        elif event_type == "damage":
            if payload.get("cause") == "poison":
                counters[actor_generation(payload, "damage")]["poison_damage_events"] += 1
        elif event_type == "tunnel_collapsed":
            counters[actor_generation(payload, "tunnel_collapsed")]["collapse_events"] += 1

    generations = _expected_generations(expected_generations)
    generations.update(agents_per_generation)
    generations.update(counters)

    points: list[BehaviorPoint] = []
    for generation in sorted(generations):
        values = counters.get(generation)
        if values is None:
            values = {
                "berry_eats": 0,
                "risky_berry_eats": 0,
                "poison_damage_events": 0,
                "digs": 0,
                "deep_digs": 0,
                "collapse_events": 0,
            }
        berry_eats = values["berry_eats"]
        digs = values["digs"]
        points.append(
            BehaviorPoint(
                generation=generation,
                agents=agents_per_generation.get(generation, 0),
                berry_eats=berry_eats,
                risky_berry_eats=values["risky_berry_eats"],
                risky_eat_rate=values["risky_berry_eats"] / berry_eats if berry_eats else 0.0,
                poison_damage_events=values["poison_damage_events"],
                digs=digs,
                deep_digs=values["deep_digs"],
                deep_dig_rate=values["deep_digs"] / digs if digs else 0.0,
                collapse_events=values["collapse_events"],
            )
        )
    return points


def authored_measurement_records(
    legacies: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Return detached authored records with an explicit measurement basis."""

    return [
        {**dict(legacy), "basis": MeasurementBasis.AUTHORED.value}
        for legacy in legacies
    ]


def knowledge_survival_curve(
    records: Iterable[Mapping[str, object]],
    facts: Iterable[KnowledgeConfig],
    classifier: KnowledgeClassifier,
    *,
    expected_generations: Iterable[int] | None = None,
    expected_channels: Iterable[str] | None = None,
    expected_bases: Iterable[str | MeasurementBasis] | None = None,
) -> list[SurvivalPoint]:
    """Classify authored texts and recipient exposures on a common grid.

    Callers may supply the expected generation/channel/basis dimensions.  Their
    Cartesian product is emitted even when no records exist, producing explicit
    zero-coverage rows instead of silently dropping a failed or still-live cohort.
    Records without a ``basis`` retain the historical ``authored`` interpretation.
    """

    grouped: dict[tuple[str, int, str], list[Mapping[str, object]]] = defaultdict(list)
    for record in records:
        generation = record.get("generation")
        channel = record.get("channel")
        text = record.get("text")
        basis = record.get("basis", MeasurementBasis.AUTHORED.value)
        if isinstance(basis, MeasurementBasis):
            basis = basis.value
        if (
            type(generation) is int
            and generation >= 0
            and channel in {"written", "oral"}
            and isinstance(text, str)
            and basis in {item.value for item in MeasurementBasis}
        ):
            grouped[(basis, generation, channel)].append(record)

    generations = _expected_generations(expected_generations)
    generations.update(key[1] for key in grouped)
    channels = _expected_channels(expected_channels)
    channels.update(key[2] for key in grouped)
    bases = _expected_bases(expected_bases)
    bases.update(key[0] for key in grouped)

    points: list[SurvivalPoint] = []
    for fact in facts:
        for basis in sorted(bases):
            for generation in sorted(generations):
                for channel in sorted(channels):
                    samples = grouped.get((basis, generation, channel), [])
                    counts = {stance: 0 for stance in Stance}
                    for record in samples:
                        stance, _score = classifier.classify(str(record["text"]), fact)
                        counts[stance] += 1
                    total = len(samples)
                    points.append(
                        SurvivalPoint(
                            fact_id=fact.id,
                            kind=fact.kind,
                            basis=basis,
                            generation=generation,
                            channel=channel,
                            total_legacies=total,
                            entails=counts[Stance.ENTAILS],
                            contradicts=counts[Stance.CONTRADICTS],
                            silent=counts[Stance.SILENT],
                            survival_rate=counts[Stance.ENTAILS] / total if total else 0.0,
                        )
                    )
    return points


def _expected_generations(values: Iterable[int] | None) -> set[int]:
    result: set[int] = set()
    for value in values or ():
        if type(value) is not int or value < 0:
            raise ValueError("expected generations must be non-negative integers")
        result.add(value)
    return result


def _expected_channels(values: Iterable[str] | None) -> set[str]:
    result: set[str] = set()
    for value in values or ():
        if value not in {"written", "oral"}:
            raise ValueError("expected channels must be written or oral")
        result.add(value)
    return result


def _expected_bases(values: Iterable[str | MeasurementBasis] | None) -> set[str]:
    result: set[str] = set()
    for value in values or ():
        try:
            basis = MeasurementBasis(value)
        except ValueError as exc:
            raise ValueError("expected basis must be authored or inherited") from exc
        result.add(basis.value)
    return result


def write_measurements(points: Iterable[SurvivalPoint], output_dir: str | Path) -> None:
    """Write to a separate output tree; never mutate a run directory."""

    columns = list(SurvivalPoint.__dataclass_fields__)
    _write_export_pair(points, columns, output_dir, "knowledge-survival")


def write_behavior_measurements(points: Iterable[BehaviorPoint], output_dir: str | Path) -> None:
    """Write the behavioral adoption curve next to the knowledge survival export."""

    columns = list(BehaviorPoint.__dataclass_fields__)
    _write_export_pair(points, columns, output_dir, "behavior-adoption")


def _write_export_pair(
    points: Iterable[SurvivalPoint | BehaviorPoint],
    columns: list[str],
    output_dir: str | Path,
    base_name: str,
) -> None:
    destination = Path(output_dir).absolute()
    rows = [asdict(point) for point in points]
    json_data = (
        json.dumps(rows, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    csv_data = csv_buffer.getvalue().encode("utf-8")

    directory_fd = _open_output_directory(destination)
    try:
        # JSON is the commit marker for a matched export pair.
        _atomic_write(directory_fd, f"{base_name}.csv", csv_data)
        _atomic_write(directory_fd, f"{base_name}.json", json_data)
    finally:
        os.close(directory_fd)


def _open_output_directory(destination: Path) -> int:
    destination.mkdir(mode=0o755, parents=True, exist_ok=True)
    info = destination.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("measurement output must be a real directory")
    flags = (
        os.O_RDONLY
        | int(getattr(os, "O_DIRECTORY", 0))
        | int(getattr(os, "O_CLOEXEC", 0))
        | int(getattr(os, "O_NOFOLLOW", 0))
    )
    directory_fd = os.open(destination, flags)
    if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
        os.close(directory_fd)
        raise ValueError("measurement output must be a real directory")
    return directory_fd


def _atomic_write(directory_fd: int, name: str, data: bytes) -> None:
    temporary = f".{name}.{secrets.token_hex(16)}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | int(getattr(os, "O_CLOEXEC", 0))
        | int(getattr(os, "O_NOFOLLOW", 0))
    )
    fd = os.open(temporary, flags, 0o644, dir_fd=directory_fd)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(fd)
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temporary = ""
        os.fsync(directory_fd)
    finally:
        os.close(fd)
        if temporary:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
