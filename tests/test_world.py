"""Determinism, schema-boundary, hidden-rule, and lifecycle tests for WorldEngine."""

from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

from terrarium.domain import (
    Action,
    AgentState,
    Observation,
    PCG64State,
    WeatherState,
    WorldState,
)
from terrarium.world import WorldConfig, WorldEngine


def _replace_state(state: WorldState, mutate: Any) -> WorldState:
    payload = state.model_dump(mode="json")
    mutate(payload)
    return WorldState.model_validate(payload)


def _put_in_inventory(
    state: WorldState, agent_id: str, resource: str, count: int = 1
) -> WorldState:
    return _replace_state(
        state,
        lambda payload: payload["agents"][agent_id]["inventory"].__setitem__(resource, count),
    )


def _set_resource(state: WorldState, location: str, resource: str, count: int) -> WorldState:
    return _replace_state(
        state,
        lambda payload: payload["resources"][location].__setitem__(resource, count),
    )


def _events_of(result: Any, event_type: str) -> list[dict[str, object]]:
    return [record for record in result.events if record["type"] == event_type]


@pytest.mark.parametrize(
    "payload",
    [
        {"agent_id": "A", "type": "noop"},
        {"agent_id": "A", "type": "move", "destination": "valley_a/cave"},
        {"agent_id": "A", "type": "forage", "resource": "red_berry"},
        {"agent_id": "A", "type": "eat", "item": "root"},
        {"agent_id": "A", "type": "dig", "depth": 4},
    ],
)
def test_action_accepts_only_closed_wire_variants(payload: dict[str, object]) -> None:
    action = Action.model_validate(payload)
    assert Action.model_validate(action.model_dump(mode="json")) == action


@pytest.mark.parametrize(
    "payload",
    [
        {"agent_id": "A", "type": "wait"},
        {"agent_id": "A", "type": "noop", "command": "cat /etc/passwd"},
        {"agent_id": "A", "type": "move"},
        {"agent_id": "A", "type": "move", "destination": "x", "depth": 2},
        {"agent_id": "A", "type": "forage", "resource": "gold"},
        {"agent_id": "A", "type": "eat", "item": "root", "text": "please"},
        {"agent_id": "A", "type": "dig", "depth": "4"},
        {"agent_id": "A", "type": "dig", "depth": True},
        {"agent_id": "A", "type": "dig", "depth": 0},
        {"agent_id": "A", "type": "dig", "depth": 11},
        {"agent_id": "../../A", "type": "noop"},
    ],
)
def test_action_rejects_free_text_commands_coercion_and_bad_variants(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        Action.model_validate(payload)


def test_action_schema_forbids_extra_properties_and_has_no_text_channel() -> None:
    schema = Action.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "agent_id",
        "type",
        "destination",
        "resource",
        "item",
        "depth",
    }
    assert not {"text", "command", "reason", "path", "tool"} & set(schema["properties"])


def test_world_state_json_round_trip_and_canonical_hash() -> None:
    engine = WorldEngine()
    first = engine.initial_state(1234, ["B", "A"], enable_false_decoy=False)
    second = engine.initial_state(1234, ["A", "B"], enable_false_decoy=False)

    dumped = first.model_dump(mode="json")
    restored = WorldState.model_validate(dumped)
    restored_from_json = WorldState.model_validate_json(first.model_dump_json())

    assert first == restored == restored_from_json == second
    assert first.canonical_json() == second.canonical_json()
    assert first.state_hash() == second.state_hash()
    assert len(first.state_hash()) == 64
    json.loads(first.canonical_json())


