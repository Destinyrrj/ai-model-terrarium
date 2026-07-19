from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

from terrarium.runtime import (
    AdapterStatus,
    CommandNotAllowed,
    CommandSpec,
    DangerousSandboxPolicy,
    DeterministicMockAdapter,
    EnvironmentPolicy,
    JSONFailureCode,
    RuntimeLimits,
    SandboxPolicy,
    SandboxUnavailable,
    SubprocessAgentAdapter,
    build_bubblewrap_argv,
    parse_structured_json,
    sanitize_terminal_text,
)
from terrarium.runtime.subprocess import _make_sandbox_directories, _uid_process_ceiling


def _python_command(source: str, *arguments: str) -> CommandSpec:
    return CommandSpec(
        argv=(sys.executable, "-I", "-c", source, *arguments),
        allowed_executables=frozenset({sys.executable}),
    )


def _unsafe_adapter(
    source: str,
    *,
    limits: RuntimeLimits | None = None,
    environment: EnvironmentPolicy | None = None,
    arguments: tuple[str, ...] = (),
) -> SubprocessAgentAdapter:
    return SubprocessAgentAdapter(
        _python_command(source, *arguments),
        sandbox=SandboxPolicy.unsafe_process(),
        limits=limits,
        environment=environment,
    )


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux procfs semantics")
def test_uid_process_limit_adds_agent_headroom_to_existing_uid_processes(
    tmp_path: Path,
) -> None:
    (tmp_path / "101").mkdir()
    (tmp_path / "202").mkdir()
    (tmp_path / "self").mkdir()

    assert _uid_process_ceiling(7, tmp_path) == 9


@pytest.mark.asyncio
async def test_mock_adapter_is_deterministic_and_domain_compatible() -> None:
    observation = {"tick": 7, "you": {"hp": 80, "loc": "grove"}}
    first = DeterministicMockAdapter("agent-7", ("red berries may hurt",), seed=19)
    second = DeterministicMockAdapter("agent-7", ("red berries may hurt",), seed=19)

    first_result = await first.act(observation)
    second_result = await second.act(observation)

    assert first_result == second_result
    assert first_result.payload == {"agent_id": "agent-7", "type": "noop"}
    assert first_result.usage is not None
    assert first_result.usage["total_tokens"] > 0
    assert not first_result.unsafe_host_execution


@pytest.mark.asyncio
async def test_mock_close_is_idempotent() -> None:
    adapter = DeterministicMockAdapter("agent-1")
    await adapter.close()
    await adapter.close()
    result = await adapter.act({"tick": 1})
    assert result.status is AdapterStatus.CLOSED
    assert result.usage == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


@pytest.mark.parametrize(
    ("document", "code"),
    [
        (b'{"a": 1, "a": 2}', JSONFailureCode.DUPLICATE_KEY),
        (b'{"a": NaN}', JSONFailureCode.NON_FINITE),
        (b'{"a": Infinity}', JSONFailureCode.NON_FINITE),
        (b'{"a": 1e999}', JSONFailureCode.NON_FINITE),
        (b'{"a":', JSONFailureCode.MALFORMED),
        (b'prose {"a": 1}', JSONFailureCode.MALFORMED),
        (b'```json\n{"a": 1}\n```', JSONFailureCode.MALFORMED),
        (b"\xff", JSONFailureCode.INVALID_UTF8),
        (b"[]", JSONFailureCode.WRONG_ROOT),
    ],
)
def test_strict_json_rejects_ambiguous_or_malformed_documents(
    document: bytes, code: JSONFailureCode
) -> None:
    result = parse_structured_json(document)
    assert not result.ok
    assert result.error is not None
    assert result.error.code is code


def test_strict_json_enforces_bytes_and_depth() -> None:
    oversized = parse_structured_json(b'{"value":"123456"}', max_bytes=8)
    too_deep = parse_structured_json(b'{"a":{"b":{"c":1}}}', max_depth=2)
    assert oversized.error is not None
    assert oversized.error.code is JSONFailureCode.TOO_LARGE
    assert too_deep.error is not None
    assert too_deep.error.code is JSONFailureCode.TOO_DEEP


def test_terminal_sanitizer_removes_escape_and_direction_controls() -> None:
    unsafe = "ok\x1b[2J\x1b]0;owned\x07\u202eevil\rnext\x00"
    assert sanitize_terminal_text(unsafe) == "okevil\nnext"


def test_command_requires_absolute_exact_allowlist() -> None:
    with pytest.raises(CommandNotAllowed):
        CommandSpec(argv=("python", "-c", "pass"), allowed_executables=frozenset({"python"}))
    with pytest.raises(CommandNotAllowed):
        CommandSpec(argv=(sys.executable, "-c", "pass"), allowed_executables=frozenset())


