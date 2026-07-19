from __future__ import annotations

import json
from pathlib import Path

import pytest

from terrarium.config import RunConfig, load_config
from terrarium.orchestrator import ExperimentRunner
from terrarium.prompting import AgentContext
from terrarium.runtime import AdapterResult, DeterministicMockAdapter


def _small_config(run_id: str) -> RunConfig:
    data = load_config("configs/mvp.yaml").model_dump(mode="json")
    data["run_id"] = run_id
    data["population"]["size"] = 1
    data["population"]["generations"] = 1
    return RunConfig.model_validate(data)


@pytest.mark.asyncio
async def test_public_contexts_are_detached_snapshots(tmp_path: Path) -> None:
    runner = ExperimentRunner(_small_config("context-snapshot"), tmp_path / "run")
    try:
        agent_id = next(iter(runner.contexts))
        exposed = runner.contexts[agent_id]
        assert exposed.current_observation is not None
        exposed.current_observation["you"]["hp"] = 1
        exposed.transitions.append({"forged": True})

        authoritative = runner.contexts[agent_id]
        assert authoritative.current_observation is not None
        assert authoritative.current_observation["you"]["hp"] == 100
        assert authoritative.transitions == []
    finally:
        await runner.close()


@pytest.mark.asyncio
async def test_checked_mvp_seeds_a_raven_death_coincidence_with_a_live_witness(
    tmp_path: Path,
) -> None:
    data = load_config("configs/mvp.yaml").model_dump(mode="json")
    data["run_id"] = "observable-decoy"
    data["population"].update({"size": 3, "generations": 2, "lifespan_ticks": 8})
    config = RunConfig.model_validate(data)
    run_dir = tmp_path / "run"
    runner = ExperimentRunner(config, run_dir)
    try:
        summary = await runner.run(max_ticks=config.world.decoy_birth_tick)
        assert summary.world_tick == config.world.decoy_birth_tick
    finally:
        await runner.close()

    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    schedules = [event for event in events if event["type"] == "decoy_coincidence_scheduled"]
    assert 1 <= len(schedules) <= 2
    observations = [
        event["payload"]["observation"] for event in events if event["type"] == "observation"
    ]
    assert any(
        {item["type"] for item in observation["events"]} >= {"death", "environmental_cue"}
        for observation in observations
    )


class _CapturingAdapter(DeterministicMockAdapter):
    def __init__(self, context: AgentContext) -> None:
        super().__init__(context.agent_id, context.inherited_legacy_texts, seed=11)
        self.act_requests: list[dict[str, object]] = []
        self.deathbed_requests: list[dict[str, object]] = []

    async def act(self, observation: dict[str, object]) -> AdapterResult:
        self.act_requests.append(json.loads(json.dumps(observation)))
        return await super().act(observation)

    async def write_legacy(
        self,
        budget_tokens: int,
        context: dict[str, object] | None = None,
    ) -> AdapterResult:
        assert context is not None
        self.deathbed_requests.append(json.loads(json.dumps(context)))
        return await super().write_legacy(budget_tokens, context)


@pytest.mark.asyncio
async def test_agent_remembers_own_actions_and_lethal_visible_outcome_for_deathbed(
    tmp_path: Path,
) -> None:
    data = load_config("configs/mvp.yaml").model_dump(mode="json")
    data["run_id"] = "full-lifetime-memory"
    data["population"].update({"size": 1, "generations": 2, "lifespan_ticks": 3})
    config = RunConfig.model_validate(data)
    adapters: list[_CapturingAdapter] = []

    def factory(context: AgentContext) -> _CapturingAdapter:
        adapter = _CapturingAdapter(context)
        adapters.append(adapter)
        return adapter

    runner = ExperimentRunner(config, tmp_path / "run", adapter_factory=factory)
    try:
        summary = await runner.run(max_ticks=4)
        assert summary.completed
    finally:
        await runner.close()

    ancestor = adapters[0]
    assert len(ancestor.act_requests) == 3
    # Prompt N+1 contains the preceding own action and its resulting observation.
    second_prompt = ancestor.act_requests[1]
    assert second_prompt["memory"][-1]["action"]["type"] == "noop"
    assert second_prompt["memory"][-1]["result"]["tick"] == 1

    deathbed = ancestor.deathbed_requests[0]
    assert deathbed["initial_observation"]["tick"] == 0
    assert len(deathbed["observed_life"]) == config.population.lifespan_ticks
    assert [item["action"]["type"] for item in deathbed["observed_life"]] == [
        "noop",
        "noop",
        "noop",
    ]
    assert deathbed["observed_life"][-1]["result"] == {
        "tick": 3,
        "type": "death",
        "cause_visible": "natural_death",
    }
    serialized = json.dumps(deathbed, sort_keys=True)
    assert "old_age" not in serialized
    assert "death_cause" not in serialized
