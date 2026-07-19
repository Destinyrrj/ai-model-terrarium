from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from terrarium.storage import EventStore
from terrarium_gui.readmodel import ReadModel, ReadModelUnavailable, _project_world


def _draft(event_type: str, payload: dict[str, object]) -> dict[str, object]:
    return {"type": event_type, "payload": payload}


def _world(tick: int) -> dict[str, object]:
    return {
        "tick": tick,
        "locations": {
            "valley/grove": {"id": "valley/grove", "neighbors": ["valley/cave"]},
            "valley/cave": {"id": "valley/cave", "neighbors": ["valley/grove"]},
        },
        "agents": {
            "agent-1": {
                "agent_id": "agent-1",
                "loc": "valley/grove",
                "hp": 100,
                "hunger": 0,
                "age": tick,
                "max_age": 10,
                "generation": 0,
                "birth_tick": 0,
                "inherited_legacy_ids": [],
                "inventory": {"red_berry": 1},
                "alive": True,
                "death_cause": None,
            }
        },
        "resources": {
            "valley/grove": {"red_berry": 2},
            "valley/cave": {"root": 1},
        },
        "weather": {"current": "rain", "recent": ["rain"]},
        "rng_state": {"private": "not returned"},
    }


def _build_run(run_dir: Path) -> None:
    with EventStore(run_dir, "gui-read") as store:
        store.commit_tick(
            0,
            [
                _draft(
                    "agent_spawned",
                    {
                        "agent_id": "agent-1",
                        "generation": 0,
                        "generation_id": "generation_00000",
                        "lineage_id": "lineage_0",
                        "model": "mock\x1b[31m",
                        "valley": "valley",
                        "location": "valley/grove",
                        "inherited_legacy_ids": [],
                    },
                ),
                _draft(
                    "legacy_written",
                    {
                        "legacy_id": "legacy-1",
                        "author_agent_id": "agent-1",
                        "generation": 0,
                        "generation_id": "generation_00000",
                        "channel": "written",
                        "text": "Never dig deep.\x1b[31m",
                        "parent_legacy_ids": [],
                    },
                ),
                _draft(
                    "token_usage",
                    {
                        "agent_id": "agent-1",
                        "provider": "mock",
                        "model": "model-1",
                        "input_tokens": 10,
                        "output_tokens": 4,
                        "reasoning_tokens": 1,
                        "total_tokens": 15,
                    },
                ),
                _draft(
                    "death_recorded",
                    {
                        "agent_id": "agent-1",
                        "generation": 0,
                        "generation_id": "generation_00000",
                        "cause": "old_age",
                    },
                ),
            ],
            {"world": _world(0), "budget": {"calls": 1, "input_tokens": 10}},
        )


