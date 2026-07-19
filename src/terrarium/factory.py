"""Construct adapters from a sealed config without dynamic command expansion."""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Callable
from pathlib import Path

from .config import RunConfig
from .prompting import AgentContext
from .runtime.base import AgentAdapter
from .runtime.claude_code import ClaudeCodeAgentAdapter
from .runtime.mock import DeterministicMockAdapter
from .runtime.subprocess import (
    CommandSpec,
    EnvironmentPolicy,
    NetworkMode,
    RuntimeLimits,
    SandboxBackend,
    SandboxPolicy,
    SubprocessAgentAdapter,
)

AdapterFactory = Callable[[AgentContext], AgentAdapter]


def build_adapter_factory(config: RunConfig) -> AdapterFactory:
    """Return a per-agent factory after validating all process capabilities."""

    if config.runtime.adapter == "mock":

        def make_mock(context: AgentContext) -> AgentAdapter:
            return DeterministicMockAdapter(
                context.agent_id,
                context.inherited_legacy_texts,
                seed=config.seed,
            )

        return make_mock

    executable = _resolve_and_verify_executable(config)
    if config.runtime.adapter == "claude-code":
        def make_claude(context: AgentContext) -> AgentAdapter:
            return ClaudeCodeAgentAdapter(
                executable=executable,
                executable_sha256=config.runtime.executable_sha256 or "",
                model=config.runtime.model_id,
                run_id=config.run_id,
                context=context,
                limits=RuntimeLimits(
                    timeout_seconds=config.runtime.timeout_seconds,
                    max_input_bytes=config.runtime.max_input_bytes,
                    max_stdout_bytes=config.runtime.max_output_bytes,
                    max_stderr_bytes=config.runtime.max_stderr_bytes,
                ),
            )

        return make_claude

    command = CommandSpec(
        argv=(executable, *config.runtime.argv[1:]),
        allowed_executables=frozenset({executable}),
    )
    sandbox = _sandbox_policy(config)
    limits = RuntimeLimits(
        timeout_seconds=config.runtime.timeout_seconds,
        max_input_bytes=config.runtime.max_input_bytes,
        max_stdout_bytes=config.runtime.max_output_bytes,
        max_stderr_bytes=config.runtime.max_stderr_bytes,
    )
    environment = _environment_policy(config)

    def make_subprocess(_context: AgentContext) -> AgentAdapter:
        return SubprocessAgentAdapter(
            command,
            sandbox=sandbox,
            limits=limits,
            environment=environment,
        )

    return make_subprocess


def _resolve_and_verify_executable(config: RunConfig) -> str:
    configured = config.runtime.argv[0]
    resolved = shutil.which(configured)
    if resolved is None:
        candidate = Path(configured)
        if candidate.is_absolute() and candidate.exists():
            resolved = str(candidate)
    if resolved is None:
        raise FileNotFoundError(f"adapter executable not found: {configured}")
    path = Path(resolved).resolve(strict=True)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != config.runtime.executable_sha256:
        raise RuntimeError("adapter executable changed after configuration was sealed")
    return str(path)


def _sandbox_policy(config: RunConfig) -> SandboxPolicy:
    source = config.runtime.sandbox
    backend = SandboxBackend(source.backend)
    if source.network == "none":
        network = NetworkMode.NONE
        acknowledge_network = False
        marker = None
    else:
        network = NetworkMode.INHERIT
        acknowledge_network = True
        marker = source.egress_proxy or "explicit-unfiltered-network"
    bwrap_path: str | None = None
    if backend is SandboxBackend.BUBBLEWRAP:
        candidate = shutil.which("bwrap")
        if candidate is None:
            raise FileNotFoundError("bubblewrap backend requested but bwrap is unavailable")
        bwrap_path = str(Path(candidate).resolve(strict=True))
        digest = hashlib.sha256(Path(bwrap_path).read_bytes()).hexdigest()
        if digest != source.bwrap_sha256:
            raise RuntimeError("bubblewrap executable digest does not match configuration")
    return SandboxPolicy(
        backend=backend,
        network=network,
        acknowledge_unsafe_host_execution=source.acknowledge_unsafe_host_execution,
        acknowledge_network_inherit=acknowledge_network,
        egress_proxy_marker=marker,
        bwrap_path=bwrap_path,
    )


def _environment_policy(config: RunConfig) -> EnvironmentPolicy:
    proxy = config.runtime.sandbox.egress_proxy
    if proxy is None:
        return EnvironmentPolicy()
    # Proxy configuration is explicit, not inherited.  Provider credentials
    # remain outside the agent environment and should live in the proxy/broker.
    values = {"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "ALL_PROXY": proxy}
    return EnvironmentPolicy(allowed_names=frozenset(values), values=values)
