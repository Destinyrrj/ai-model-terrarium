from __future__ import annotations

from pathlib import Path

import pytest

from terrarium.config import load_config
from terrarium.orchestrator import ExperimentRunner
from terrarium.replay import mechanics_config


@pytest.mark.asyncio
async def test_checked_mvp_keeps_starvation_and_collapse_nonlethal(
    tmp_path: Path,
) -> None:
    config = load_config("configs/mvp.yaml")
    replay_mechanics = mechanics_config(config)
    assert replay_mechanics.starvation_lethal is False
    assert replay_mechanics.collapse_lethal is False

    runner = ExperimentRunner(config, tmp_path / "run")
    try:
        assert runner.engine.config.starvation_lethal is False
        assert runner.engine.config.collapse_lethal is False
    finally:
        await runner.close()