def test_validated_world_state_is_deeply_immutable() -> None:
    state = WorldEngine().initial_state(1234, ["A"], enable_false_decoy=False)
    sealed_hash = state.state_hash()

    with pytest.raises(TypeError, match="immutable"):
        state.resources["valley_a/grove"]["red_berry"] = 0
    with pytest.raises((TypeError, ValidationError)):
        state.locations["valley_a/grove"].neighbors += ("hidden",)

    assert state.state_hash() == sealed_hash

    result = WorldEngine(WorldConfig(hunger_per_tick=0)).step(
        state, {"A": {"agent_id": "A", "type": "noop", "command": "forged"}}
    )
    with pytest.raises(TypeError, match="immutable"):
        result.events[0]["type"] = "forged"


def test_world_state_rejects_extra_and_broken_references() -> None:
    state = WorldEngine().initial_state(1, ["A"], enable_false_decoy=False)
    payload = state.model_dump(mode="json")
    payload["surprise"] = True
    with pytest.raises(ValidationError):
        WorldState.model_validate(payload)

    payload = state.model_dump(mode="json")
    payload["agents"]["A"]["loc"] = "nowhere"
    with pytest.raises(ValidationError, match="unknown location"):
        WorldState.model_validate(payload)


def test_pcg64_is_explicit_and_rejects_another_bit_generator() -> None:
    state = WorldEngine().initial_state(9, ["A"])
    assert state.rng_state.bit_generator == "PCG64"
    assert isinstance(state.rng_state.to_generator().bit_generator, np.random.PCG64)
    with pytest.raises(TypeError, match="PCG64"):
        PCG64State.from_generator(np.random.Generator(np.random.Philox(9)))


def test_no_random_mechanic_means_rng_checkpoint_does_not_move() -> None:
    engine = WorldEngine(WorldConfig(hunger_per_tick=0))
    state = engine.initial_state(44, ["A", "B"], enable_false_decoy=False)
    result = engine.step(
        state,
        {
            "A": {"agent_id": "A", "type": "noop"},
            "B": {"agent_id": "B", "type": "dig", "depth": 3},
        },
    )
    assert result.state.rng_state == state.rng_state
    assert not _events_of(result, "tunnel_collapsed")


def test_resource_conflict_and_entire_result_are_input_order_invariant() -> None:
    engine = WorldEngine(WorldConfig(hunger_per_tick=0))
    state = engine.initial_state(
        8,
        ["C", "A", "B"],
        starting_positions={
            "A": "valley_a/grove",
            "B": "valley_a/grove",
            "C": "valley_a/grove",
        },
        enable_false_decoy=False,
    )
    state = _set_resource(state, "valley_a/grove", "red_berry", 1)
    ascending = OrderedDict(
        (
            agent_id,
            {"agent_id": agent_id, "type": "forage", "resource": "red_berry"},
        )
        for agent_id in ["A", "B", "C"]
    )
    descending = OrderedDict(reversed(list(ascending.items())))

    first = engine.step(state, ascending)
    second = engine.step(state, descending)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.state.state_hash() == second.state.state_hash()
    assert first.state.rng_state != state.rng_state
    winners = [
        agent_id
        for agent_id, agent in first.state.agents.items()
        if agent.inventory.get("red_berry") == 1
    ]
    assert len(winners) == 1
    conflict = _events_of(first, "resource_conflict")
    assert len(conflict) == 1
    assert conflict[0]["payload"]["claimants"] == ("A", "B", "C")
    assert conflict[0]["payload"]["winners"] == tuple(winners)


def test_simultaneous_moves_use_tick_start_graph_and_are_order_invariant() -> None:
    engine = WorldEngine(WorldConfig(hunger_per_tick=0))
    state = engine.initial_state(
        3,
        ["A", "B"],
        starting_positions={"A": "valley_a/grove", "B": "valley_a/cave"},
        enable_false_decoy=False,
    )
    actions = {
        "A": {"agent_id": "A", "type": "move", "destination": "valley_a/cave"},
        "B": {"agent_id": "B", "type": "move", "destination": "valley_a/grove"},
    }
    reversed_actions = dict(reversed(list(actions.items())))
    first = engine.step(state, actions)
    second = engine.step(state, reversed_actions)
    assert first.state.state_hash() == second.state.state_hash()
    assert first.events == second.events
    assert first.state.agents["A"].loc == "valley_a/cave"
    assert first.state.agents["B"].loc == "valley_a/grove"


