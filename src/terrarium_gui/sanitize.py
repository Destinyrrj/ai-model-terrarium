"""Fail-closed projections for data displayed by the Terrarium GUI.

The durable event log contains model-authored text and opaque payloads.  Neither
is a public API contract.  This module deliberately copies only known fields and
uses :func:`terrarium.storage.sanitize_control_text` at the final display
boundary.  In particular, ``raw_text`` and checkpoint state are never returned.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Final

from terrarium.storage import sanitize_control_text

type Sanitizer = Callable[[object], object]

_INVALID: Final = object()
_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_HASH_RE: Final = re.compile(r"^[0-9a-f]{64}$")
MAX_SHORT_TEXT: Final = 512
MAX_LEGACY_TEXT: Final = 64 * 1024


def _text(value: object, *, max_length: int) -> object:
    if not isinstance(value, str):
        return _INVALID
    return sanitize_control_text(value, max_length=max_length)


def _short_text(value: object) -> object:
    return _text(value, max_length=MAX_SHORT_TEXT)


def _legacy_text(value: object) -> object:
    return _text(value, max_length=MAX_LEGACY_TEXT)


def _token(value: object) -> object:
    if not isinstance(value, str):
        return _INVALID
    cleaned = sanitize_control_text(value, max_length=256)
    if _TOKEN_RE.fullmatch(cleaned) is None:
        return _INVALID
    return cleaned


def _digest(value: object) -> object:
    return value if isinstance(value, str) and _HASH_RE.fullmatch(value) else _INVALID


def _nonnegative_int(value: object) -> object:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        return _INVALID
    return value


def _signed_int(value: object) -> object:
    if type(value) is not int or not -(2**63) <= value <= 2**63 - 1:
        return _INVALID
    return value


def _number(value: object) -> object:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _INVALID
    if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
        return _INVALID
    return value


def _boolean(value: object) -> object:
    return value if type(value) is bool else _INVALID


def _token_list(value: object) -> object:
    if not isinstance(value, (list, tuple)) or len(value) > 32:
        return _INVALID
    result: list[str] = []
    for item in value:
        projected = _token(item)
        if projected is _INVALID:
            return _INVALID
        assert isinstance(projected, str)
        result.append(projected)
    return result


def _project_fields(
    value: Mapping[object, object], policy: Mapping[str, Sanitizer]
) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, sanitizer in policy.items():
        if name not in value:
            continue
        projected = sanitizer(value[name])
        if projected is not _INVALID:
            result[name] = projected
    return result


def _action(value: object) -> object:
    """Project the closed mechanics action schema, never an arbitrary mapping."""

    if not isinstance(value, Mapping):
        return _INVALID
    projected = _project_fields(
        value,
        {
            "agent_id": _token,
            "type": _token,
            "destination": _token,
            "resource": _token,
            "item": _token,
            "depth": _nonnegative_int,
        },
    )
    action_type = projected.get("type")
    required = {
        "noop": set(),
        "move": {"destination"},
        "forage": {"resource"},
        "eat": {"item"},
        "dig": {"depth"},
    }
    if action_type not in required or "agent_id" not in projected:
        return _INVALID
    variants = {"destination", "resource", "item", "depth"}
    if variants.intersection(projected) != required[action_type]:
        return _INVALID
    if action_type == "dig" and not 1 <= projected["depth"] <= 10:
        return _INVALID
    return projected


_AGENT_FIELDS: Final[dict[str, Sanitizer]] = {
    "agent_id": _token,
    "generation": _nonnegative_int,
    "generation_id": _token,
    "lineage_id": _token,
    "location": _token,
    "inherited_legacy_ids": _token_list,
    "provider": _token,
    "model": _short_text,
    "valley": _token,
}
_LEGACY_FIELDS: Final[dict[str, Sanitizer]] = {
    "id": _token,
    "legacy_id": _token,
    "author_agent_id": _token,
    "generation": _nonnegative_int,
    "generation_id": _token,
    "valley": _token,
    "channel": _token,
    "text": _legacy_text,
    "parent_legacy_ids": _token_list,
}
_DEATH_FIELDS: Final[dict[str, Sanitizer]] = {
    "agent": _token,
    "agent_id": _token,
    "lineage_id": _token,
    "generation": _nonnegative_int,
    "generation_id": _token,
    "cause": _token,
    "location": _token,
    "age": _nonnegative_int,
}
_ACTION_FIELDS: Final[dict[str, Sanitizer]] = {
    "action_id": _token,
    "agent": _token,
    "agent_id": _token,
    "action_type": _token,
    "kind": _token,
    "type": _token,
    "action": _action,
    "valid": _boolean,
    "reason": _short_text,
    "error_code": _token,
    "replacement": _token,
}
_EFFECT_FIELDS: Final[dict[str, Sanitizer]] = {
    "agent": _token,
    "agent_id": _token,
    "cause": _token,
    "reason": _short_text,
    "resource": _token,
    "location": _token,
    "before": _signed_int,
    "after": _signed_int,
    "amount": _nonnegative_int,
    "generation": _nonnegative_int,
}
_TOKEN_USAGE_FIELDS: Final[dict[str, Sanitizer]] = {
    "agent_id": _token,
    "operation": _token,
    "provider": _token,
    "model": _short_text,
    "input_tokens": _nonnegative_int,
    "output_tokens": _nonnegative_int,
    "reasoning_tokens": _nonnegative_int,
    "total_tokens": _nonnegative_int,
    "success": _boolean,
    "status": _token,
}
_GENERATION_FIELDS: Final[dict[str, Sanitizer]] = {
    "id": _token,
    "generation": _nonnegative_int,
    "generation_id": _token,
    "valley": _token,
    "model": _short_text,
}
_TICK_BEGIN_FIELDS: Final[dict[str, Sanitizer]] = {"event_count": _nonnegative_int}
_TICK_COMMIT_FIELDS: Final[dict[str, Sanitizer]] = {
    "begin_seq": _nonnegative_int,
    "event_count": _nonnegative_int,
    "checkpoint_hash": _digest,
    "checkpoint_event_hash": _digest,
}
_CHECKPOINT_FIELDS: Final[dict[str, Sanitizer]] = {"state_hash": _digest}
_FAILURE_FIELDS: Final[dict[str, Sanitizer]] = {
    "agent_id": _token,
    "generation": _nonnegative_int,
    "reason": _short_text,
    "status": _token,
    "error_code": _token,
}
_WORLD_EVENT_FIELDS: Final[dict[str, Sanitizer]] = {
    "agent_id": _token,
    "location": _token,
    "destination": _token,
    "resource": _token,
    "item": _token,
    "depth": _nonnegative_int,
    "amount": _nonnegative_int,
    "damage": _nonnegative_int,
    "cause": _token,
    "weather": _token,
    "valid": _boolean,
    "success": _boolean,
}
_RUN_FIELDS: Final[dict[str, Sanitizer]] = {"manifest_sha256": _digest}
_DECOY_FIELDS: Final[dict[str, Sanitizer]] = {
    "location": _token,
    "birth_tick": _nonnegative_int,
    "tick": _nonnegative_int,
}


def _policies() -> dict[str, Mapping[str, Sanitizer]]:
    policies: dict[str, Mapping[str, Sanitizer]] = {
        "tick_begin": _TICK_BEGIN_FIELDS,
        "state_checkpoint": _CHECKPOINT_FIELDS,
        "tick_commit": _TICK_COMMIT_FIELDS,
        "run_started": _RUN_FIELDS,
        "agent": _AGENT_FIELDS,
        "agent_spawned": _AGENT_FIELDS,
        "agent_created": _AGENT_FIELDS,
        "spawn": _AGENT_FIELDS,
        "legacy": _LEGACY_FIELDS,
        "legacy_created": _LEGACY_FIELDS,
        "legacy_written": _LEGACY_FIELDS,
        "oral_legacy": _LEGACY_FIELDS,
        "death": _DEATH_FIELDS,
        "agent_died": _DEATH_FIELDS,
        "death_recorded": _DEATH_FIELDS,
        "action": _ACTION_FIELDS,
        "intent": _ACTION_FIELDS,
        "action_resolved": _ACTION_FIELDS,
        "invalid_action": _ACTION_FIELDS,
        "action_validated": _ACTION_FIELDS,
        "effect": _EFFECT_FIELDS,
        "shock": _EFFECT_FIELDS,
        "token_usage": _TOKEN_USAGE_FIELDS,
        "usage": _TOKEN_USAGE_FIELDS,
        "model_usage": _TOKEN_USAGE_FIELDS,
        "generation": _GENERATION_FIELDS,
        "generation_started": _GENERATION_FIELDS,
        "generation_spawned": _GENERATION_FIELDS,
        "generation_ended": _GENERATION_FIELDS,
        "generation_completed": _GENERATION_FIELDS,
        "adapter_failed": _FAILURE_FIELDS,
        "deathbed_failed": _FAILURE_FIELDS,
        "decoy_scheduled": _DECOY_FIELDS,
        "decoy_emitted": _DECOY_FIELDS,
    }
    # Mechanical events are structured and share a small display projection.
    for name in (
        "ate",
        "collapse",
        "damage",
        "dig",
        "dug",
        "forage",
        "foraged",
        "move",
        "moved",
        "poison_damage",
        "starvation_damage",
        "weather_changed",
        "world_step",
    ):
        policies[name] = _WORLD_EVENT_FIELDS
    return policies


EVENT_FIELD_POLICIES: Final = _policies()


def project_event(event: Mapping[str, object]) -> dict[str, object] | None:
    """Return a detached, allowlisted event or ``None`` for unknown envelopes."""

    event_type = event.get("type")
    policy = EVENT_FIELD_POLICIES.get(event_type) if isinstance(event_type, str) else None
    if policy is None:
        return None
    seq = _nonnegative_int(event.get("seq"))
    tick = _nonnegative_int(event.get("tick"))
    payload = event.get("payload")
    if seq is _INVALID or tick is _INVALID or not isinstance(payload, Mapping):
        return None

    projected: dict[str, object] = {
        "seq": seq,
        "tick": tick,
        "type": event_type,
        "payload": _project_fields(payload, policy),
    }
    schema_version = _nonnegative_int(event.get("schema_version"))
    run_id = _token(event.get("run_id"))
    digest = _digest(event.get("hash"))
    if schema_version is not _INVALID:
        projected["schema_version"] = schema_version
    if run_id is not _INVALID:
        projected["run_id"] = run_id
    if digest is not _INVALID:
        projected["hash"] = digest
    return projected


def sanitize_event(event: Mapping[str, object]) -> dict[str, object] | None:
    """Compatibility alias for :func:`project_event`."""

    return project_event(event)


def sanitize_display_text(value: object, *, max_length: int = MAX_SHORT_TEXT) -> str | None:
    """Sanitize an optional text field for non-event read-model responses."""

    if not isinstance(value, str):
        return None
    return sanitize_control_text(value, max_length=max_length)


__all__ = [
    "EVENT_FIELD_POLICIES",
    "MAX_LEGACY_TEXT",
    "MAX_SHORT_TEXT",
    "project_event",
    "sanitize_display_text",
    "sanitize_event",
]
