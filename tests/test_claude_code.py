from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from terrarium.prompting import AgentContext, Persona
from terrarium.runtime.claude_code import ClaudeCodeAgentAdapter


def _context(*, resumed: bool = False) -> AgentContext:
    observation = {
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
    }
    context = AgentContext(
        agent_id="agent-1",
        lineage_id="lineage-1",
        persona=Persona(name="agent-1", temperament="careful"),
        current_observation=observation,
    )
    if resumed:
        context.record_transition(
            {"agent_id": "agent-1", "type": "noop"},
            {**observation, "tick": 1},
        )
    return context


def _fake_cli(tmp_path: Path) -> Path:
    executable = tmp_path / "fake-claude"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "json.load(sys.stdin)\n"
        "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
        "'structured_output':{'agent_id':'agent-1','type':'noop'},"
        "'usage':{'input_tokens':2,'cache_read_input_tokens':3,'output_tokens':5},"
        "'total_cost_usd':0.01}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


async def test_claude_code_adapter_is_tool_free_stateful_and_accounts_cache_tokens(
    tmp_path: Path,
) -> None:
    executable = _fake_cli(tmp_path)
    adapter = ClaudeCodeAgentAdapter(
        executable=str(executable),
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        model="test-model",
        run_id="test-run",
        context=_context(),
    )
    argv = adapter._argv({"type": "object"})
    assert "--safe-mode" in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert "--session-id" in argv

    result = await adapter.act(_context().act_envelope())
    assert result.ok
    assert result.payload == {"agent_id": "agent-1", "type": "noop"}
    assert result.usage == {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}
    assert "--resume" in adapter._argv({"type": "object"})
    await adapter.close()


def test_checkpointed_context_resumes_deterministic_session(tmp_path: Path) -> None:
    executable = _fake_cli(tmp_path)
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    fresh = ClaudeCodeAgentAdapter(
        executable=str(executable),
        executable_sha256=digest,
        model="m",
        run_id="r",
        context=_context(),
    )
    resumed = ClaudeCodeAgentAdapter(
        executable=str(executable),
        executable_sha256=digest,
        model="m",
        run_id="r",
        context=_context(resumed=True),
    )
    assert fresh.session_id == resumed.session_id
    assert "--resume" in resumed._argv({"type": "object"})
    asyncio.run(fresh.close())
    asyncio.run(resumed.close())