def test_invalid_missing_mismatched_and_unknown_intents_are_bounded_noops() -> None:
    engine = WorldEngine(WorldConfig(hunger_per_tick=0))
    state = engine.initial_state(5, ["A", "B", "C"], enable_false_decoy=False)
    result = engine.step(
        state,
        {
            "A": {
                "agent_id": "A",
                "type": "noop",
                "command": "SECRET; rm -rf /",
            },
            "B": {"agent_id": "someone_else", "type": "noop"},
            "unknown": {"agent_id": "unknown", "type": "noop"},
        },
    )
    assert len(_events_of(result, "invalid_action")) == 2
    assert len(_events_of(result, "missing_action")) == 1
    assert len(_events_of(result, "ignored_action")) == 1
    assert result.state.agents_pos == state.agents_pos
    serialized_records = json.dumps(result.events, sort_keys=True)
    assert "SECRET" not in serialized_records
    assert "rm -rf" not in serialized_records


@pytest.mark.parametrize(
    ("weather", "item", "expected_hp"),
    [
        (WeatherState(current="rain", recent=["rain"]), "red_berry", 60),
        (WeatherState(current="clear", recent=["clear"]), "red_berry", 100),
        (WeatherState(current="rain", recent=["rain"]), "root", 100),
        (
            WeatherState(current="clear", recent=["rain", "clear"]),
            "red_berry",
            60,
        ),
    ],
)
def test_red_berry_poison_rule_after_recent_rain_only(
    weather: WeatherState, item: str, expected_hp: int
) -> None:
    engine = WorldEngine(WorldConfig(hunger_per_tick=0))
    state = engine.initial_state(0, ["A"], weather=weather, enable_false_decoy=False)
    state = _put_in_inventory(state, "A", item)
    result = engine.step(state, {"A": {"agent_id": "A", "type": "eat", "item": item}})
    assert result.state.agents["A"].hp == expected_hp
    damage_events = _events_of(result, "damage")
    if expected_hp == 60:
        assert damage_events[0]["payload"]["cause"] == "poison"
    else:
        assert damage_events == []


def test_poison_true_cause_is_in_audit_event_but_not_in_observation() -> None:
    engine = WorldEngine(WorldConfig(hunger_per_tick=0, poison_damage=100))
    state = engine.initial_state(
        1,
        ["A", "B"],
        starting_positions={"A": "valley_a/grove", "B": "valley_a/grove"},
        weather=WeatherState(current="rain", recent=["rain"]),
        enable_false_decoy=False,
    )
    state = _put_in_inventory(state, "A", "red_berry")
    result = engine.step(
        state,
        {
            "A": {"agent_id": "A", "type": "eat", "item": "red_berry"},
            "B": {"agent_id": "B", "type": "noop"},
        },
    )
    assert set(result.observations) == {"B"}
    observation_dump = result.observations["B"].model_dump(mode="json")
    assert observation_dump["events"] == [
        {
            "type": "death",
            "agent_id": "A",
            "cause_visible": "sudden_illness",
            "cue": None,
        }
    ]
    serialized = json.dumps(observation_dump, sort_keys=True)
    assert "poison" not in serialized
    assert "false_decoy" not in serialized
    assert _events_of(result, "death")[0]["payload"]["cause"] == "poison"


@pytest.mark.parametrize(("seed", "collapsed"), [(2, True), (0, False)])
def test_deep_dig_collapse_probability_uses_seeded_pcg64(seed: int, collapsed: bool) -> None:
    # PCG64's first draws for these seeds fall respectively below and above 0.4.
    engine = WorldEngine(WorldConfig(hunger_per_tick=0))
    state = engine.initial_state(seed, ["A"], enable_false_decoy=False)
    result = engine.step(state, {"A": {"agent_id": "A", "type": "dig", "depth": 4}})
    assert bool(_events_of(result, "tunnel_collapsed")) is collapsed
    assert result.state.agents["A"].hp == (40 if collapsed else 100)
    assert result.state.rng_state != state.rng_state


