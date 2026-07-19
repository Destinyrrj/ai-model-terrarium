from __future__ import annotations

from pathlib import Path

import pytest

from terrarium.config import RunConfig, load_config
from terrarium.domain import Action, WorldState
from terrarium.events import json_sha256
from terrarium.manifest import RunManifest
from terrarium.replay import ReplayIntegrityError, mechanics_config, replay_run
from terrarium.storage import EventStore
from terrarium.world import WorldEngine


def _config(run_id: str) -> RunConfig:
    data = load_config(Path(__file__).parents[1] / "configs" / "mvp.yaml").model_dump(mode="json")
    data["run_id"] = run_id
    data["population"]["size"] = 2
    data["population"]["generations"] = 2
    return RunConfig.model_validate(data)


def _sealed_log(
    root: Path,
    *,
    accepted_actions: list[dict[str, object]] | None = None,
    after_state_hash: str | None = None,
    tamper_world_outputs: bool = False,
    tamper_checkpoint_world: bool = False,
) -> RunConfig:
    config = _config("replay-test")
    manifest = RunManifest.from_config(config)
    manifest.write_new(root / "manifest.json")
    engine = WorldEngine(mechanics_config(config))
    initial = engine.initial_state(config.seed, ["agent-0", "agent-1"])
    actions = {
        agent_id: Action(agent_id=agent_id, type="noop") for agent_id in sorted(initial.agents)
    }
    result = engine.step(initial, actions)
    recorded_actions = accepted_actions or [
        action.model_dump(mode="json", exclude_none=True) for action in actions.values()
    ]
    recorded_after = after_state_hash or result.state.state_hash()
    output_hashes = _world_output_hashes(result.model_dump(mode="json"))
    if tamper_world_outputs:
        output_hashes["events_sha256"] = "f" * 64
    checkpoint_world = result.state
    if tamper_checkpoint_world:
        world_data = result.state.model_dump(mode="json")
        world_data["resources"]["valley_a/grove"]["root"] += 1
        checkpoint_world = WorldState.model_validate(world_data)
    with EventStore(root, config.run_id) as store:
        store.commit_tick(
            0,
            [
                {
                    "type": "run_started",
                    "payload": {
                        "initial_world": initial.model_dump(mode="json"),
                        "manifest_sha256": manifest.sha256(),
                    },
                }
            ],
            _checkpoint(config, initial),
        )
        store.commit_tick(
            1,
            [
                {
                    "type": "world_step",
                    "payload": {
                        "before_state_hash": initial.state_hash(),
                        "after_state_hash": recorded_after,
                        "accepted_actions": recorded_actions,
                    },
                },
                {
                    "type": "world_outputs",
                    "payload": output_hashes,
                },
                {
                    "type": "lifecycle_step",
                    "payload": {
                        "before_state_hash": result.state.state_hash(),
                        "after_state_hash": result.state.state_hash(),
                        "operations": [],
                    },
                },
            ],
            _checkpoint(config, checkpoint_world),
        )
    return config


def _world_output_hashes(result: dict[str, object]) -> dict[str, str]:
    return {
        "events_sha256": json_sha256(result["events"]),
        "effects_sha256": json_sha256(result["effects"]),
        "observations_sha256": json_sha256(result["observations"]),
    }


def _checkpoint(config: RunConfig, world: WorldState) -> dict[str, object]:
    world_json = world.model_dump(mode="json")
    return {
        "schema_version": 2,
        "config_sha256": config.digest(),
        "world": world_json,
        "rng_state": world_json["rng_state"],
        "contexts": {},
        "lineages": {},
        "legacies": [],
        "budget": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "failures": 0},
    }


def test_valid_world_log_replays(tmp_path: Path) -> None:
    _sealed_log(tmp_path)
    summary = replay_run(tmp_path)
    assert summary.status == "ok"
    assert summary.ticks_replayed == 1
    assert summary.last_tick == 1


def test_replay_detects_tampered_accepted_action(tmp_path: Path) -> None:
    actions = [
        {"agent_id": "agent-0", "type": "dig", "depth": 4},
        {"agent_id": "agent-1", "type": "noop"},
    ]
    _sealed_log(tmp_path, accepted_actions=actions)
    with pytest.raises(ReplayIntegrityError, match="after-state hash mismatch"):
        replay_run(tmp_path)


def test_replay_detects_tampered_recorded_hash(tmp_path: Path) -> None:
    _sealed_log(tmp_path, after_state_hash="f" * 64)
    with pytest.raises(ReplayIntegrityError, match="after-state hash mismatch"):
        replay_run(tmp_path)


def test_replay_requires_full_sorted_action_batch(tmp_path: Path) -> None:
    actions = [{"agent_id": "agent-1", "type": "noop"}]
    _sealed_log(tmp_path, accepted_actions=actions)
    with pytest.raises(ReplayIntegrityError, match="every living agent"):
        replay_run(tmp_path)


def test_replay_detects_tampered_world_outputs(tmp_path: Path) -> None:
    _sealed_log(tmp_path, tamper_world_outputs=True)
    with pytest.raises(ReplayIntegrityError, match="world output hash mismatch"):
        replay_run(tmp_path)


def test_replay_rejects_unrecorded_lifecycle_world_edit(tmp_path: Path) -> None:
    _sealed_log(tmp_path, tamper_checkpoint_world=True)
    with pytest.raises(ReplayIntegrityError, match="deterministic lifecycle result"):
        replay_run(tmp_path)
