"""Checkpoint-authoritative Claude Code adapter using official headless mode.

This is intentionally not an Anthropic API wrapper.  It invokes the installed
``claude`` binary, lets that binary own authentication, and gives the model no
tools. OAuth users must not add ``--bare``: current
Claude Code explicitly disables OAuth/keychain reads in bare mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import signal
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .base import AdapterResult, AdapterStatus, JSONValue
from .subprocess import (
    RuntimeLimits,
    _feed_stdin,
    _OutputLimitExceeded,
    _read_bounded,
    parse_structured_json,
    sanitize_terminal_text,
)

_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "oneOf": [
        {
            "properties": {"agent_id": {"type": "string"}, "type": {"const": "noop"}},
            "required": ["agent_id", "type"],
            "additionalProperties": False,
        },
        {
            "properties": {
                "agent_id": {"type": "string"},
                "type": {"const": "move"},
                "destination": {"type": "string"},
            },
            "required": ["agent_id", "type", "destination"],
            "additionalProperties": False,
        },
        {
            "properties": {
                "agent_id": {"type": "string"},
                "type": {"const": "forage"},
                "resource": {"enum": ["red_berry", "root"]},
            },
            "required": ["agent_id", "type", "resource"],
            "additionalProperties": False,
        },
        {
            "properties": {
                "agent_id": {"type": "string"},
                "type": {"const": "eat"},
                "item": {"enum": ["red_berry", "root"]},
            },
            "required": ["agent_id", "type", "item"],
            "additionalProperties": False,
        },
        {
            "properties": {
                "agent_id": {"type": "string"},
                "type": {"const": "dig"},
                "depth": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["agent_id", "type", "depth"],
            "additionalProperties": False,
        },
    ],
}
_TEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


class ClaudeCodeAgentAdapter:
    """Run each inference solely from the committed Terrarium context.

    Claude Code session persistence cannot participate in Terrarium's atomic
    tick commit. Keeping hidden provider history would therefore create a
    split-brain after a timeout or crash. Every call is deliberately ephemeral;
    the complete checkpointed AgentContext is the sole lifetime memory.
    """

    def __init__(
        self,
        *,
        executable: str,
        executable_sha256: str,
        model: str,
        run_id: str,
        context: Any,
        limits: RuntimeLimits | None = None,
    ) -> None:
        path = Path(executable).resolve(strict=True)
        if hashlib.sha256(path.read_bytes()).hexdigest() != executable_sha256:
            raise RuntimeError("Claude Code executable digest changed")
        self.executable = str(path)
        self.model = model
        self.agent_id = context.agent_id
        self.run_id = run_id
        self.limits = limits or RuntimeLimits()
        self._closed = False
        self._lock = asyncio.Lock()
        self._active: asyncio.subprocess.Process | None = None

    async def act(self, observation: dict[str, Any]) -> AdapterResult:
        return await self._call("act", observation, _ACTION_SCHEMA)

    async def write_legacy(
        self, budget_tokens: int, context: dict[str, Any] | None = None
    ) -> AdapterResult:
        prompt: JSONValue = {
            "operation": "write_legacy",
            "budget_tokens": budget_tokens,
            "instructions": (
                "Use your remembered life to write the final record. Return only structured data."
            ),
        }
        if context is not None:
            prompt["context"] = context
        return await self._call("write_legacy", prompt, _TEXT_SCHEMA)

    async def retell(self, record: dict[str, Any]) -> AdapterResult:
        return await self._call("retell", record, _TEXT_SCHEMA)

    async def answer_survey(self, probe: dict[str, Any]) -> AdapterResult:
        return await self._call("answer_survey", probe, {"type": "object"})

    def _argv(self, schema: Mapping[str, Any]) -> tuple[str, ...]:
        return (
            self.executable,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--model",
            self.model,
            "--safe-mode",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--tools",
            "",
            "--permission-mode",
            "dontAsk",
            "--json-schema",
            json.dumps(schema, sort_keys=True, separators=(",", ":")),
        )

    async def _call(
        self, operation: str, payload: JSONValue, schema: Mapping[str, Any]
    ) -> AdapterResult:
        async with self._lock:
            if self._closed:
                return self._failure(AdapterStatus.CLOSED, "adapter_closed", "adapter is closed")
            request = json.dumps(
                {"operation": operation, "payload": payload},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
            if len(request) > self.limits.max_input_bytes:
                return self._failure(
                    AdapterStatus.SECURITY_ERROR, "input_limit", "request exceeds input limit"
                )
            try:
                with tempfile.TemporaryDirectory(prefix="terrarium-claude-") as work:
                    process = await asyncio.create_subprocess_exec(
                        *self._argv(schema),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=work,
                        close_fds=True,
                        start_new_session=True,
                    )
                    self._active = process
                    try:
                        stdout, stderr = await asyncio.wait_for(
                            self._communicate_bounded(process, request),
                            timeout=self.limits.timeout_seconds,
                        )
                    except TimeoutError:
                        return self._failure(
                            AdapterStatus.TIMEOUT,
                            "claude_timeout",
                            "Claude Code timed out",
                            retryable=True,
                        )
                    except _OutputLimitExceeded:
                        return self._failure(
                            AdapterStatus.OUTPUT_LIMIT,
                            "claude_output_limit",
                            "Claude Code output exceeded limit",
                        )
                    finally:
                        # The orchestrator's own timeout cancels this coroutine
                        # rather than raising TimeoutError here, so the process
                        # must be reaped on *every* non-clean exit, shielded
                        # from further cancellation.
                        if process.returncode is None:
                            await asyncio.shield(self._terminate(process))
                        self._active = None
            except OSError:
                return self._failure(
                    AdapterStatus.PROCESS_ERROR,
                    "claude_spawn_failed",
                    "Claude Code could not start",
                )
            safe_diagnostic = (
                sanitize_terminal_text(stderr, max_chars=self.limits.max_safe_log_chars) or None
            )
            if process.returncode != 0:
                return self._failure(
                    AdapterStatus.PROCESS_ERROR,
                    "claude_process_error",
                    "Claude Code returned an error",
                    raw_text=safe_diagnostic,
                    retryable=True,
                )
            parsed = parse_structured_json(
                stdout, max_bytes=self.limits.max_stdout_bytes, max_depth=self.limits.max_json_depth
            )
            if not parsed.ok or not isinstance(parsed.value, dict):
                return self._failure(
                    AdapterStatus.INVALID_OUTPUT,
                    "claude_invalid_json",
                    "Claude Code returned invalid JSON",
                    raw_text=safe_diagnostic,
                )
            try:
                result = self._extract_result(parsed.value)
            except (TypeError, ValueError):
                return self._failure(
                    AdapterStatus.INVALID_OUTPUT,
                    "claude_invalid_envelope",
                    "Claude Code returned an invalid result envelope",
                    raw_text=safe_diagnostic,
                )
            return AdapterResult.success(
                result[0],
                raw_text=None,
                usage=result[1],
                unsafe_host_execution=True,
                metadata={
                    "adapter": "claude-code",
                    "memory_mode": "checkpoint",
                    "total_cost_usd": result[2],
                },
            )

    async def _communicate_bounded(
        self, process: asyncio.subprocess.Process, request: bytes
    ) -> tuple[bytes, bytes]:
        """Feed stdin and read both pipes with incremental output limits.

        Unlike ``process.communicate``, a runaway CLI cannot buffer unbounded
        bytes into orchestrator memory: reading aborts fail-closed with
        ``_OutputLimitExceeded`` as soon as either stream passes its limit.
        """

        stdout_task = asyncio.create_task(
            _read_bounded(process.stdout, self.limits.max_stdout_bytes, "stdout")
        )
        stderr_task = asyncio.create_task(
            _read_bounded(process.stderr, self.limits.max_stderr_bytes, "stderr")
        )
        stdin_task = asyncio.create_task(_feed_stdin(process.stdin, request))
        tasks = (stdout_task, stderr_task, stdin_task)
        try:
            stdout, stderr, _ = await asyncio.gather(*tasks)
            await process.wait()
            return stdout, stderr
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _extract_result(
        envelope: dict[str, JSONValue],
    ) -> tuple[JSONValue, Mapping[str, int], float | None]:
        if envelope.get("is_error") is True or envelope.get("subtype") not in {None, "success"}:
            raise ValueError("unsuccessful result")
        output = envelope.get("structured_output")
        if output is None:
            output = envelope.get("result")
            if isinstance(output, str):
                decoded = parse_structured_json(output)
                if not decoded.ok:
                    raise ValueError("result is not structured JSON")
                output = decoded.value
        if not isinstance(output, dict):
            raise ValueError("missing structured output")
        raw_usage = envelope.get("usage")
        if not isinstance(raw_usage, dict):
            raise ValueError("missing usage")

        def token(name: str) -> int:
            value = raw_usage.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("invalid usage")
            return value

        input_tokens = sum(
            token(name)
            for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        output_tokens = token("output_tokens")
        cost = envelope.get("total_cost_usd")
        if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int, float))):
            raise ValueError("invalid cost")
        return (
            output,
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
            cost,
        )

    def _failure(
        self,
        status: AdapterStatus,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        raw_text: str | None = None,
    ) -> AdapterResult:
        return AdapterResult.failure(
            status,
            code,
            message,
            retryable=retryable,
            raw_text=raw_text,
            unsafe_host_execution=True,
            metadata={"adapter": "claude-code", "memory_mode": "checkpoint"},
        )

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=1)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()

    async def close(self) -> None:
        self._closed = True
        if self._active is not None:
            await self._terminate(self._active)
