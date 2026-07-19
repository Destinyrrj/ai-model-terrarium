"""Read-only SQLite queries used by the scientific GUI.

SQLite is a replaceable projection of the authoritative JSONL log.  Every
connection uses URI ``mode=ro`` and ``query_only``; this module never opens an
``EventStore`` and cannot acquire the single-writer lock.
"""

from __future__ import annotations

import json
import sqlite3
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import asdict
from pathlib import Path
from typing import TypeVar

from terrarium.events import HASH_RE, EventValidationError, strict_json_loads
from terrarium.measurement import (
    LexicalKnowledgeClassifier,
    MeasurementBasis,
    authored_measurement_records,
    behavior_adoption_curve,
    extract_inherited_exposures,
    extract_legacies,
    knowledge_survival_curve,
)
from terrarium.replay import ReplayError, load_manifest_config
from terrarium.storage import SQLITE_NAME

from .sanitize import (
    EVENT_FIELD_POLICIES,
    MAX_LEGACY_TEXT,
    project_event,
    sanitize_display_text,
)
from .sqlite_snapshot import open_projection_snapshot

T = TypeVar("T")
SQLITE_INT_MAX = (1 << 63) - 1
_DISPLAY_EVENT_TYPES = tuple(sorted(EVENT_FIELD_POLICIES))
_DISPLAY_EVENT_TYPES_JSON = json.dumps(_DISPLAY_EVENT_TYPES, separators=(",", ":"))
_TOKEN_EVENT_TYPES_JSON = json.dumps(
    ("model_usage", "token_usage", "usage"),
    separators=(",", ":"),
)


class ReadModelError(RuntimeError):
    """Base class for safe projection-read failures."""


class ReadModelUnavailable(ReadModelError):
    """The SQLite projection is absent, busy, corrupt, or temporarily unreadable."""


class ReadModelDataError(ReadModelError):
    """A projected JSON value does not satisfy the read-model contract."""


