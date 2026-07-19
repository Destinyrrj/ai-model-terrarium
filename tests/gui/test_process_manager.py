from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from terrarium.cli import main as terrarium_main
from terrarium_gui.process_manager import ProcessManager
from terrarium_gui.registry import RunRegistry
from terrarium_gui.tools import ToolManager

MVP_YAML = Path("configs/mvp.yaml").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_sigint_then_resume_keeps_run_verifiable(tmp_path: Path, capsys: object) -> None:
    document = yaml.safe_load(MVP_YAML)
    document["run_id"] = "gui-interrupt-resume"
    document["population"]["generations"] = 1_000
    document["population"]["lifespan_ticks"] = 1_000
    config_path = tmp_path / "long.yaml"
    config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    registry = RunRegistry(tmp_path / "runs")
    manager = ProcessManager(
        registry,
        tmp_path / "artifacts",
        interrupt_timeout=3,
        terminate_timeout=2,
    )
    handle = await manager.start(
        run_name="interruptible", config_path=config_path, max_ticks=100_000
    )

    for _ in range(200):
        try:
            record = registry.inspect("interruptible", managed_running=handle.running)
        except FileNotFoundError:
            record = None
        if record is not None and record.last_tick is not None and handle.running:
            break
        await asyncio.sleep(0.025)
    else:
        raise AssertionError("run did not reach its first durable checkpoint")

    stopped = await manager.stop("interruptible")
    assert not stopped.running

    resumed = await manager.resume("interruptible", max_ticks=1)
    await resumed.process.wait()
    assert resumed._monitor is not None
    await resumed._monitor
    assert resumed.returncode == 0
    assert registry.inspect("interruptible").last_tick is not None

    assert terrarium_main(["verify", str(tmp_path / "runs" / "interruptible")]) == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert '"status": "ok"' in captured.out
    await manager.shutdown()


@pytest.mark.asyncio
async def test_artifact_child_symlinks_are_rejected_before_write(tmp_path: Path) -> None:
    registry = RunRegistry(tmp_path / "runs")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (artifacts / "runs").symlink_to(outside, target_is_directory=True)
    manager = ProcessManager(registry, artifacts)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(MVP_YAML, encoding="utf-8")
    with pytest.raises(RuntimeError, match="artifact directory is unsafe"):
        await manager.start(run_name="safe-name", config_path=config_path, max_ticks=0)
    assert list(outside.iterdir()) == []

    (artifacts / "runs").unlink()
    (artifacts / "tools").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="tool artifacts directory"):
        ToolManager(registry, manager, artifacts)
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_history_failure_never_orphans_or_blocks_child_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = yaml.safe_load(MVP_YAML)
    document["run_id"] = "gui-history-failure"
    document["population"]["generations"] = 1_000
    document["population"]["lifespan_ticks"] = 1_000
    config_path = tmp_path / "long.yaml"
    config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    manager = ProcessManager(
        RunRegistry(tmp_path / "runs"),
        tmp_path / "artifacts",
        interrupt_timeout=2,
        terminate_timeout=1,
    )

    async def broken_history(_event: str, _handle: object) -> None:
        raise OSError("simulated history fsync failure")

    monkeypatch.setattr(manager, "_append_history", broken_history)
    handle = await manager.start(
        run_name="history-failure",
        config_path=config_path,
        max_ticks=100_000,
    )
    assert handle._monitor is not None
    assert handle.history_error == "history_unavailable"

    stopped = await manager.stop("history-failure")
    assert not stopped.running
    assert stopped._monitor is not None and stopped._monitor.done()
    assert stopped.history_error == "history_unavailable"
