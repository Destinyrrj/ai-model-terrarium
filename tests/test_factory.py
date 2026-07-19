from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from terrarium.config import load_config
from terrarium.factory import build_adapter_factory
from terrarium.prompting import AgentContext, Persona
from terrarium.runtime.mock import DeterministicMockAdapter


def test_mock_factory_never_constructs_a_process() -> None:
    config = load_config("configs/mvp.yaml")
    factory = build_adapter_factory(config)
    adapter = factory(
        AgentContext(
            agent_id="A1",
            lineage_id="lineage_1",
            persona=Persona(name="A1", temperament="careful"),
            current_observation={
                "tick": 0,
                "weather": "clear",
                "you": {
                    "hp": 100,
                    "hunger": 0,
                    "age": 0,
                    "loc": "valley_a/grove",
                    "neighbors": ["valley_a/cave"],
                    "inventory": {},
                },
                "visible": [],
                "events": [],
            },
        )
    )
    assert isinstance(adapter, DeterministicMockAdapter)
    asyncio.run(adapter.close())


def test_subprocess_digest_is_rechecked(tmp_path: Path) -> None:
    executable = tmp_path / "adapter"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    config = load_config("configs/mvp.yaml")
    runtime = config.runtime.model_copy(
        update={
            "adapter": "subprocess",
            "argv": (str(executable),),
            "executable_sha256": "0" * 64,
            "sandbox": config.runtime.sandbox.model_copy(
                update={
                    "backend": "process",
                    "acknowledge_unsafe_host_execution": True,
                }
            ),
        }
    )
    changed = config.model_copy(update={"runtime": runtime})
    with pytest.raises(RuntimeError, match="changed"):
        build_adapter_factory(changed)