def test_multiple_deep_dig_trials_are_agent_order_invariant() -> None:
    engine = WorldEngine(WorldConfig(hunger_per_tick=0))
    state = engine.initial_state(77, ["C", "A", "B"], enable_false_decoy=False)
    actions = {
        agent_id: {"agent_id": agent_id, "type": "dig", "depth": 5} for agent_id in ["A", "B", "C"]
    }
    first = engine.step(state, actions)
    second = engine.step(state, dict(reversed(list(actions.items()))))
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_false_decoy_is_tracked_observable_and_has_no_mechanical_or_rng_effect() -> None:
    engine = WorldEngine(WorldConfig(hunger_per_tick=0))
    with_decoy = engine.initial_state(91, ["A"], enable_false_decoy=True)
    without_decoy = engine.initial_state(91, ["A"], enable_false_decoy=False)
    action = {"A": {"agent_id": "A", "type": "noop"}}

    decoy_result = engine.step(with_decoy, action)
    control_result = engine.step(without_decoy, action)

    assert decoy_result.state.false_decoy is not None
    assert decoy_result.state.false_decoy.emitted_ticks == (1,)
    assert len(_events_of(decoy_result, "false_decoy_emitted")) == 1
    assert decoy_result.effects == control_result.effects
    assert decoy_result.state.agents == control_result.state.agents
    assert decoy_result.state.resources == control_result.state.resources
    assert decoy_result.state.weather == control_result.state.weather
    assert decoy_result.state.rng_state == control_result.state.rng_state

    observation = decoy_result.observations["A"].model_dump(mode="json")
    assert observation["events"] == [
        {
            "type": "environmental_cue",
            "agent_id": None,
            "cause_visible": None,
            "cue": "raven",
        }
    ]
    serialized = json.dumps(observation)
    assert "false_decoy" not in serialized
    assert "raven_death_omen" not in serialized


def test_false_decoy_engineers_a_harmless_same_location_death_coincidence() -> None:
    engine = WorldEngine(WorldConfig(hunger_per_tick=0))
    state = engine.initial_state(
        91,
        ["A", "B"],
        starting_positions={"A": "valley_a/grove", "B": "valley_a/grove"},
        enable_false_decoy=True,
        false_decoy_birth_tick=1,
    )
    state = _replace_state(
        state,
        lambda payload: payload["agents"]["A"].__setitem__("max_age", 1),
    )
    result = engine.step(
        state,
        {
            "A": {"agent_id": "A", "type": "noop"},
            "B": {"agent_id": "B", "type": "noop"},
        },
    )

    death = _events_of(result, "death")[0]
    cue = _events_of(result, "false_decoy_emitted")[0]
    assert cue["payload"]["location"] == death["payload"]["location"]
    assert cue["payload"]["engineered_coincidence"] is True
    assert [event.type for event in result.observations["B"].events] == [
        "death",
        "environmental_cue",
    ]
    assert result.effects == WorldEngine(WorldConfig(hunger_per_tick=0)).step(
        _replace_state(
            state,
            lambda payload: payload.__setitem__("false_decoy", None),
        ),
        {
            "A": {"agent_id": "A", "type": "noop"},
            "B": {"agent_id": "B", "type": "noop"},
        },
    ).effects


