"""Deterministic Terrarium world mechanics.

The engine is deliberately synchronous and contains no LLM calls.  An orchestrator
may collect intents concurrently, but must hand the resulting mapping to ``step``;
the engine validates and canonically orders it before any random draw.  Given the
same engine config, checkpoint, and intents, the next state and records are exact.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Annotated, Any

import numpy as np
from pydantic import Field, ValidationError

from terrarium.domain import (
    Action,
    AgentState,
    FalseDecoyState,
    Location,
    Observation,
    ObservationEvent,
    PCG64State,
    SelfObservation,
    StepResult,
    StrictModel,
    VisibleAction,
    WeatherState,
    WorldState,
)


class WorldConfig(StrictModel):
    """Versioned mechanical constants supplied by the experiment config.

    ``rain_probability=None`` means weather is externally scheduled and the current
    value persists; otherwise one PCG64 draw at the end of every tick chooses the
    next tick's weather.  The hidden-rule MVP defaults are poison damage 40 and a
    deep-dig collapse probability of exactly 0.4.
    """

    max_age: Annotated[int, Field(strict=True, ge=1)] = 100
    hunger_per_tick: Annotated[int, Field(strict=True, ge=0, le=100)] = 5
    starvation_damage: Annotated[int, Field(strict=True, ge=1, le=100)] = 20
    starvation_lethal: bool = True
    food_relief: Annotated[int, Field(strict=True, ge=0, le=100)] = 30
    poison_damage: Annotated[int, Field(strict=True, ge=1, le=100)] = 40
    collapse_damage: Annotated[int, Field(strict=True, ge=1, le=100)] = 60
    collapse_probability: Annotated[float, Field(strict=True, ge=0.0, le=1.0)] = 0.4
    collapse_lethal: bool = True
    rain_probability: Annotated[float, Field(strict=True, ge=0.0, le=1.0)] | None = None


def _record(record_type: str, **payload: Any) -> dict[str, object]:
    """Construct the sole event/effect wire shape."""

    return {"type": record_type, "payload": payload}


def _default_locations() -> dict[str, Location]:
    locations = {
        "valley_a/grove": Location(id="valley_a/grove", neighbors=["valley_a/cave"]),
        "valley_a/cave": Location(id="valley_a/cave", neighbors=["valley_a/grove", "bridge"]),
        "bridge": Location(id="bridge", neighbors=["valley_a/cave", "valley_b/cave"]),
        "valley_b/cave": Location(id="valley_b/cave", neighbors=["bridge", "valley_b/grove"]),
        "valley_b/grove": Location(id="valley_b/grove", neighbors=["valley_b/cave"]),
    }
    return {location_id: locations[location_id] for location_id in sorted(locations)}


def _default_resources() -> dict[str, dict[str, int]]:
    return {
        "bridge": {},
        "valley_a/cave": {"root": 1},
        "valley_a/grove": {"red_berry": 4, "root": 3},
        "valley_b/cave": {"root": 1},
        "valley_b/grove": {"red_berry": 4, "root": 3},
    }


class WorldEngine:
    """Pure-mechanics facade over immutable ``WorldState`` checkpoints.

    Public methods never mutate their input state.  All stochastic methods restore a
    ``np.random.Generator(np.random.PCG64)`` from ``state.rng_state`` and return a new
    checkpoint containing the post-draw state.  No module-level or secondary RNG is
    used.
    """

    def __init__(self, config: WorldConfig | None = None) -> None:
        self.config = config if config is not None else WorldConfig()

    def initial_state(
        self,
        seed: int,
        agent_ids: Sequence[str],
        *,
        starting_positions: Mapping[str, str] | None = None,
        locations: Mapping[str, Location | Mapping[str, object]] | None = None,
        resources: Mapping[str, Mapping[str, int]] | None = None,
        weather: WeatherState | Mapping[str, object] | None = None,
        enable_false_decoy: bool = True,
        false_decoy_birth_tick: int = 1,
    ) -> WorldState:
        """Build tick zero without consuming an RNG draw.

        Agent IDs are canonicalized lexicographically, so sequence order cannot
        affect initial placement or state hashes.  By default agents alternate
        between the two grove spawn points, resources use the two-valley MVP map,
        and the harmless raven cue is born on tick 1.
        """

        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError("seed must be a non-negative integer")
        if isinstance(agent_ids, (str, bytes)) or not isinstance(agent_ids, Sequence):
            raise TypeError("agent_ids must be a sequence of IDs")
        canonical_ids = sorted(agent_ids)
        if len(canonical_ids) != len(set(canonical_ids)):
            raise ValueError("agent_ids must be unique")

        if locations is None:
            location_models = _default_locations()
        else:
            location_models = {
                location_id: (
                    location
                    if isinstance(location, Location)
                    else Location.model_validate(location)
                )
                for location_id, location in sorted(locations.items())
            }
        location_ids = sorted(location_models)
        if not location_ids:
            raise ValueError("at least one location is required")

        spawn_locations = (
            sorted(location_id for location_id in location_ids if location_id.endswith("/grove"))
            or location_ids
        )
        provided_positions = dict(starting_positions or {})
        unknown_position_agents = set(provided_positions) - set(canonical_ids)
        if unknown_position_agents:
            raise ValueError("starting_positions contains an unknown agent")

        agent_models: dict[str, AgentState] = {}
        for index, agent_id in enumerate(canonical_ids):
            loc = provided_positions.get(agent_id, spawn_locations[index % len(spawn_locations)])
            agent_models[agent_id] = AgentState(
                agent_id=agent_id,
                loc=loc,
                max_age=self.config.max_age,
                birth_tick=0,
            )

        if resources is None:
            if set(location_models) == set(_default_locations()):
                resource_data = _default_resources()
            else:
                resource_data = {location_id: {} for location_id in location_ids}
        else:
            resource_data = {
                location_id: dict(location_resources)
                for location_id, location_resources in sorted(resources.items())
            }

        if weather is None:
            weather_model = WeatherState()
        elif isinstance(weather, WeatherState):
            weather_model = weather
        else:
            weather_model = WeatherState.model_validate(weather)

        decoy: FalseDecoyState | None = None
        if enable_false_decoy:
            decoy_location = (
                "valley_a/grove" if "valley_a/grove" in location_models else location_ids[0]
            )
            decoy = FalseDecoyState(
                location=decoy_location,
                birth_tick=false_decoy_birth_tick,
            )

        generator = np.random.Generator(np.random.PCG64(seed))
        return WorldState(
            tick=0,
            locations=location_models,
            agents=agent_models,
            resources=resource_data,
            weather=weather_model,
            rng_state=PCG64State.from_generator(generator),
            false_decoy=decoy,
        )

    def rng_draw_index(self, state: WorldState, upper: int) -> tuple[WorldState, int]:
        """Draw uniformly from ``range(upper)`` and checkpoint the advanced PCG64.

        This is the safe RNG hook for inheritance/lifecycle selection.  It does not
        change the world tick or any mechanical field other than ``rng_state``.
        """

        if isinstance(upper, bool) or not isinstance(upper, int) or upper < 1:
            raise ValueError("upper must be a positive integer")
        generator = state.rng_state.to_generator()
        index = int(generator.integers(0, upper))
        return self._with_rng(state, generator), index

    def draw_index(self, state: WorldState, upper: int) -> tuple[WorldState, int]:
        """Backward-compatible spelling of :meth:`rng_draw_index`."""

        return self.rng_draw_index(state, upper)

    def spawn_agent(
        self,
        state: WorldState,
        agent_id: str,
        generation: int,
        inherited_legacy_ids: Sequence[str] = (),
        location: str | None = None,
    ) -> tuple[WorldState, dict[str, object]]:
        """Add a live agent and return ``(new_state, agent_spawned_event)``.

        If ``location`` is absent, one sorted grove (or, on custom maps, one sorted
        location) is selected through the state's PCG64 using ``rng_draw_index``.
        Legacy *IDs* are stored mechanically; legacy text never enters WorldState.
        """

        if agent_id in state.agents:
            raise ValueError(f"agent {agent_id!r} already exists")
        if isinstance(inherited_legacy_ids, (str, bytes)):
            raise TypeError("inherited_legacy_ids must be a sequence of IDs")

        next_state = state
        if location is None:
            choices = sorted(
                location_id for location_id in state.locations if location_id.endswith("/grove")
            ) or sorted(state.locations)
            next_state, index = self.rng_draw_index(state, len(choices))
            location = choices[index]

        data = next_state.model_dump(mode="json")
        new_agent = AgentState(
            agent_id=agent_id,
            loc=location,
            max_age=self.config.max_age,
            generation=generation,
            birth_tick=state.tick,
            inherited_legacy_ids=list(inherited_legacy_ids),
        )
        data["agents"][agent_id] = new_agent.model_dump(mode="json")
        spawned_state = WorldState.model_validate(data)
        event = _record(
            "agent_spawned",
            agent_id=agent_id,
            generation=generation,
            location=location,
            inherited_legacy_ids=list(inherited_legacy_ids),
        )
        return spawned_state, event

    def observations(
        self,
        state: WorldState,
        actions: Mapping[str, Action] | None = None,
        events: Sequence[Mapping[str, object]] = (),
    ) -> dict[str, Observation]:
        """Project a checkpoint into dry observations keyed by every living agent.

        Only same-location public actions, visible death consequences, and the
        harmless environmental cue are projected.  Validation errors, hidden rule
        identifiers, internal decoy IDs, damage causes, and ``death.payload.cause``
        are never copied.  This standalone form treats current positions as witness
        positions; ``step`` additionally preserves where each witness began the tick.
        """

        canonical_actions = dict(actions or {})
        for key, action in canonical_actions.items():
            if key != action.agent_id:
                raise ValueError("action mapping key must equal action.agent_id")
        witness_locations = {agent_id: agent.loc for agent_id, agent in state.agents.items()}
        action_locations = dict(witness_locations)
        return self._observations(
            state,
            canonical_actions,
            list(events),
            witness_locations=witness_locations,
            action_locations=action_locations,
        )

    def step(
        self,
        state: WorldState,
        intents: Mapping[str, Action | Mapping[str, object]],
    ) -> StepResult:
        """Resolve one simultaneous tick and return an atomic ``StepResult``.

        Invalid or missing intents become no-ops with a fixed-code event.  The input
        mapping's insertion/completion order is discarded.  Conflict groups and
        claimants are sorted before the PCG64 winner draw; deep-dig trials are also
        drawn in sorted agent order.  Thus both state and semantic record order are
        invariant under permutations of the same intents.
        """

        if not isinstance(intents, Mapping):
            raise TypeError("intents must be a mapping keyed by agent_id")

        events: list[dict[str, object]] = []
        effects: list[dict[str, object]] = []
        actions = self._normalize_actions(state, intents, events)
        action_locations = {agent_id: agent.loc for agent_id, agent in state.agents.items()}
        witness_locations = dict(action_locations)

        data = state.model_dump(mode="json")
        agents: dict[str, dict[str, Any]] = data["agents"]
        resources: dict[str, dict[str, int]] = data["resources"]
        generator = state.rng_state.to_generator()

        # Each intent is resolved against the tick-start snapshot.  Moves are one-hop
        # and cannot influence the location of a forage performed in the same tick,
        # because an agent has exactly one action.
        for agent_id in sorted(actions):
            action = actions[agent_id]
            if action.type != "move":
                continue
            origin = action_locations[agent_id]
            destination = action.destination
            assert destination is not None
            if destination in state.locations[origin].neighbors:
                agents[agent_id]["loc"] = destination
                events.append(
                    _record(
                        "moved",
                        agent_id=agent_id,
                        origin=origin,
                        destination=destination,
                    )
                )
                effects.append(
                    _record(
                        "position_changed",
                        agent_id=agent_id,
                        before=origin,
                        after=destination,
                    )
                )
            else:
                events.append(
                    _record(
                        "move_failed",
                        agent_id=agent_id,
                        origin=origin,
                        destination=destination,
                        reason="not_adjacent",
                    )
                )

        forage_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        for agent_id in sorted(actions):
            action = actions[agent_id]
            if action.type == "forage":
                assert action.resource is not None
                forage_groups[(action_locations[agent_id], action.resource)].append(agent_id)

        for (location, resource), raw_claimants in sorted(forage_groups.items()):
            claimants = sorted(raw_claimants)
            available = resources[location].get(resource, 0)
            win_count = min(available, len(claimants))
            if 0 < win_count < len(claimants):
                chosen = generator.choice(len(claimants), size=win_count, replace=False)
                winner_ids = {claimants[int(index)] for index in chosen.tolist()}
                events.append(
                    _record(
                        "resource_conflict",
                        location=location,
                        resource=resource,
                        available_before=available,
                        claimants=claimants,
                        winners=sorted(winner_ids),
                    )
                )
            elif win_count == len(claimants):
                winner_ids = set(claimants)
            else:
                winner_ids = set()

            for agent_id in claimants:
                if agent_id not in winner_ids:
                    events.append(
                        _record(
                            "forage_failed",
                            agent_id=agent_id,
                            location=location,
                            resource=resource,
                            reason="unavailable",
                        )
                    )
                    continue
                before = agents[agent_id]["inventory"].get(resource, 0)
                agents[agent_id]["inventory"][resource] = before + 1
                resources[location][resource] = resources[location].get(resource, 0) - 1
                events.append(
                    _record(
                        "foraged",
                        agent_id=agent_id,
                        location=location,
                        resource=resource,
                        amount=1,
                    )
                )
                effects.append(
                    _record(
                        "inventory_changed",
                        agent_id=agent_id,
                        resource=resource,
                        before=before,
                        after=before + 1,
                    )
                )
                effects.append(
                    _record(
                        "resource_changed",
                        location=location,
                        resource=resource,
                        before=resources[location][resource] + 1,
                        after=resources[location][resource],
                    )
                )

        for agent_id in sorted(actions):
            action = actions[agent_id]
            if action.type != "eat":
                continue
            item = action.item
            assert item is not None
            inventory_before = agents[agent_id]["inventory"].get(item, 0)
            if inventory_before < 1:
                events.append(
                    _record(
                        "eat_failed",
                        agent_id=agent_id,
                        item=item,
                        reason="not_in_inventory",
                    )
                )
                continue

            agents[agent_id]["inventory"][item] = inventory_before - 1
            hunger_before = agents[agent_id]["hunger"]
            agents[agent_id]["hunger"] = max(0, hunger_before - self.config.food_relief)
            events.append(_record("ate", agent_id=agent_id, item=item))
            effects.append(
                _record(
                    "inventory_changed",
                    agent_id=agent_id,
                    resource=item,
                    before=inventory_before,
                    after=inventory_before - 1,
                )
            )
            effects.append(
                _record(
                    "hunger_changed",
                    agent_id=agent_id,
                    before=hunger_before,
                    after=agents[agent_id]["hunger"],
                    reason="ate",
                )
            )
            if item == "red_berry" and state.weather.rained_last(3):
                self._damage(
                    agents,
                    agent_id,
                    self.config.poison_damage,
                    "poison",
                    events,
                    effects,
                )

        # The probabilistic hidden rule draws exactly once per eligible digger in
        # canonical agent order.  Digging at depth <= 3 never draws and never caves in.
        for agent_id in sorted(actions):
            action = actions[agent_id]
            if action.type != "dig":
                continue
            depth = action.depth
            assert depth is not None
            location = action_locations[agent_id]
            events.append(_record("dug", agent_id=agent_id, location=location, depth=depth))
            if depth > 3 and generator.random() < self.config.collapse_probability:
                events.append(
                    _record(
                        "tunnel_collapsed",
                        agent_id=agent_id,
                        location=location,
                        depth=depth,
                    )
                )
                self._damage(
                    agents,
                    agent_id,
                    self.config.collapse_damage,
                    "collapsed_tunnel",
                    events,
                    effects,
                    lethal=self.config.collapse_lethal,
                )

        # Hunger and age advance after action/rule effects.  A rule death is terminal
        # for the tick; survivors can then die from starvation, followed by old age.
        for agent_id in sorted(agents):
            agent = agents[agent_id]
            if not agent["alive"]:
                continue
            age_before = agent["age"]
            hunger_before = agent["hunger"]
            agent["age"] = age_before + 1
            agent["hunger"] = min(100, hunger_before + self.config.hunger_per_tick)
            effects.append(
                _record(
                    "age_changed",
                    agent_id=agent_id,
                    before=age_before,
                    after=agent["age"],
                )
            )
            if agent["hunger"] != hunger_before:
                effects.append(
                    _record(
                        "hunger_changed",
                        agent_id=agent_id,
                        before=hunger_before,
                        after=agent["hunger"],
                        reason="tick",
                    )
                )
            if agent["hunger"] >= 100:
                self._damage(
                    agents,
                    agent_id,
                    self.config.starvation_damage,
                    "starvation",
                    events,
                    effects,
                    lethal=self.config.starvation_lethal,
                )
            if agent["alive"] and agent["age"] >= agent["max_age"]:
                self._kill(
                    agents,
                    agent_id,
                    "old_age",
                    events,
                    effects,
                )

        next_tick = state.tick + 1
        decoy = state.false_decoy
        death_locations = [
            str(record["payload"]["location"])
            for record in events
            if record["type"] == "death" and isinstance(record["payload"], dict)
        ]
        first_cue_due = (
            decoy is not None
            and next_tick >= decoy.birth_tick
            and not decoy.emitted_ticks
        )
        engineered_coincidence_due = (
            decoy is not None
            and next_tick >= decoy.birth_tick
            and bool(death_locations)
            and len(decoy.emitted_ticks) < 2
            and next_tick not in decoy.emitted_ticks
        )
        if decoy is not None and (first_cue_due or engineered_coincidence_due):
            # The cue has no mechanical effect.  At most one later emission is
            # deliberately colocated with an independently resolved death to seed
            # the false raven/omen hypothesis requested by the experiment design.
            emission_location = death_locations[0] if death_locations else decoy.location
            events.append(
                _record(
                    "false_decoy_emitted",
                    decoy_id=decoy.decoy_id,
                    birth_tick=decoy.birth_tick,
                    emitted_tick=next_tick,
                    location=emission_location,
                    engineered_coincidence=bool(death_locations),
                )
            )
            data["false_decoy"]["location"] = emission_location
            data["false_decoy"]["emitted_ticks"] = [*decoy.emitted_ticks, next_tick]

        # Optional stochastic weather is selected for the *next* tick only after all
        # current-tick hidden rules.  It uses the same restored generator/checkpoint.
        next_weather = state.weather.current
        if self.config.rain_probability is not None:
            next_weather = "rain" if generator.random() < self.config.rain_probability else "clear"
        data["weather"] = state.weather.advance(next_weather).model_dump(mode="json")
        data["tick"] = next_tick
        data["rng_state"] = PCG64State.from_generator(generator).model_dump(mode="json")
        next_state = WorldState.model_validate(data)

        observations = self._observations(
            next_state,
            actions,
            events,
            witness_locations=witness_locations,
            action_locations=action_locations,
        )
        return StepResult(
            state=next_state,
            events=events,
            effects=effects,
            observations=observations,
        )

    def _normalize_actions(
        self,
        state: WorldState,
        intents: Mapping[str, Action | Mapping[str, object]],
        events: list[dict[str, object]],
    ) -> dict[str, Action]:
        """Validate in canonical order, replacing every bad/missing intent by noop."""

        non_string_keys = sum(not isinstance(key, str) for key in intents)
        if non_string_keys:
            events.append(
                _record(
                    "invalid_intent_keys",
                    count=non_string_keys,
                    reason="key_must_be_agent_id",
                )
            )
        string_keys = {key for key in intents if isinstance(key, str)}
        living_ids = {agent_id for agent_id, agent in state.agents.items() if agent.alive}
        actions: dict[str, Action] = {}
        for agent_id in sorted(living_ids):
            if agent_id not in string_keys:
                events.append(_record("missing_action", agent_id=agent_id, replacement="noop"))
                actions[agent_id] = Action(agent_id=agent_id, type="noop")
                continue
            raw = intents[agent_id]
            try:
                action = raw if isinstance(raw, Action) else Action.model_validate(raw)
            except (ValidationError, TypeError, ValueError):
                events.append(
                    _record(
                        "invalid_action",
                        agent_id=agent_id,
                        reason="schema_invalid",
                        replacement="noop",
                    )
                )
                actions[agent_id] = Action(agent_id=agent_id, type="noop")
                continue
            if action.agent_id != agent_id:
                events.append(
                    _record(
                        "invalid_action",
                        agent_id=agent_id,
                        reason="agent_id_mismatch",
                        replacement="noop",
                    )
                )
                actions[agent_id] = Action(agent_id=agent_id, type="noop")
                continue
            actions[agent_id] = action

        for agent_id in sorted(string_keys - living_ids):
            reason = "dead_agent" if agent_id in state.agents else "unknown_agent"
            events.append(_record("ignored_action", agent_id=agent_id, reason=reason))
        return actions

    def _observations(
        self,
        state: WorldState,
        actions: Mapping[str, Action],
        events: Sequence[Mapping[str, object]],
        *,
        witness_locations: Mapping[str, str],
        action_locations: Mapping[str, str],
    ) -> dict[str, Observation]:
        result: dict[str, Observation] = {}
        for observer_id in sorted(state.agents):
            observer = state.agents[observer_id]
            if not observer.alive:
                continue
            witness_location = witness_locations.get(observer_id, observer.loc)
            visible: list[VisibleAction] = []
            for actor_id in sorted(actions):
                action = actions[actor_id]
                if (
                    actor_id == observer_id
                    or action.type == "noop"
                    or action_locations.get(actor_id) != witness_location
                ):
                    continue
                visible.append(VisibleAction.from_action(action))

            observed_events: list[ObservationEvent] = []
            for record in events:
                if set(record) != {"type", "payload"}:
                    continue
                record_type = record.get("type")
                payload = record.get("payload")
                if not isinstance(payload, Mapping):
                    continue
                event_location = payload.get("location")
                nearby_locations = {
                    witness_location,
                    *state.locations[witness_location].neighbors,
                }
                if event_location not in nearby_locations:
                    continue
                if record_type == "death":
                    observed_events.append(
                        ObservationEvent(
                            type="death",
                            agent_id=payload["agent_id"],
                            cause_visible=payload["cause_visible"],
                        )
                    )
                elif record_type == "false_decoy_emitted":
                    # Crucially, neither the internal record type nor decoy_id crosses
                    # the observation boundary.
                    observed_events.append(ObservationEvent(type="environmental_cue", cue="raven"))

            result[observer_id] = Observation(
                tick=state.tick,
                weather=state.weather.current,
                you=SelfObservation(
                    hp=observer.hp,
                    hunger=observer.hunger,
                    age=observer.age,
                    loc=observer.loc,
                    neighbors=state.locations[observer.loc].neighbors,
                    inventory=dict(observer.inventory),
                ),
                visible=visible,
                events=observed_events,
            )
        return result

    @staticmethod
    def _damage(
        agents: dict[str, dict[str, Any]],
        agent_id: str,
        amount: int,
        cause: str,
        events: list[dict[str, object]],
        effects: list[dict[str, object]],
        *,
        lethal: bool = True,
    ) -> None:
        agent = agents[agent_id]
        if not agent["alive"]:
            return
        hp_before = agent["hp"]
        hp_floor = 0 if lethal else 1
        hp_after = max(hp_floor, hp_before - amount)
        agent["hp"] = hp_after
        actual_amount = hp_before - hp_after
        events.append(
            _record(
                "damage",
                agent_id=agent_id,
                amount=actual_amount,
                cause=cause,
                location=agent["loc"],
            )
        )
        effects.append(
            _record(
                "health_changed",
                agent_id=agent_id,
                before=hp_before,
                after=hp_after,
                cause=cause,
            )
        )
        if hp_after == 0:
            WorldEngine._kill(agents, agent_id, cause, events, effects)

    @staticmethod
    def _kill(
        agents: dict[str, dict[str, Any]],
        agent_id: str,
        cause: str,
        events: list[dict[str, object]],
        effects: list[dict[str, object]],
    ) -> None:
        agent = agents[agent_id]
        if not agent["alive"]:
            return
        visible_causes = {
            "poison": "sudden_illness",
            "collapsed_tunnel": "collapsed_tunnel",
            "starvation": "wasting",
            "old_age": "natural_death",
        }
        agent["alive"] = False
        agent["hp"] = 0
        agent["death_cause"] = cause
        events.append(
            _record(
                "death",
                agent_id=agent_id,
                cause=cause,
                cause_visible=visible_causes[cause],
                location=agent["loc"],
                age=agent["age"],
            )
        )
        effects.append(_record("agent_died", agent_id=agent_id, cause=cause))

    @staticmethod
    def _with_rng(state: WorldState, generator: np.random.Generator) -> WorldState:
        data = state.model_dump(mode="json")
        data["rng_state"] = PCG64State.from_generator(generator).model_dump(mode="json")
        return WorldState.model_validate(data)
