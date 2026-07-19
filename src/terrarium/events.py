"""Canonical, hash-chained event envelopes.

The JSONL log is the source of truth for a run.  This module deliberately has
no dependency on the world model: payloads are opaque JSON objects and only the
small envelope is interpreted by the storage layer.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

SCHEMA_VERSION: Final = 1
GENESIS_HASH: Final = "0" * 64
HASH_RE: Final = re.compile(r"^[0-9a-f]{64}$")
EVENT_TYPE_RE: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
RUN_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class EventValidationError(ValueError):
    """An event or JSON value violates the durable-log contract."""


def _validate_json_value(value: Any, path: str = "$") -> None:
    """Reject values whose JSON representation is ambiguous or non-portable."""

    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        # ``allow_nan=False`` below is the final guard.  Keeping the explicit
        # branch gives callers a useful path in the error message.
        if value != value or value in (float("inf"), float("-inf")):
            raise EventValidationError(f"{path}: non-finite floats are not valid JSON")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise EventValidationError(f"{path}: JSON object keys must be strings")
            _validate_json_value(item, f"{path}.{key}")
        return
    raise EventValidationError(
        f"{path}: unsupported JSON value {type(value).__name__}; "
        "use dict/list/tuple/string/number/bool/null"
    )


def canonical_json(value: Any) -> str:
    """Return the one JSON representation used for hashing and persistence."""

    _validate_json_value(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:  # defensive: validation should catch it
        raise EventValidationError(str(exc)) from exc


def canonical_json_bytes(value: Any) -> bytes:
    """UTF-8 encoded :func:`canonical_json`."""

    return canonical_json(value).encode("utf-8")


def json_sha256(value: Any) -> str:
    """Hash a JSON value using the canonical encoding."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _reject_constant(value: str) -> None:
    raise EventValidationError(f"non-standard JSON numeric constant: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EventValidationError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def strict_json_loads(data: str | bytes | bytearray) -> Any:
    """Parse strict JSON, rejecting duplicate keys and NaN/Infinity."""

    try:
        value = json.loads(
            data,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except EventValidationError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EventValidationError(f"invalid JSON: {exc}") from exc
    _validate_json_value(value)
    return value


def validate_run_id(run_id: str) -> str:
    """Validate the logical run identifier (it is never used as a path)."""

    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        raise EventValidationError(
            "run_id must be 1-128 ASCII letters, digits, '.', '_' or '-', "
            "and begin with a letter or digit"
        )
    if run_id in {".", ".."}:
        raise EventValidationError("run_id may not be '.' or '..'")
    return run_id


def validate_event_type(event_type: str) -> str:
    if not isinstance(event_type, str) or EVENT_TYPE_RE.fullmatch(event_type) is None:
        raise EventValidationError("event type must be 1-128 ASCII identifier characters")
    return event_type


@dataclass(frozen=True, slots=True)
class Event:
    """Versioned event envelope linked to the previous event by SHA-256."""

    schema_version: int
    run_id: str
    seq: int
    tick: int
    type: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        seq: int,
        tick: int,
        type: str,
        payload: Mapping[str, Any],
        prev_hash: str,
        schema_version: int = SCHEMA_VERSION,
    ) -> Event:
        """Validate, detach and hash a new event."""

        fields = _validated_fields(
            schema_version=schema_version,
            run_id=run_id,
            seq=seq,
            tick=tick,
            event_type=type,
            payload=payload,
            prev_hash=prev_hash,
        )
        digest = hashlib.sha256(canonical_json_bytes(fields)).hexdigest()
        return cls(hash=digest, **fields)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> Event:
        """Construct an envelope and fail if its schema or digest is invalid."""

        if not isinstance(value, Mapping):
            raise EventValidationError("event envelope must be a JSON object")
        required = {
            "schema_version",
            "run_id",
            "seq",
            "tick",
            "type",
            "payload",
            "prev_hash",
            "hash",
        }
        actual = set(value)
        if actual != required:
            missing = sorted(required - actual)
            extra = sorted(actual - required)
            raise EventValidationError(
                f"event envelope keys do not match schema; missing={missing}, extra={extra}"
            )
        fields = _validated_fields(
            schema_version=value["schema_version"],
            run_id=value["run_id"],
            seq=value["seq"],
            tick=value["tick"],
            event_type=value["type"],
            payload=value["payload"],
            prev_hash=value["prev_hash"],
        )
        digest = value["hash"]
        if not isinstance(digest, str) or HASH_RE.fullmatch(digest) is None:
            raise EventValidationError("hash must be a lowercase SHA-256 hex digest")
        event = cls(hash=digest, **fields)
        if verify_hash:
            event.verify_hash()
        return event

    def hash_input(self) -> dict[str, Any]:
        """Envelope fields covered by the digest (all fields except ``hash``)."""

        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "seq": self.seq,
            "tick": self.tick,
            "type": self.type,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
        }

    def computed_hash(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.hash_input())).hexdigest()

    def verify_hash(self) -> None:
        expected = self.computed_hash()
        if not hmac.compare_digest(expected, self.hash):
            raise EventValidationError(
                f"event seq {self.seq}: hash mismatch; expected {expected}, got {self.hash}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {**self.hash_input(), "hash": self.hash}

    def to_json(self) -> str:
        return canonical_json(self.to_dict())


def _validated_fields(
    *,
    schema_version: Any,
    run_id: Any,
    seq: Any,
    tick: Any,
    event_type: Any,
    payload: Any,
    prev_hash: Any,
) -> dict[str, Any]:
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        raise EventValidationError(f"unsupported schema_version: {schema_version!r}")
    validate_run_id(run_id)
    if type(seq) is not int or seq < 0:
        raise EventValidationError("seq must be a non-negative integer")
    if type(tick) is not int or tick < 0:
        raise EventValidationError("tick must be a non-negative integer")
    validate_event_type(event_type)
    if not isinstance(payload, Mapping):
        raise EventValidationError("payload must be a JSON object")
    # A canonical round trip both validates and detaches caller-owned containers.
    detached = strict_json_loads(canonical_json(dict(payload)))
    if not isinstance(detached, dict):  # kept for static type checkers and defense
        raise EventValidationError("payload must be a JSON object")
    if not isinstance(prev_hash, str) or HASH_RE.fullmatch(prev_hash) is None:
        raise EventValidationError("prev_hash must be a lowercase SHA-256 hex digest")
    return {
        "schema_version": schema_version,
        "run_id": run_id,
        "seq": seq,
        "tick": tick,
        "type": event_type,
        "payload": detached,
        "prev_hash": prev_hash,
    }


__all__ = [
    "EVENT_TYPE_RE",
    "GENESIS_HASH",
    "HASH_RE",
    "RUN_ID_RE",
    "SCHEMA_VERSION",
    "Event",
    "EventValidationError",
    "canonical_json",
    "canonical_json_bytes",
    "json_sha256",
    "strict_json_loads",
    "validate_event_type",
    "validate_run_id",
]