def test_death_and_decoy_cue_are_visible_from_a_neighboring_location() -> None:
    engine = WorldEngine(WorldConfig(hunger_per_tick=0))
    state = engine.initial_state(
        91,
        ["A", "B"],
        starting_positions={"A": "valley_a/grove", "B": "valley_a/cave"},
        enable_false_decoy=True,
        false_decoy_birth_tick=1,
    )
    state = _replace_state(
        state,
        lambda payload: payload["agents"]["A"].__setitem__("max_age", 1),
    )
    result = engine.step(
        state,
        {
            "A": {"agent_id": "A", "type": "noop"},
            "B": {"agent_id": "B", "type": "noop"},
        },
    )

    assert [event.type for event in result.observations["B"].events] == [
        "death",
        "environmental_cue",
    ]


def test_hunger_age_starvation_and_old_age_deaths() -> None:
    starvation_engine = WorldEngine(
        WorldConfig(hunger_per_tick=100, starvation_damage=100, max_age=50)
    )
    starvation_state = starvation_engine.initial_state(1, ["A"], enable_false_decoy=False)
    starvation = starvation_engine.step(starvation_state, {"A": {"agent_id": "A", "type": "noop"}})
    agent = starvation.state.agents["A"]
    assert (agent.age, agent.hunger, agent.alive, agent.death_cause) == (
        1,
        100,
        False,
        "starvation",
    )
    assert starvation.observations == {}

    age_engine = WorldEngine(WorldConfig(max_age=1, hunger_per_tick=0))
    age_state = age_engine.initial_state(1, ["A"], enable_false_decoy=False)
    old_age = age_engine.step(age_state, {"A": {"agent_id": "A", "type": "noop"}})
    assert old_age.state.agents["A"].death_cause == "old_age"
    assert _events_of(old_age, "death")[0]["payload"]["cause_visible"] == ("natural_death")


def test_starvation_can_be_configured_as_nonlethal_pressure() -> None:
    engine = WorldEngine(
        WorldConfig(
            hunger_per_tick=100,
            starvation_damage=100,
            starvation_lethal=False,
            max_age=50,
        )
    )
    state = engine.initial_state(1, ["A"], enable_false_decoy=False)
    result = engine.step(state, {"A": {"agent_id": "A", "type": "noop"}})

    agent = result.state.agents["A"]
    assert (agent.hp, agent.hunger, agent.alive, agent.death_cause) == (1, 100, True, None)
    assert not _events_of(result, "death")
    assert _events_of(result, "damage")[0]["payload"] == {
        "agent_id": "A",
        "amount": 99,
        "cause": "starvation",
        "location": "valley_a/grove",
    }


def test_collapse_can_be_configured_as_nonlethal_pressure() -> None:
    # PCG64 seed 2 makes the first eligible collapse trial succeed.
    engine = WorldEngine(
        WorldConfig(
            hunger_per_tick=0,
            collapse_damage=100,
            collapse_lethal=False,
        )
    )
    state = engine.initial_state(2, ["A"], enable_false_decoy=False)
    result = engine.step(
        state,
        {"A": {"agent_id": "A", "type": "dig", "depth": 4}},
    )

    agent = result.state.agents["A"]
    assert (agent.hp, agent.alive, agent.death_cause) == (1, True, None)
    assert _events_of(result, "tunnel_collapsed")
    assert not _events_of(result, "death")


def test_observations_are_personal_dry_and_same_location_only() -> None:
    engine = WorldEngine(WorldConfig(hunger_per_tick=0))
    state = engine.initial_state(
        4,
        ["A", "B", "C"],
        starting_positions={
            "A": "valley_a/grove",
            "B": "valley_a/grove",
            "C": "valley_b/grove",
        },
        enable_false_decoy=False,
    )
    result = engine.step(
        state,
        {
            "A": {"agent_id": "A", "type": "dig", "depth": 2},
            "B": {"agent_id": "B", "type": "noop"},
            "C": {"agent_id": "C", "type": "forage", "resource": "root"},
        },
    )
    b_visible = [item.model_dump(mode="json") for item in result.observations["B"].visible]
    assert result.observations["B"].you.neighbors == ("valley_a/cave",)
    assert b_visible == [
        {
            "agent_id": "A",
            "action": "dig",
            "destination": None,
            "resource": None,
            "item": None,
            "depth": 2,
        }
    ]
    assert result.observations["A"].visible == ()
    assert result.observations["C"].visible == ()
    for observation in result.observations.values():
        assert isinstance(observation, Observation)
        assert observation.tick == 1
        assert observation.weather == "clear"


