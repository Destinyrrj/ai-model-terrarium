from __future__ import annotations

import json
from pathlib import Path

import pytest

from terrarium.measurement import (
    BehaviorPoint,
    behavior_adoption_curve,
    write_behavior_measurements,
)


def _spawn(agent_id: str, generation: int) -> dict[str, object]:
    return {
        "type": "agent_spawned",
        "payload": {"agent_id": agent_id, "generation": generation},
    }


def _checkpoint(tick: int, recent: list[str]) -> dict[str, object]:
    return {
        "type": "state_checkpoint",
        "tick": tick,
        "payload": {
            "state": {"world": {"weather": {"current": recent[-1], "recent": recent}}},
            "rng_state": {},
            "state_hash": "irrelevant-here",
        },
    }


def _ate(tick: int, agent_id: str, item: str = "red_berry") -> dict[str, object]:
    return {"type": "ate", "tick": tick, "payload": {"agent_id": agent_id, "item": item}}


def _dug(tick: int, agent_id: str, depth: int) -> dict[str, object]:
    return {
        "type": "dug",
        "tick": tick,
        "payload": {"agent_id": agent_id, "location": "valley_a/grove", "depth": depth},
    }


def test_behavior_curve_separates_risky_and_safe_acts_by_generation() -> None:
    events = [
        _spawn("elder", 0),
        _spawn("child", 1),
        _checkpoint(0, ["rain"]),
        # Tick 1 starts inside the poison window (rain at tick-start).
        _ate(1, "elder"),
        _dug(1, "elder", depth=5),
        {
            "type": "damage",
            "tick": 1,
            "payload": {"agent_id": "elder", "amount": 40, "cause": "poison"},
        },
        {
            "type": "tunnel_collapsed",
            "tick": 1,
            "payload": {"agent_id": "elder", "location": "valley_a/grove", "depth": 5},
        },
        _checkpoint(1, ["rain", "clear"]),
        _checkpoint(2, ["rain", "clear", "clear"]),
        _checkpoint(3, ["clear", "clear", "clear"]),
        # Tick 4 starts with three clear ticks: the same acts are now safe/shallow.
        _ate(4, "child"),
        _ate(4, "child", item="root"),
        _dug(4, "child", depth=2),
        {
            "type": "damage",
            "tick": 4,
            "payload": {"agent_id": "child", "amount": 15, "cause": "starvation"},
        },
    ]
    points = behavior_adoption_curve(events)
    assert points == [
        BehaviorPoint(
            generation=0,
            agents=1,
            berry_eats=1,
            risky_berry_eats=1,
            risky_eat_rate=1.0,
            poison_damage_events=1,
            digs=1,
            deep_digs=1,
            deep_dig_rate=1.0,
            collapse_events=1,
        ),
        BehaviorPoint(
            generation=1,
            agents=1,
            berry_eats=1,
            risky_berry_eats=0,
            risky_eat_rate=0.0,
            poison_damage_events=0,
            digs=1,
            deep_digs=0,
            deep_dig_rate=0.0,
            collapse_events=0,
        ),
    ]


def test_rain_earlier_in_window_still_counts_as_risky() -> None:
    events = [
        _spawn("a", 0),
        _checkpoint(0, ["clear"]),
        _checkpoint(1, ["clear", "rain"]),
        _checkpoint(2, ["clear", "rain", "clear"]),
        _ate(3, "a"),
    ]
    (point,) = behavior_adoption_curve(events)
    assert point.risky_berry_eats == 1


def test_expected_generations_emit_zero_rows() -> None:
    points = behavior_adoption_curve([], expected_generations=range(3))
    assert [point.generation for point in points] == [0, 1, 2]
    assert all(point.agents == 0 and point.berry_eats == 0 for point in points)
    assert all(point.risky_eat_rate == 0.0 and point.deep_dig_rate == 0.0 for point in points)


def test_actor_without_spawn_record_fails_closed() -> None:
    events = [_checkpoint(0, ["clear"]), _ate(1, "ghost")]
    with pytest.raises(ValueError, match="spawn record"):
        behavior_adoption_curve(events)


def test_eat_without_prior_weather_checkpoint_fails_closed() -> None:
    events = [_spawn("a", 0), _ate(1, "a")]
    with pytest.raises(ValueError, match="weather checkpoint"):
        behavior_adoption_curve(events)


def test_checkpoint_without_world_weather_fails_closed() -> None:
    broken = {
        "type": "state_checkpoint",
        "tick": 0,
        "payload": {"state": {"budget": {}}, "rng_state": {}, "state_hash": "x"},
    }
    with pytest.raises(ValueError, match="world weather"):
        behavior_adoption_curve([broken])


def test_duplicate_spawn_fails_closed() -> None:
    with pytest.raises(ValueError, match="redefines"):
        behavior_adoption_curve([_spawn("a", 0), _spawn("a", 1)])


def test_root_eats_and_shallow_digs_are_not_risk_denominator_noise() -> None:
    events = [
        _spawn("a", 0),
        _checkpoint(0, ["rain"]),
        _ate(1, "a", item="root"),
        _dug(1, "a", depth=3),
    ]
    (point,) = behavior_adoption_curve(events)
    assert point.berry_eats == 0
    assert point.digs == 1
    assert point.deep_digs == 0


def test_write_behavior_measurements_exports_pair(tmp_path: Path) -> None:
    points = behavior_adoption_curve(
        [
            _spawn("a", 0),
            _checkpoint(0, ["rain"]),
            _ate(1, "a"),
        ]
    )
    output = tmp_path / "measurements"
    write_behavior_measurements(points, output)
    rows = json.loads((output / "behavior-adoption.json").read_text())
    assert rows[0]["generation"] == 0
    assert rows[0]["risky_berry_eats"] == 1
    csv_text = (output / "behavior-adoption.csv").read_text()
    assert csv_text.splitlines()[0].startswith("generation,agents,berry_eats")
