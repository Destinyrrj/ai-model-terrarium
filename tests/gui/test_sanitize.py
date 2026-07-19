from __future__ import annotations

import json

from terrarium_gui.sanitize import MAX_LEGACY_TEXT, project_event


def envelope(event_type: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": "safe-run",
        "seq": 7,
        "tick": 3,
        "type": event_type,
        "payload": payload,
        "hash": "a" * 64,
    }


def test_legacy_projection_sanitizes_control_text_and_never_exposes_raw() -> None:
    projected = project_event(
        envelope(
            "legacy_written",
            {
                "legacy_id": "legacy-1",
                "author_agent_id": "agent-1",
                "generation": 2,
                "text": "<script>literal</script>\x1b[31m red\u202e spoof",
                "parent_legacy_ids": [],
                "raw_text": "SECRET",
                "prompt": "also private",
            },
        )
    )

    assert projected is not None
    text = projected["payload"]["text"]  # type: ignore[index]
    assert text == "<script>literal</script> red spoof"
    assert "raw_text" not in json.dumps(projected)
    assert "prompt" not in json.dumps(projected)


def test_checkpoint_projection_drops_state_and_unknown_events_fail_closed() -> None:
    projected = project_event(
        envelope(
            "state_checkpoint",
            {
                "state": {"contexts": {"agent": {"raw_text": "private"}}},
                "rng_state": {"secret": 1},
                "state_hash": "b" * 64,
            },
        )
    )
    assert projected is not None
    assert projected["payload"] == {"state_hash": "b" * 64}
    assert project_event(envelope("future_opaque_event", {"raw_text": "private"})) is None


def test_legacy_text_is_bounded() -> None:
    projected = project_event(
        envelope(
            "legacy_written",
            {
                "legacy_id": "legacy-1",
                "author_agent_id": "agent-1",
                "generation": 0,
                "text": "x" * (MAX_LEGACY_TEXT + 100),
            },
        )
    )
    assert projected is not None
    assert len(projected["payload"]["text"]) == MAX_LEGACY_TEXT  # type: ignore[index]