def test_rng_draw_index_advances_only_rng_and_is_repeatable() -> None:
    engine = WorldEngine()
    state = engine.initial_state(717, ["A"], enable_false_decoy=False)
    first_state, first_index = engine.rng_draw_index(state, 7)
    second_state, second_index = engine.draw_index(state, 7)

    assert 0 <= first_index < 7
    assert first_index == second_index
    assert first_state == second_state
    assert first_state.tick == state.tick
    assert first_state.rng_state != state.rng_state
    assert first_state.agents == state.agents
    assert state == engine.initial_state(717, ["A"], enable_false_decoy=False)
    with pytest.raises(ValueError):
        engine.rng_draw_index(state, 0)
    with pytest.raises(ValueError):
        engine.rng_draw_index(state, True)


def test_spawn_agent_records_lineage_and_explicit_location_consumes_no_rng() -> None:
    engine = WorldEngine(WorldConfig(max_age=17))
    state = engine.initial_state(12, ["parent"], enable_false_decoy=False)
    spawned, event = engine.spawn_agent(
        state,
        "child",
        generation=1,
        inherited_legacy_ids=["legacy_1", "legacy_2"],
        location="valley_b/grove",
    )
    child = spawned.agents["child"]
    assert child == AgentState(
        agent_id="child",
        loc="valley_b/grove",
        max_age=17,
        generation=1,
        birth_tick=0,
        inherited_legacy_ids=["legacy_1", "legacy_2"],
    )
    assert spawned.rng_state == state.rng_state
    assert event == {
        "type": "agent_spawned",
        "payload": {
            "agent_id": "child",
            "generation": 1,
            "location": "valley_b/grove",
            "inherited_legacy_ids": ["legacy_1", "legacy_2"],
        },
    }
    assert "child" not in state.agents


def test_spawn_without_location_is_deterministic_and_checkpoints_rng() -> None:
    engine = WorldEngine()
    state = engine.initial_state(13, [], enable_false_decoy=False)
    first, first_event = engine.spawn_agent(state, "child", 0)
    second, second_event = engine.spawn_agent(state, "child", 0)
    assert first == second
    assert first_event == second_event
    assert first.rng_state != state.rng_state
    assert first.agents["child"].loc in {"valley_a/grove", "valley_b/grove"}


def test_optional_weather_rng_is_checkpointed_and_applies_to_next_tick() -> None:
    engine = WorldEngine(WorldConfig(hunger_per_tick=0, rain_probability=1.0))
    state = engine.initial_state(3, ["A"], enable_false_decoy=False)
    result = engine.step(state, {"A": {"agent_id": "A", "type": "noop"}})
    assert state.weather.current == "clear"
    assert result.state.weather.current == "rain"
    assert result.state.rng_state != state.rng_state
    assert result.observations["A"].weather == "rain"


def test_step_result_records_are_json_safe_timestamp_free_and_round_trip() -> None:
    engine = WorldEngine(WorldConfig(hunger_per_tick=0))
    state = engine.initial_state(2, ["A"], enable_false_decoy=True)
    result = engine.step(state, {"A": {"agent_id": "A", "type": "dig", "depth": 4}})
    dumped = result.model_dump(mode="json")
    json.dumps(dumped, allow_nan=False)
    restored = type(result).model_validate(dumped)
    assert restored == result
    for collection in (result.events, result.effects):
        for record in collection:
            assert set(record) == {"type", "payload"}
            assert "timestamp" not in record
            assert "timestamp" not in record["payload"]
