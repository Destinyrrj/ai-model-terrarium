from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from terrarium.viewer import export_viewer


def test_viewer_escapes_by_construction_and_drops_raw_fields(tmp_path: Path) -> None:
    payload = '<img src=x onerror=alert(1)><script>alert(2)</script>'
    events = [
        {
            "tick": 3,
            "type": "legacy_created",
            "payload": {
                "id": "leg_x",
                "generation": 1,
                "text": payload,
                "reasoning": "private chain",
                "stderr": "secret",
            },
        }
    ]
    export_viewer(events, run_id="safe-run", output_dir=tmp_path)
    data = json.loads((tmp_path / "data.json").read_text(encoding="utf-8"))
    assert data["events"][0]["payload"]["text"] == payload
    assert "reasoning" not in data["events"][0]["payload"]
    assert "stderr" not in data["events"][0]["payload"]
    app = (tmp_path / "app.js").read_text(encoding="utf-8")
    assert "textContent" in app
    assert "innerHTML" not in app
    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "Content-Security-Policy" in index
    assert payload not in index


def test_viewer_projects_exact_fields_and_drops_nested_secrets(tmp_path: Path) -> None:
    events = [
        {
            "tick": 4,
            "type": "intent",
            "payload": {
                "action_id": "4:agent_1",
                "agent_id": "agent_1",
                "valid": True,
                "action": {
                    "agent_id": "agent_1",
                    "type": "move",
                    "destination": "valley/grove",
                    "reasoning": "nested private chain",
                    "metadata": {"raw": "provider secret"},
                },
                "private": {"prompt": "do not export"},
                "raw_response": "do not export either",
            },
        }
    ]

    export_viewer(events, run_id="safe-run", output_dir=tmp_path)

    raw = (tmp_path / "data.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["events"] == [
        {
            "tick": 4,
            "type": "intent",
            "payload": {
                "action_id": "4:agent_1",
                "agent_id": "agent_1",
                "valid": True,
                "action": {
                    "agent_id": "agent_1",
                    "type": "move",
                    "destination": "valley/grove",
                },
            },
        }
    ]
    for secret in ("nested private chain", "provider secret", "do not export"):
        assert secret not in raw


def test_viewer_rejects_malformed_required_public_fields(tmp_path: Path) -> None:
    events = [
        {
            "tick": 1,
            "type": "legacy_written",
            "payload": {
                "legacy_id": "legacy_1",
                "author_agent_id": "agent_1",
                "generation": 0,
                "text": "safe\u202ehidden",
            },
        },
        {
            "tick": True,
            "type": "death",
            "payload": {"agent_id": "agent_1", "cause": "old_age"},
        },
    ]

    export_viewer(events, run_id="safe-run", output_dir=tmp_path)
    data = json.loads((tmp_path / "data.json").read_text(encoding="utf-8"))
    assert data["events"] == []


def test_viewer_atomically_replaces_child_symlink_without_following_it(tmp_path: Path) -> None:
    output = tmp_path / "viewer"
    output.mkdir()
    victim = tmp_path / "victim.json"
    victim.write_text("keep me", encoding="utf-8")
    os.symlink(victim, output / "data.json")

    export_viewer([], run_id="safe-run", output_dir=output)

    assert victim.read_text(encoding="utf-8") == "keep me"
    assert not (output / "data.json").is_symlink()
    assert json.loads((output / "data.json").read_text(encoding="utf-8"))["events"] == []


def test_viewer_rejects_symlink_output_directory(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    os.symlink(real, linked)

    with pytest.raises(ValueError, match="real directory"):
        export_viewer([], run_id="safe-run", output_dir=linked)