def test_readmodel_projects_scientific_views_without_writing(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _build_run(run_dir)
    before = {path.name for path in run_dir.iterdir()}
    model = ReadModel(run_dir)

    agents = model.list_agents()
    assert agents[0]["agent_id"] == "agent-1"
    assert agents[0]["lineage_id"] == "lineage_0"
    assert agents[0]["model"] == "mock"
    assert agents[0]["cause"] == "old_age"
    assert model.lineage()["nodes"][0]["generation"] == 0  # type: ignore[index]
    assert model.lineage()["nodes"][0]["cause"] == "old_age"  # type: ignore[index]

    legacies = model.list_legacies()
    assert legacies[0]["text"] == "Never dig deep."
    assert legacies[0]["parent_legacy_ids"] == []

    world = model.world()
    assert world is not None
    assert world["world"]["agents"]["agent-1"]["loc"] == "valley/grove"  # type: ignore[index]
    assert "rng_state" not in world["world"]  # type: ignore[operator]

    tokens = model.token_metrics()
    assert tokens["totals"]["total_tokens"] == 15  # type: ignore[index]
    budget = model.budget_metrics()
    assert budget["usage"]["calls"] == 1  # type: ignore[index]
    ticks = model.ticks()
    assert ticks["ticks"][0]["tick"] == 0  # type: ignore[index]
    assert ticks["generation_markers"] == [  # type: ignore[index]
        {
            "generation": 0,
            "generation_id": "generation_00000",
            "started_tick": 0,
        }
    ]
    latest = model.latest_events(limit=2)
    assert [event["seq"] for event in latest] == [5, 6]
    assert {path.name for path in run_dir.iterdir()} == before


def test_readmodel_uses_sqlite_mode_ro(tmp_path: Path) -> None:
    missing = tmp_path / "missing-run"
    missing.mkdir()
    with pytest.raises(ReadModelUnavailable):
        ReadModel(missing).list_agents()
    assert list(missing.iterdir()) == []


def test_event_pagination_filters_unknown_types_before_sql_limit(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    with EventStore(run_dir, "gui-pages") as store:
        store.commit_tick(
            0,
            [
                _draft("opaque_observation", {"raw_text": "never display"}),
                _draft("world_outputs", {"prompt": "never display"}),
                _draft(
                    "legacy_written",
                    {
                        "legacy_id": "visible",
                        "author_agent_id": "agent-1",
                        "generation": 0,
                        "generation_id": "generation_00000",
                        "channel": "written",
                        "text": "safe",
                        "parent_legacy_ids": [],
                    },
                ),
            ],
            {},
        )

    model = ReadModel(run_dir)
    first = model.events(limit=2)
    second = model.events(after_seq=first[-1]["seq"], limit=2)

    assert [event["type"] for event in first] == ["tick_begin", "legacy_written"]
    assert [event["type"] for event in second] == ["state_checkpoint", "tick_commit"]
    assert model.latest_events(limit=2) == second


def test_token_window_includes_alias_types_and_discloses_truncation(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    with EventStore(run_dir, "gui-token-window") as store:
        store.commit_tick(
            0,
            [_draft("usage", {"prompt_tokens": 3, "completion_tokens": 2})],
            {},
        )
        store.commit_tick(
            1,
            [_draft("model_usage", {"input_tokens": 7, "output_tokens": 5})],
            {},
        )

    metrics = ReadModel(run_dir).token_metrics(limit_ticks=1)

    assert [point["tick"] for point in metrics["series"]] == [1]  # type: ignore[index]
    assert metrics["totals"]["total_tokens"] == 12  # type: ignore[index]
    assert metrics["window"] == {
        "after_tick": None,
        "limit_ticks": 1,
        "first_tick": 1,
        "last_tick": 1,
        "returned_ticks": 1,
        "total_ticks": None,
        "total_ticks_lower_bound": 2,
        "window_truncated": True,
        "totals_scope": "window",
    }


def test_token_window_limit_counts_distinct_ticks_not_calls(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    usage = {"input_tokens": 1, "output_tokens": 1}
    with EventStore(run_dir, "gui-token-distinct") as store:
        store.commit_tick(0, [_draft("token_usage", usage)], {})
        store.commit_tick(
            1,
            [_draft("token_usage", usage), _draft("token_usage", usage)],
            {},
        )

    metrics = ReadModel(run_dir).token_metrics(limit_ticks=2)

    assert sorted({point["tick"] for point in metrics["series"]}) == [0, 1]  # type: ignore[index]
    assert metrics["window"]["returned_ticks"] == 2  # type: ignore[index]


def test_lineage_page_exposes_cross_page_parent(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    with EventStore(run_dir, "gui-lineage-page") as store:
        for tick, generation in enumerate((0, 1)):
            store.commit_tick(
                tick,
                [
                    _draft(
                        "agent_spawned",
                        {
                            "agent_id": f"agent-{generation}",
                            "generation": generation,
                            "generation_id": f"generation_{generation:05d}",
                            "lineage_id": "lineage-0",
                            "location": "valley/grove",
                            "inherited_legacy_ids": [],
                        },
                    )
                ],
                {},
            )

    page = ReadModel(run_dir).lineage(offset=1, limit=1)

    assert page["boundary_parents"] == [
        {
            "lineage_id": "lineage-0",
            "source": "agent-0",
            "target": "agent-1",
        }
    ]


def test_busy_errors_become_typed_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    _build_run(run_dir)
    model = ReadModel(run_dir, busy_retries=1, retry_delay=0)

    def busy() -> sqlite3.Connection:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(model, "_connect", busy)
    with pytest.raises(ReadModelUnavailable):
        model.list_agents()


def test_world_projection_selects_live_agents_before_history_bound() -> None:
    historical = {
        f"dead-{index}": {"alive": False, "generation": index}
        for index in range(10_001)
    }
    historical["current"] = {
        "alive": True,
        "generation": 79,
        "loc": "valley/grove",
    }

    projected = _project_world({"tick": 500, "agents": historical})

    assert list(projected["agents"]) == ["current"]  # type: ignore[arg-type]
    assert projected["agent_counts"] == {  # type: ignore[index]
        "live": 1,
        "dead": 10_001,
        "invalid": 0,
        "live_truncated": False,
    }