def test_dangerous_sandbox_policies_are_rejected() -> None:
    with pytest.raises(DangerousSandboxPolicy):
        SandboxPolicy(backend="process")
    with pytest.raises(DangerousSandboxPolicy):
        SandboxPolicy(
            backend="bubblewrap",
            network="inherit",
            acknowledge_network_inherit=True,
        )
    with pytest.raises(DangerousSandboxPolicy):
        SandboxPolicy(
            backend="bubblewrap",
            network="inherit",
            egress_proxy_marker="proxy-reviewed",
        )
    with pytest.raises(DangerousSandboxPolicy):
        SubprocessAgentAdapter(_python_command("pass"))


def test_missing_bubblewrap_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("terrarium.runtime.subprocess.shutil.which", lambda _: None)
    policy = SandboxPolicy(backend="bubblewrap")
    with pytest.raises(SandboxUnavailable):
        SubprocessAgentAdapter(_python_command("pass"), sandbox=policy)


@pytest.mark.skipif(
    not shutil.which("bwrap") or not Path("/usr/bin/python3").exists(),
    reason="bubblewrap builder needs system executables",
)
def test_bubblewrap_builder_has_no_repo_mount_and_private_tmpfs() -> None:
    policy = SandboxPolicy(backend="bubblewrap", bwrap_path=shutil.which("bwrap"))
    command = CommandSpec(
        argv=("/usr/bin/python3", "-c", "pass"),
        allowed_executables=frozenset({"/usr/bin/python3"}),
    )
    owner, directories = _make_sandbox_directories()
    try:
        environment = EnvironmentPolicy().build(directories, bubblewrap=True)
        argv = build_bubblewrap_argv(command, directories, environment, policy)
    finally:
        owner.cleanup()
    assert "--unshare-net" in argv
    assert "--disable-userns" in argv
    assert "--tmpfs" in argv
    assert "--bind" not in argv
    assert str(directories.root) not in argv
    assert not any(item.startswith("/workspace") for item in argv)


@pytest.mark.asyncio
async def test_subprocess_valid_envelope_and_missing_usage_is_unknown() -> None:
    with_usage = _unsafe_adapter(
        "import json,sys; json.load(sys.stdin); "
        "json.dump({'payload': {'type': 'noop'}, "
        "'usage': {'input_tokens': 2, 'output_tokens': 3}}, sys.stdout)"
    )
    result = await with_usage.act({"tick": 1})
    assert result.ok
    assert result.payload == {"type": "noop"}
    assert result.usage == {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}
    assert result.unsafe_host_execution
    assert result.metadata["filesystem_isolated"] is False
    await with_usage.close()

    without_usage = _unsafe_adapter(
        "import json,sys; json.load(sys.stdin); "
        "json.dump({'payload': {'type': 'noop'}}, sys.stdout)"
    )
    result = await without_usage.act({"tick": 1})
    assert result.ok
    assert result.usage is None
    await without_usage.close()


@pytest.mark.asyncio
async def test_secret_environment_is_not_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERRARIUM_TEST_SECRET", "should-not-cross-boundary")
    adapter = _unsafe_adapter(
        "import json,os,sys; json.load(sys.stdin); "
        "json.dump({'payload': {'secret': os.getenv('TERRARIUM_TEST_SECRET')}}, sys.stdout)"
    )
    result = await adapter.act({"tick": 1})
    assert result.ok
    assert result.payload == {"secret": None}
    assert "should-not-cross-boundary" not in (result.raw_text or "")
    await adapter.close()


@pytest.mark.asyncio
async def test_environment_requires_allowlist() -> None:
    with pytest.raises(DangerousSandboxPolicy):
        EnvironmentPolicy(values={"VISIBLE_VALUE": "yes"})

    environment = EnvironmentPolicy(
        allowed_names=frozenset({"VISIBLE_VALUE"}),
        values={"VISIBLE_VALUE": "yes"},
    )
    adapter = _unsafe_adapter(
        "import json,os,sys; json.load(sys.stdin); "
        "json.dump({'payload': {'value': os.environ['VISIBLE_VALUE']}}, sys.stdout)",
        environment=environment,
    )
    result = await adapter.act({"tick": 1})
    assert result.payload == {"value": "yes"}
    await adapter.close()


@pytest.mark.asyncio
async def test_cwd_home_and_tmp_are_ephemeral_and_private() -> None:
    adapter = _unsafe_adapter(
        "import json,os,stat,sys; json.load(sys.stdin); "
        "p=os.getcwd(); h=os.environ['HOME']; t=os.environ['TMPDIR']; "
        "json.dump({'payload': {'cwd': p, 'home': h, 'tmp': t, "
        "'cwd_mode': stat.S_IMODE(os.stat(p).st_mode), "
        "'home_mode': stat.S_IMODE(os.stat(h).st_mode), "
        "'tmp_mode': stat.S_IMODE(os.stat(t).st_mode)}}, sys.stdout)"
    )
    result = await adapter.act({"tick": 1})
    assert result.ok
    assert isinstance(result.payload, dict)
    root = Path(str(result.payload["cwd"])).parent
    assert root.name.startswith("terrarium-runtime-")
    assert result.payload["cwd_mode"] == 0o700
    assert result.payload["home_mode"] == 0o700
    assert result.payload["tmp_mode"] == 0o700
    assert not root.exists(), "ephemeral runtime directory must be removed after the call"
    await adapter.close()


