from __future__ import annotations

import os
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path

import pytest

from terrarium.budget import BudgetExhausted, BudgetGovernor
from terrarium.config import RunConfig, load_config
from terrarium.manifest import RunManifest


def test_manifest_roundtrip_and_drift_refusal(tmp_path: Path) -> None:
    config = load_config("configs/mvp.yaml")
    manifest = RunManifest.from_config(config)
    path = tmp_path / "manifest.json"
    manifest.write_new(path)
    manifest.write_new(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert manifest.tokenizer == "cl100k_base"
    assert manifest.schema_version == 2
    assert manifest.argv_file_sha256 == {}
    assert len(manifest.prompt_template_sha256) == 64
    assert len(manifest.package_code_sha256) == 64
    assert manifest.dependency_lock_sha256 is not None
    changed = manifest.model_copy(update={"model_id": "different"})
    with pytest.raises(RuntimeError, match="drift"):
        changed.write_new(path)


def test_manifest_concurrent_publish_never_overwrites_winner(tmp_path: Path) -> None:
    base = RunManifest.from_config(load_config("configs/mvp.yaml"))
    alternate = base.model_copy(update={"model_id": "different"})
    destination = tmp_path / "manifest.json"

    def publish(manifest: RunManifest) -> str:
        try:
            manifest.write_new(destination)
        except RuntimeError:
            return "drift"
        return "winner"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(publish, (base, alternate)))
    assert sorted(outcomes) == ["drift", "winner"]
    winner = RunManifest.model_validate_json(destination.read_bytes())
    assert winner.canonical_bytes() in {base.canonical_bytes(), alternate.canonical_bytes()}


def test_manifest_refuses_existing_symlink(tmp_path: Path) -> None:
    manifest = RunManifest.from_config(load_config("configs/mvp.yaml"))
    target = tmp_path / "target.json"
    target.write_bytes(manifest.canonical_bytes())
    destination = tmp_path / "manifest.json"
    os.symlink(target, destination)

    with pytest.raises(OSError):
        manifest.write_new(destination)


def test_manifest_seals_absolute_interpreter_script_and_replay_environment(
    tmp_path: Path,
) -> None:
    script = tmp_path / "adapter.py"
    script.write_text("print('{}')\n", encoding="utf-8")
    data = load_config("configs/mvp.yaml").model_dump(mode="json")
    executable = Path(sys.executable).resolve()
    data["runtime"].update(
        {
            "adapter": "subprocess",
            "argv": [str(executable), str(script)],
            "executable_sha256": sha256(executable.read_bytes()).hexdigest(),
        }
    )
    data["runtime"]["sandbox"] = {
        "backend": "process",
        "network": "none",
        "acknowledge_unsafe_host_execution": True,
        "external_egress_enforced": False,
    }
    manifest = RunManifest.from_config(RunConfig.model_validate(data))
    assert manifest.argv_file_sha256[str(script.resolve())] == sha256(
        script.read_bytes()
    ).hexdigest()
    manifest.assert_current_replay_environment()
    drifted = manifest.model_copy(update={"package_code_sha256": "0" * 64})
    with pytest.raises(RuntimeError, match="environment drift"):
        drifted.assert_current_replay_environment()

    script.write_text("print('{\"changed\": true}')\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="argv_file_sha256"):
        manifest.assert_current_replay_environment()


@pytest.mark.asyncio
async def test_budget_gates_calls_and_unknown_usage_fails_closed() -> None:
    config = load_config("configs/mvp.yaml")
    governor = BudgetGovernor(config.budget)
    await governor.reserve_call()
    await governor.record(input_tokens=10, output_tokens=2, success=True)
    assert governor.snapshot()["calls"] == 1
    await governor.reserve_call()
    with pytest.raises(BudgetExhausted, match="omitted"):
        await governor.record(input_tokens=None, output_tokens=None, success=False)


@pytest.mark.asyncio
async def test_reported_token_ceiling_blocks_every_later_dispatch() -> None:
    limits = load_config("configs/mvp.yaml").budget.model_copy(
        update={"max_input_tokens": 10, "max_output_tokens": 10}
    )
    governor = BudgetGovernor(limits)
    await governor.reserve_call()
    await governor.record(input_tokens=10, output_tokens=1, success=True)
    with pytest.raises(BudgetExhausted, match="input token"):
        await governor.reserve_call()