class ReadModel:
    """Query one run's SQLite projection without creating any run artifacts."""

    def __init__(
        self,
        run_dir: str | Path,
        *,
        busy_retries: int = 3,
        retry_delay: float = 0.03,
    ) -> None:
        if type(busy_retries) is not int or busy_retries < 0:
            raise ValueError("busy_retries must be a non-negative integer")
        if retry_delay < 0:
            raise ValueError("retry_delay must be non-negative")
        self.run_dir = Path(run_dir).absolute()
        self.sqlite_path = self.run_dir / SQLITE_NAME
        self.busy_retries = busy_retries
        self.retry_delay = retry_delay

    def events(self, *, after_seq: int = -1, limit: int = 500) -> list[dict[str, object]]:
        if type(after_seq) is not int or not -1 <= after_seq <= SQLITE_INT_MAX:
            raise ValueError("after_seq must fit SQLite's signed integer range")
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        rows = self._rows(
            """
            SELECT schema_version, run_id, seq, tick, type, payload_json, prev_hash, hash
            FROM events
            WHERE seq > ? AND type IN (SELECT value FROM json_each(?))
            ORDER BY seq LIMIT ?
            """,
            (after_seq, _DISPLAY_EVENT_TYPES_JSON, limit),
        )
        return self._project_event_rows(rows)

    def events_before(
        self, *, before_seq: int, limit: int = 500
    ) -> list[dict[str, object]]:
        """Return the nearest display events before a sparse sequence cursor."""

        if type(before_seq) is not int or not 0 <= before_seq <= SQLITE_INT_MAX:
            raise ValueError("before_seq must fit SQLite's signed integer range")
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        rows = self._rows(
            """
            SELECT schema_version, run_id, seq, tick, type, payload_json, prev_hash, hash
            FROM events
            WHERE seq < ? AND type IN (SELECT value FROM json_each(?))
            ORDER BY seq DESC LIMIT ?
            """,
            (before_seq, _DISPLAY_EVENT_TYPES_JSON, limit),
        )
        rows.reverse()
        return self._project_event_rows(rows)

    def latest_events(self, *, limit: int = 500) -> list[dict[str, object]]:
        """Return the newest projected events in ascending sequence order."""

        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        rows = self._rows(
            """
            SELECT schema_version, run_id, seq, tick, type, payload_json, prev_hash, hash
            FROM events
            WHERE type IN (SELECT value FROM json_each(?))
            ORDER BY seq DESC LIMIT ?
            """,
            (_DISPLAY_EVENT_TYPES_JSON, limit),
        )
        rows.reverse()
        return self._project_event_rows(rows)

    def event_head(self) -> tuple[int | None, str | None]:
        """Return the replaceable projection's raw durable identity."""

        rows = self._rows("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1")
        if not rows:
            return None, None
        seq, event_hash = rows[0]
        if (
            type(seq) is not int
            or seq < 0
            or not isinstance(event_hash, str)
            or HASH_RE.fullmatch(event_hash) is None
        ):
            raise ReadModelDataError("event projection head is invalid")
        return seq, event_hash

    @staticmethod
    def _project_event_rows(rows: Sequence[sqlite3.Row]) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for row in rows:
            payload = _decode_object(row[5], context="event payload")
            projected = project_event(
                {
                    "schema_version": row[0],
                    "run_id": row[1],
                    "seq": row[2],
                    "tick": row[3],
                    "type": row[4],
                    "payload": payload,
                    "prev_hash": row[6],
                    "hash": row[7],
                }
            )
            if projected is None:
                raise ReadModelDataError("allowlisted event projection is invalid")
            result.append(projected)
        return result

    def list_agents(
        self,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        if type(offset) is not int or not 0 <= offset <= SQLITE_INT_MAX:
            raise ValueError("offset must fit SQLite's signed integer range")
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("limit must be a positive integer or None")
        sql = """
            SELECT a.agent_id, a.generation_id, a.model, a.valley,
                   a.born_tick, a.died_tick, a.data_json,
                   (
                       SELECT d.cause FROM deaths AS d
                       WHERE d.agent_id = a.agent_id
                       ORDER BY d.tick DESC, d.event_seq DESC LIMIT 1
                   ) AS death_cause
            FROM agents AS a ORDER BY a.created_event_seq
            """
        parameters: Sequence[object] = ()
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            parameters = (limit, offset)
        rows = self._rows(sql, parameters)
        result: list[dict[str, object]] = []
        for row in rows:
            payload = _decode_object(row[6], context="agent payload")
            item: dict[str, object] = {
                "agent_id": _safe_text(row[0]),
                "generation_id": _safe_optional(row[1]),
                "model": _safe_optional(row[2]),
                "valley": _safe_optional(row[3]),
                "born_tick": row[4],
                "died_tick": row[5],
            }
            cause = _safe_optional(row[7])
            if cause is not None:
                item["cause"] = cause
            _copy_int(payload, item, "generation")
            _copy_text(payload, item, "lineage_id")
            _copy_text(payload, item, "location")
            inherited = _safe_token_list(payload.get("inherited_legacy_ids"))
            if inherited is not None:
                item["inherited_legacy_ids"] = inherited
            result.append(item)
        return result

    def agent_count(self) -> int:
        rows = self._rows("SELECT COUNT(*) FROM agents")
        return int(rows[0][0]) if rows else 0

    # Short alias used by route handlers and third-party consumers.
    agents = list_agents

    def lineage(
        self,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, object]:
        by_lineage: dict[str, list[dict[str, object]]] = {}
        for agent in self.list_agents(offset=offset, limit=limit):
            lineage_id = agent.get("lineage_id")
            if not isinstance(lineage_id, str):
                lineage_id = f"agent:{agent['agent_id']}"
            by_lineage.setdefault(lineage_id, []).append(agent)

        ordered_lineages = {
            lineage_id: sorted(
                by_lineage[lineage_id],
                key=lambda item: (
                    item.get("generation") if type(item.get("generation")) is int else -1,
                    item.get("born_tick") if type(item.get("born_tick")) is int else -1,
                    str(item["agent_id"]),
                ),
            )
            for lineage_id in sorted(by_lineage)
        }
        predecessors = (
            self._lineage_predecessors(
                [str(agents[0]["agent_id"]) for agents in ordered_lineages.values() if agents]
            )
            if offset > 0
            else {}
        )
        nodes: list[dict[str, object]] = []
        edges: list[dict[str, object]] = []
        boundary_parents: list[dict[str, object]] = []
        for lineage_id, agents in ordered_lineages.items():
            if agents:
                first = agents[0]
                first_id = str(first["agent_id"])
                predecessor = predecessors.get(first_id)
                if predecessor is not None:
                    boundary_parents.append(
                        {
                            "lineage_id": lineage_id,
                            "source": predecessor,
                            "target": first_id,
                        }
                    )
            previous: str | None = None
            for agent in agents:
                agent_id = str(agent["agent_id"])
                nodes.append(
                    {
                        key: value
                        for key, value in agent.items()
                        if key
                        in {
                            "agent_id",
                            "lineage_id",
                            "generation",
                            "generation_id",
                            "born_tick",
                            "died_tick",
                            "cause",
                            "model",
                            "valley",
                        }
                    }
                )
                if previous is not None:
                    edges.append(
                        {
                            "source": previous,
                            "target": agent_id,
                            "lineage_id": lineage_id,
                        }
                    )
                previous = agent_id
        total = self.agent_count()
        return {
            "nodes": nodes,
            "edges": edges,
            "boundary_parents": boundary_parents,
            "page": {
                "offset": offset,
                "limit": limit,
                "total": total,
                "has_more": limit is not None and offset + len(nodes) < total,
            },
        }

    def _lineage_predecessors(self, agent_ids: list[str]) -> dict[str, str]:
        if not agent_ids:
            return {}
        rows = self._rows(
            """
            WITH ordered AS (
                SELECT agent_id,
                       LAG(agent_id) OVER (
                           PARTITION BY json_extract(data_json, '$.lineage_id')
                           ORDER BY CAST(json_extract(data_json, '$.generation') AS INTEGER),
                                    created_event_seq
                       ) AS predecessor
                FROM agents
                WHERE json_type(data_json, '$.lineage_id') = 'text'
                  AND json_type(data_json, '$.generation') = 'integer'
            )
            SELECT agent_id, predecessor FROM ordered
            WHERE agent_id IN (SELECT value FROM json_each(?))
            """,
            (json.dumps(agent_ids, separators=(",", ":")),),
        )
        return {
            target: predecessor
            for row in rows
            if (target := _safe_optional(row[0])) is not None
            and (predecessor := _safe_optional(row[1])) is not None
        }

    def list_legacies(
        self,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        if type(offset) is not int or not 0 <= offset <= SQLITE_INT_MAX:
            raise ValueError("offset must fit SQLite's signed integer range")
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("limit must be a positive integer or None")
        sql = """
            SELECT legacy_id, author_agent_id, generation_id, valley, channel, text,
                   parent_legacy_ids_json, event_seq, data_json
            FROM legacies ORDER BY event_seq
            """
        parameters: Sequence[object] = ()
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            parameters = (limit, offset)
        rows = self._rows(sql, parameters)
        result: list[dict[str, object]] = []
        for row in rows:
            payload = _decode_object(row[8], context="legacy payload")
            parents_value = _decode_json(row[6], context="legacy parents")
            parents = _safe_token_list(parents_value) or []
            item: dict[str, object] = {
                "legacy_id": _safe_text(row[0]),
                "author_agent_id": _safe_optional(row[1]),
                "generation_id": _safe_optional(row[2]),
                "valley": _safe_optional(row[3]),
                "channel": _safe_text(row[4]),
                "text": sanitize_display_text(row[5], max_length=MAX_LEGACY_TEXT) or "",
                "parent_legacy_ids": parents,
                "event_seq": row[7],
            }
            _copy_int(payload, item, "generation")
            result.append(item)
        return result

    def legacy_count(self) -> int:
        rows = self._rows("SELECT COUNT(*) FROM legacies")
        return int(rows[0][0]) if rows else 0

    legacies = list_legacies

    def world(self, *, tick: int | None = None) -> dict[str, object] | None:
        if tick is not None and (
            type(tick) is not int or not 0 <= tick <= SQLITE_INT_MAX
        ):
            raise ValueError("tick must fit SQLite's signed integer range or be None")
        if tick is None:
            sql = "SELECT tick, event_seq, state_json FROM checkpoints ORDER BY tick DESC LIMIT 1"
            params: Sequence[object] = ()
        else:
            sql = (
                "SELECT tick, event_seq, state_json FROM checkpoints "
                "WHERE tick <= ? ORDER BY tick DESC LIMIT 1"
            )
            params = (tick,)
        rows = self._rows(sql, params)
        if not rows:
            return None
        checkpoint = _decode_object(rows[0][2], context="checkpoint state")
        raw_world = checkpoint.get("world", checkpoint)
        if not isinstance(raw_world, Mapping):
            raise ReadModelDataError("checkpoint world is not an object")
        return {
            "checkpoint_tick": rows[0][0],
            "event_seq": rows[0][1],
            "world": _project_world(raw_world),
        }

    def ticks(self, *, limit: int | None = None) -> dict[str, object]:
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("limit must be a positive integer or None")
        sql = (
            "SELECT seq, tick, payload_json FROM events "
            "WHERE type = 'tick_commit' ORDER BY tick"
        )
        parameters: Sequence[object] = ()
        if limit is not None:
            sql = (
                "SELECT seq, tick, payload_json FROM ("
                "SELECT seq, tick, payload_json FROM events "
                "WHERE type = 'tick_commit' ORDER BY tick DESC LIMIT ?"
                ") ORDER BY tick"
            )
            parameters = (limit,)
        commit_rows = self._rows(
            sql,
            parameters,
        )
        ticks: list[dict[str, object]] = []
        for seq, tick, raw_payload in commit_rows:
            payload = _decode_object(raw_payload, context="tick commit payload")
            item: dict[str, object] = {"tick": tick, "commit_seq": seq}
            _copy_int(payload, item, "event_count")
            ticks.append(item)

        window_start = ticks[0]["tick"] if ticks else 0
        marker_types = json.dumps(
            ("agent", "agent_created", "agent_spawned", "spawn"),
            separators=(",", ":"),
        )
        generation_rows = self._rows(
            """
            SELECT CAST(json_extract(payload_json, '$.generation') AS INTEGER),
                   json_extract(payload_json, '$.generation_id'), MIN(tick)
            FROM events
            WHERE type IN (SELECT value FROM json_each(?))
              AND tick >= ?
              AND json_type(payload_json, '$.generation') = 'integer'
              AND json_type(payload_json, '$.generation_id') = 'text'
            GROUP BY 1, 2 ORDER BY 1, 3, 2
            """,
            (marker_types, window_start),
        )
        markers = [
            {
                "generation": row[0],
                "generation_id": _safe_text(row[1]),
                "started_tick": row[2],
            }
            for row in generation_rows
        ]
        previous_rows = self._rows(
            """
            SELECT CAST(json_extract(payload_json, '$.generation') AS INTEGER),
                   json_extract(payload_json, '$.generation_id')
            FROM events
            WHERE type IN (SELECT value FROM json_each(?))
              AND tick < ?
              AND json_type(payload_json, '$.generation') = 'integer'
              AND json_type(payload_json, '$.generation_id') = 'text'
            ORDER BY tick DESC, seq DESC LIMIT 1
            """,
            (marker_types, window_start),
        )
        if previous_rows:
            previous = {
                "generation": previous_rows[0][0],
                "generation_id": _safe_text(previous_rows[0][1]),
                "started_tick": window_start,
                "continued_from_before_window": True,
            }
            if not any(
                marker["generation"] == previous["generation"]
                and marker["generation_id"] == previous["generation_id"]
                for marker in markers
            ):
                markers.insert(0, previous)
        count_rows = self._rows(
            "SELECT COUNT(*) FROM events WHERE type = 'tick_commit'"
        )
        total_ticks = int(count_rows[0][0]) if count_rows else 0
        return {
            "ticks": ticks,
            "generation_markers": markers,
            "total_ticks": total_ticks,
            "window_truncated": limit is not None and total_ticks > len(ticks),
        }

    def token_metrics(
        self,
        *,
        after_tick: int | None = None,
        limit_ticks: int | None = None,
    ) -> dict[str, object]:
        if after_tick is not None and (
            type(after_tick) is not int or not -1 <= after_tick <= SQLITE_INT_MAX
        ):
            raise ValueError("after_tick must fit SQLite's signed integer range")
        if limit_ticks is not None and (type(limit_ticks) is not int or limit_ticks < 1):
            raise ValueError("limit_ticks must be a positive integer or None")
        if after_tick is not None and limit_ticks is not None:
            raise ValueError("after_tick and limit_ticks are mutually exclusive")
        sql = """
            SELECT tick, provider, model,
                   SUM(input_tokens), SUM(output_tokens), SUM(reasoning_tokens),
                   SUM(total_tokens), COUNT(*)
            FROM token_usage
            GROUP BY tick, provider, model
            ORDER BY tick, provider, model
            """
        parameters: Sequence[object] = ()
        if after_tick is not None:
            sql = """
            SELECT t.tick, t.provider, t.model,
                   SUM(t.input_tokens), SUM(t.output_tokens), SUM(t.reasoning_tokens),
                   SUM(t.total_tokens), COUNT(*)
            FROM events AS e JOIN token_usage AS t ON t.event_seq = e.seq
            WHERE e.type IN (SELECT value FROM json_each(?)) AND e.tick > ?
            GROUP BY t.tick, t.provider, t.model
            ORDER BY t.tick, t.provider, t.model
            """
            parameters = (_TOKEN_EVENT_TYPES_JSON, after_tick)
        elif limit_ticks is not None:
            sql = """
            WITH selected_ticks(tick) AS (
                SELECT DISTINCT tick FROM (
                    SELECT tick FROM (
                        SELECT DISTINCT tick FROM events WHERE type = 'token_usage'
                        ORDER BY tick DESC LIMIT ?
                    )
                    UNION ALL
                    SELECT tick FROM (
                        SELECT DISTINCT tick FROM events WHERE type = 'usage'
                        ORDER BY tick DESC LIMIT ?
                    )
                    UNION ALL
                    SELECT tick FROM (
                        SELECT DISTINCT tick FROM events WHERE type = 'model_usage'
                        ORDER BY tick DESC LIMIT ?
                    )
                ) ORDER BY tick DESC LIMIT ?
            )
            SELECT t.tick, t.provider, t.model,
                   SUM(t.input_tokens), SUM(t.output_tokens), SUM(t.reasoning_tokens),
                   SUM(t.total_tokens), COUNT(*)
            FROM selected_ticks AS selected
            JOIN events AS e
              ON e.tick = selected.tick
             AND e.type IN (SELECT value FROM json_each(?))
            JOIN token_usage AS t ON t.event_seq = e.seq
            GROUP BY t.tick, t.provider, t.model
            ORDER BY t.tick, t.provider, t.model
            """
            parameters = (
                limit_ticks,
                limit_ticks,
                limit_ticks,
                limit_ticks,
                _TOKEN_EVENT_TYPES_JSON,
            )
        rows = self._rows(sql, parameters)
        series: list[dict[str, object]] = []
        totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
            "calls": 0,
        }
        for row in rows:
            item = {
                "tick": row[0],
                "provider": _safe_optional(row[1]),
                "model": _safe_optional(row[2]),
                "input_tokens": row[3],
                "output_tokens": row[4],
                "reasoning_tokens": row[5],
                "total_tokens": row[6],
                "calls": row[7],
            }
            series.append(item)
            for key in totals:
                totals[key] += int(item[key])
        first_tick = series[0]["tick"] if series else None
        last_tick = series[-1]["tick"] if series else None
        returned_ticks = len({item["tick"] for item in series})
        older_exists = bool(
            self._rows(
                "SELECT 1 FROM events "
                "WHERE type IN (SELECT value FROM json_each(?)) AND tick < ? LIMIT 1",
                (_TOKEN_EVENT_TYPES_JSON, first_tick),
            )
        ) if type(first_tick) is int else False
        return {
            "series": series,
            "totals": totals,
            "window": {
                "after_tick": after_tick,
                "limit_ticks": limit_ticks,
                "first_tick": first_tick,
                "last_tick": last_tick,
                "returned_ticks": returned_ticks,
                "total_ticks": None if older_exists else returned_ticks,
                "total_ticks_lower_bound": returned_ticks + int(older_exists),
                "window_truncated": older_exists,
                "totals_scope": "window",
            },
        }

    def budget_metrics(self) -> dict[str, object]:
        rows = self._rows("SELECT tick, state_json FROM checkpoints ORDER BY tick DESC LIMIT 1")
        usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "failures": 0}
        tick: int | None = None
        if rows:
            tick = rows[0][0]
            state = _decode_object(rows[0][1], context="checkpoint state")
            raw_usage = state.get("budget")
            if isinstance(raw_usage, Mapping):
                for name in usage:
                    value = raw_usage.get(name)
                    if type(value) is int and value >= 0:
                        usage[name] = value

        limits: dict[str, int] | None = None
        try:
            config = load_manifest_config(self.run_dir)
        except (OSError, ReplayError, ValueError):
            config = None
        if config is not None:
            limits = {
                "calls": config.budget.max_calls,
                "input_tokens": config.budget.max_input_tokens,
                "output_tokens": config.budget.max_output_tokens,
                "failures": config.budget.max_failures,
            }
        gauges: list[dict[str, object]] = []
        for name, used in usage.items():
            limit = limits[name] if limits is not None else None
            ratio = None if limit in {None, 0} else used / limit
            gauges.append({"name": name, "used": used, "limit": limit, "ratio": ratio})
        return {"tick": tick, "usage": usage, "limits": limits, "gauges": gauges}

    def behavior_metrics(self) -> list[dict[str, object]]:
        events = self._raw_events()
        expected = _expected_generations(self.run_dir)
        try:
            points = behavior_adoption_curve(events, expected_generations=expected)
        except ValueError as exc:
            raise ReadModelDataError("behavior measurement failed") from exc
        return [asdict(point) for point in points]

    def knowledge_survival_metrics(self) -> list[dict[str, object]]:
        events = self._raw_events()
        try:
            config = load_manifest_config(self.run_dir)
            legacies = extract_legacies(events)
            inherited = extract_inherited_exposures(events, legacies)
            records = [*authored_measurement_records(legacies), *inherited]
            points = knowledge_survival_curve(
                records,
                config.knowledge,
                LexicalKnowledgeClassifier(),
                expected_generations=range(config.population.generations),
                expected_channels=("written",),
                expected_bases=tuple(MeasurementBasis),
            )
        except (OSError, ReplayError, ValueError) as exc:
            raise ReadModelDataError("knowledge-survival measurement failed") from exc
        return [asdict(point) for point in points]

    def _raw_events(self) -> list[dict[str, object]]:
        rows = self._rows(
            """
            SELECT schema_version, run_id, seq, tick, type, payload_json, prev_hash, hash
            FROM events ORDER BY seq
            """
        )
        return [
            {
                "schema_version": row[0],
                "run_id": row[1],
                "seq": row[2],
                "tick": row[3],
                "type": row[4],
                "payload": _decode_object(row[5], context="event payload"),
                "prev_hash": row[6],
                "hash": row[7],
            }
            for row in rows
        ]

    def _rows(
        self, sql: str, parameters: Sequence[object] = ()
    ) -> list[sqlite3.Row]:
        def execute(connection: sqlite3.Connection) -> list[sqlite3.Row]:
            return list(connection.execute(sql, tuple(parameters)).fetchall())

        return self._with_connection(execute)

    def _with_connection(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        attempts = self.busy_retries + 1
        last_error: BaseException | None = None
        for attempt in range(attempts):
            try:
                with self._connect() as connection:
                    return operation(connection)
            except sqlite3.OperationalError as exc:
                last_error = exc
                if not _is_busy(exc) or attempt + 1 >= attempts:
                    break
            except (sqlite3.DatabaseError, OSError) as exc:
                last_error = exc
                break
            if self.retry_delay:
                time.sleep(self.retry_delay * (attempt + 1))
        raise ReadModelUnavailable("SQLite read projection is unavailable") from last_error

    def _connect(self) -> AbstractContextManager[sqlite3.Connection]:
        return open_projection_snapshot(self.sqlite_path, timeout=0)


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _safe_sidecar_exists(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ReadModelUnavailable("SQLite WAL sidecar is unsafe")
    return True


def _decode_json(raw: object, *, context: str) -> object:
    if not isinstance(raw, (str, bytes, bytearray)):
        raise ReadModelDataError(f"{context} is not encoded JSON")
    try:
        return strict_json_loads(raw)
    except EventValidationError as exc:
        raise ReadModelDataError(f"{context} is invalid") from exc


def _decode_object(raw: object, *, context: str) -> dict[str, object]:
    value = _decode_json(raw, context=context)
    if not isinstance(value, dict):
        raise ReadModelDataError(f"{context} is not an object")
    return value


def _safe_text(value: object, *, max_length: int = 512) -> str:
    return sanitize_display_text(value, max_length=max_length) or ""


def _safe_optional(value: object, *, max_length: int = 512) -> str | None:
    return sanitize_display_text(value, max_length=max_length)


def _copy_text(source: Mapping[str, object], target: dict[str, object], name: str) -> None:
    value = _safe_optional(source.get(name))
    if value is not None:
        target[name] = value


def _copy_int(source: Mapping[str, object], target: dict[str, object], name: str) -> None:
    value = source.get(name)
    if type(value) is int and value >= 0:
        target[name] = value


def _safe_token_list(value: object) -> list[str] | None:
    if not isinstance(value, (list, tuple)) or len(value) > 128:
        return None
    result: list[str] = []
    for item in value:
        cleaned = _safe_optional(item, max_length=256)
        if cleaned is None:
            return None
        result.append(cleaned)
    return result


def _project_world(world: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    tick = world.get("tick")
    if type(tick) is int and tick >= 0:
        result["tick"] = tick

    locations: dict[str, object] = {}
    raw_locations = world.get("locations")
    if isinstance(raw_locations, Mapping):
        for raw_id, raw_location in list(raw_locations.items())[:10_000]:
            location_id = _safe_optional(raw_id, max_length=256)
            if location_id is None or not isinstance(raw_location, Mapping):
                continue
            neighbors = _safe_token_list(raw_location.get("neighbors")) or []
            locations[location_id] = {"id": location_id, "neighbors": neighbors}
    result["locations"] = locations

    agents: dict[str, object] = {}
    raw_agents = world.get("agents")
    if isinstance(raw_agents, Mapping):
        live_count = 0
        dead_count = 0
        invalid_count = 0
        for raw_id, raw_agent in raw_agents.items():
            if not isinstance(raw_agent, Mapping):
                invalid_count += 1
                continue
            alive = raw_agent.get("alive")
            if alive is False:
                dead_count += 1
                continue
            if alive is not True:
                invalid_count += 1
                continue
            live_count += 1
            if len(agents) >= 10_000:
                continue
            agent_id = _safe_optional(raw_id, max_length=256)
            if agent_id is None:
                invalid_count += 1
                continue
            item: dict[str, object] = {"agent_id": agent_id}
            for name in ("loc", "death_cause"):
                _copy_text(raw_agent, item, name)
            for name in ("hp", "hunger", "age", "max_age", "generation", "birth_tick"):
                _copy_int(raw_agent, item, name)
            item["alive"] = True
            inherited = _safe_token_list(raw_agent.get("inherited_legacy_ids"))
            if inherited is not None:
                item["inherited_legacy_ids"] = inherited
            inventory = _project_count_map(raw_agent.get("inventory"))
            if inventory is not None:
                item["inventory"] = inventory
            agents[agent_id] = item
        result["agent_counts"] = {
            "live": live_count,
            "dead": dead_count,
            "invalid": invalid_count,
            "live_truncated": live_count > len(agents),
        }
    result["agents"] = agents

    resources: dict[str, object] = {}
    raw_resources = world.get("resources")
    if isinstance(raw_resources, Mapping):
        for raw_id, raw_counts in list(raw_resources.items())[:10_000]:
            location_id = _safe_optional(raw_id, max_length=256)
            counts = _project_count_map(raw_counts)
            if location_id is not None and counts is not None:
                resources[location_id] = counts
    result["resources"] = resources

    weather: dict[str, object] = {}
    raw_weather = world.get("weather")
    if isinstance(raw_weather, Mapping):
        current = _safe_optional(raw_weather.get("current"), max_length=64)
        recent = _safe_token_list(raw_weather.get("recent"))
        if current is not None:
            weather["current"] = current
        if recent is not None:
            weather["recent"] = recent
    result["weather"] = weather

    raw_decoy = world.get("false_decoy")
    if isinstance(raw_decoy, Mapping):
        decoy: dict[str, object] = {}
        for name in ("decoy_id", "location"):
            _copy_text(raw_decoy, decoy, name)
        _copy_int(raw_decoy, decoy, "birth_tick")
        emitted = raw_decoy.get("emitted_ticks")
        if isinstance(emitted, (list, tuple)) and all(type(value) is int for value in emitted):
            decoy["emitted_ticks"] = list(emitted[:128])
        result["false_decoy"] = decoy
    return result


def _project_count_map(value: object) -> dict[str, int] | None:
    if not isinstance(value, Mapping) or len(value) > 1_000:
        return None
    result: dict[str, int] = {}
    for raw_key, raw_value in value.items():
        key = _safe_optional(raw_key, max_length=128)
        if key is None or type(raw_value) is not int or raw_value < 0:
            continue
        result[key] = raw_value
    return result


def _expected_generations(run_dir: Path) -> range | None:
    try:
        config = load_manifest_config(run_dir)
    except (OSError, ReplayError, ValueError):
        return None
    return range(config.population.generations)


__all__ = [
    "ReadModel",
    "ReadModelDataError",
    "ReadModelError",
    "ReadModelUnavailable",
]