@pytest.mark.asyncio
async def test_stdout_flood_is_killed_at_byte_limit() -> None:
    adapter = _unsafe_adapter(
        "import sys; sys.stdout.write('x' * 1000000); sys.stdout.flush()",
        limits=RuntimeLimits(
            timeout_seconds=2,
            termination_grace_seconds=0.05,
            max_stdout_bytes=128,
        ),
    )
    result = await adapter.act({"tick": 1})
    assert result.status is AdapterStatus.OUTPUT_LIMIT
    assert result.error is not None
    assert result.error.code == "stdout_limit"
    assert result.raw_text is not None
    assert len(result.raw_text) <= 128
    await adapter.close()


@pytest.mark.asyncio
async def test_stderr_flood_is_killed_at_byte_limit() -> None:
    adapter = _unsafe_adapter(
        "import sys; sys.stderr.write('x' * 1000000); sys.stderr.flush()",
        limits=RuntimeLimits(
            timeout_seconds=2,
            termination_grace_seconds=0.05,
            max_stderr_bytes=128,
        ),
    )
    result = await adapter.act({"tick": 1})
    assert result.status is AdapterStatus.OUTPUT_LIMIT
    assert result.error is not None
    assert result.error.code == "stderr_limit"
    await adapter.close()


@pytest.mark.asyncio
async def test_malformed_process_json_becomes_typed_failure() -> None:
    adapter = _unsafe_adapter("import sys; sys.stdout.write('{\"x\": NaN}')")
    result = await adapter.act({"tick": 1})
    assert result.status is AdapterStatus.INVALID_OUTPUT
    assert result.payload is None
    assert result.error is not None
    assert result.error.code == JSONFailureCode.NON_FINITE
    await adapter.close()


@pytest.mark.asyncio
async def test_input_size_and_depth_fail_before_process_launch(tmp_path: Path) -> None:
    marker = tmp_path / "launched"
    adapter = _unsafe_adapter(
        "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('yes')",
        arguments=(str(marker),),
        limits=RuntimeLimits(max_input_bytes=64, max_json_depth=3),
    )
    result = await adapter.act({"nested": {"more": {"too": {"deep": "x"}}}})
    assert result.status is AdapterStatus.SECURITY_ERROR
    assert not marker.exists()
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="process-group semantics are POSIX-specific")
async def test_timeout_terms_descendants_then_kills_group(tmp_path: Path) -> None:
    child_marker = tmp_path / "child-term"
    parent_marker = tmp_path / "parent-term"
    child_source = (
        "import pathlib,signal,sys,time; "
        "signal.signal(signal.SIGTERM, lambda *_: pathlib.Path(sys.argv[1]).write_text('term')); "
        "time.sleep(30)"
    )
    parent_source = (
        "import json,pathlib,signal,subprocess,sys,time; json.load(sys.stdin); "
        "signal.signal(signal.SIGTERM, lambda *_: pathlib.Path(sys.argv[2]).write_text('term')); "
        "subprocess.Popen([sys.executable, '-c', sys.argv[3], sys.argv[1]]); time.sleep(30)"
    )
    adapter = _unsafe_adapter(
        parent_source,
        arguments=(str(child_marker), str(parent_marker), child_source),
        limits=RuntimeLimits(timeout_seconds=0.25, termination_grace_seconds=0.2),
    )
    result = await adapter.act({"tick": 1})
    assert result.status is AdapterStatus.TIMEOUT
    # Both handlers receive TERM because the supervisor signals the entire group.
    assert child_marker.read_text() == "term"
    assert parent_marker.read_text() == "term"
    await adapter.close()


@pytest.mark.asyncio
async def test_subprocess_close_is_idempotent() -> None:
    adapter = _unsafe_adapter(
        "import json,sys; json.load(sys.stdin); json.dump({'payload': {}}, sys.stdout)"
    )
    await adapter.close()
    await adapter.close()
    result = await adapter.act({"tick": 1})
    assert result.status is AdapterStatus.CLOSED


@pytest.mark.asyncio
async def test_close_terminates_an_active_process() -> None:
    adapter = _unsafe_adapter(
        "import json,sys,time; json.load(sys.stdin); time.sleep(30)",
        limits=RuntimeLimits(timeout_seconds=30, termination_grace_seconds=0.05),
    )
    call = asyncio.create_task(adapter.act({"tick": 1}))
    for _ in range(100):
        if adapter._active:  # intentional white-box assertion of supervisor state
            break
        await asyncio.sleep(0.01)
    await adapter.close()
    result = await asyncio.wait_for(call, timeout=2)
    assert result.status in {AdapterStatus.PROCESS_ERROR, AdapterStatus.INVALID_OUTPUT}
