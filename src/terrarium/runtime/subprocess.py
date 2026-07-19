"""Fail-closed subprocess supervision for real model CLIs.

This module intentionally implements only a small JSON-over-stdin protocol.  It
does not parse prose and it never invokes a shell.  A provider wrapper receives
one request document and must emit exactly one structured JSON document.

Strong isolation is available through bubblewrap on Linux.  Plain process
execution is an explicitly acknowledged development escape hatch: resource
limits, a clean environment and an ephemeral cwd are useful containment, but
they are *not* a filesystem or network sandbox.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import math
import os
import re
import shutil
import signal
import stat
import sys
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, cast

try:  # pragma: no cover - exercised on POSIX CI, retained for import portability
    import resource
except ImportError:  # pragma: no cover - Windows
    resource = None  # type: ignore[assignment]

from .base import AdapterResult, AdapterStatus, JSONScalar, JSONValue


class RuntimeSecurityError(ValueError):
    """Base class for rejected runtime configuration."""


class CommandNotAllowed(RuntimeSecurityError):
    """The executable is absent from the exact-path allowlist."""


class DangerousSandboxPolicy(RuntimeSecurityError):
    """A policy requests a capability without its explicit acknowledgement."""


class SandboxUnavailable(RuntimeSecurityError):
    """The requested strong sandbox backend cannot be resolved."""


class JSONFailureCode(StrEnum):
    TOO_LARGE = "json_too_large"
    INVALID_UTF8 = "json_invalid_utf8"
    MALFORMED = "json_malformed"
    DUPLICATE_KEY = "json_duplicate_key"
    NON_FINITE = "json_non_finite"
    TOO_DEEP = "json_too_deep"
    WRONG_ROOT = "json_wrong_root"


@dataclass(frozen=True, slots=True)
class JSONParseFailure:
    code: JSONFailureCode
    message: str


@dataclass(frozen=True, slots=True)
class JSONParseResult:
    """Non-throwing result used at the untrusted output boundary."""

    value: JSONValue | None = None
    error: JSONParseFailure | None = None

    def __post_init__(self) -> None:
        if self.error is not None and self.value is not None:
            raise ValueError("failed JSONParseResult cannot also contain a value")

    @property
    def ok(self) -> bool:
        return self.error is None


class StrictJSONError(ValueError):
    def __init__(self, failure: JSONParseFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


class _DuplicateKeyError(ValueError):
    pass


class _NonFiniteNumberError(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, JSONValue]]) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _finite_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise _NonFiniteNumberError(raw)
    return value


def _reject_non_finite(raw: str) -> None:
    raise _NonFiniteNumberError(raw)


def _exceeds_depth(value: JSONValue, max_depth: int) -> bool:
    stack: list[tuple[JSONValue, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > max_depth:
            return True
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return False


def parse_structured_json(
    data: bytes | str,
    *,
    max_bytes: int = 65_536,
    max_depth: int = 32,
    require_object: bool = True,
) -> JSONParseResult:
    """Parse a bounded JSON document without permissive Python JSON extensions.

    Duplicate keys, NaN/Infinity (including exponent overflow), invalid UTF-8,
    excessive depth and non-object roots are rejected.  The parser never tries
    to recover a JSON fragment from surrounding free text.
    """

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 1:
        raise ValueError("max_depth must be a positive integer")

    if isinstance(data, str):
        try:
            encoded = data.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return JSONParseResult(
                error=JSONParseFailure(JSONFailureCode.INVALID_UTF8, "invalid UTF-8 JSON")
            )
        text = data
    else:
        encoded = bytes(data)
        try:
            text = encoded.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return JSONParseResult(
                error=JSONParseFailure(JSONFailureCode.INVALID_UTF8, "invalid UTF-8 JSON")
            )

    if len(encoded) > max_bytes:
        return JSONParseResult(
            error=JSONParseFailure(JSONFailureCode.TOO_LARGE, "JSON document exceeds byte limit")
        )

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_non_finite,
            parse_float=_finite_float,
        )
    except _DuplicateKeyError:
        return JSONParseResult(
            error=JSONParseFailure(JSONFailureCode.DUPLICATE_KEY, "JSON has a duplicate key")
        )
    except _NonFiniteNumberError:
        return JSONParseResult(
            error=JSONParseFailure(JSONFailureCode.NON_FINITE, "JSON has a non-finite number")
        )
    except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError, OverflowError):
        return JSONParseResult(
            error=JSONParseFailure(JSONFailureCode.MALFORMED, "malformed JSON document")
        )

    if require_object and not isinstance(parsed, dict):
        return JSONParseResult(
            error=JSONParseFailure(JSONFailureCode.WRONG_ROOT, "JSON root must be an object")
        )
    value = cast(JSONValue, parsed)
    if _exceeds_depth(value, max_depth):
        return JSONParseResult(
            error=JSONParseFailure(JSONFailureCode.TOO_DEEP, "JSON exceeds nesting depth limit")
        )
    return JSONParseResult(value=value)


def strict_json_loads(
    data: bytes | str,
    *,
    max_bytes: int = 65_536,
    max_depth: int = 32,
    require_object: bool = True,
) -> JSONValue:
    """Raising convenience wrapper around :func:`parse_structured_json`."""

    result = parse_structured_json(
        data,
        max_bytes=max_bytes,
        max_depth=max_depth,
        require_object=require_object,
    )
    if result.error is not None:
        raise StrictJSONError(result.error)
    return result.value


_ANSI_CSI_RE: Final = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE: Final = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_SINGLE_RE: Final = re.compile(r"\x1b[@-_]")


def sanitize_terminal_text(value: bytes | str, *, max_chars: int = 8_192) -> str:
    """Make bounded process output safe to place in logs or a terminal."""

    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 0:
        raise ValueError("max_chars must be a non-negative integer")
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = value
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ANSI_OSC_RE.sub("", text)
    text = _ANSI_CSI_RE.sub("", text)
    text = _ANSI_SINGLE_RE.sub("", text)
    text = "".join(
        character
        for character in text
        if character in {"\n", "\t"} or unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
    )
    if len(text) > max_chars:
        return text[:max_chars] + "\n[truncated]"
    return text


class SandboxBackend(StrEnum):
    MOCK = "mock"
    BUBBLEWRAP = "bubblewrap"
    PROCESS = "process"


class NetworkMode(StrEnum):
    NONE = "none"
    INHERIT = "inherit"


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """Explicit capability policy for an adapter.

    ``mock`` is the safe default and rejects real commands.  ``bubblewrap`` has
    no fallback.  ``process`` is intentionally named and flagged as unsafe; its
    acknowledgement does not turn it into a sandbox.
    """

    backend: SandboxBackend | str = SandboxBackend.MOCK
    network: NetworkMode | str = NetworkMode.NONE
    acknowledge_unsafe_host_execution: bool = False
    acknowledge_network_inherit: bool = False
    egress_proxy_marker: str | None = None
    bwrap_path: str | None = None

    def __post_init__(self) -> None:
        try:
            backend = SandboxBackend(self.backend)
            network = NetworkMode(self.network)
        except ValueError as error:
            raise DangerousSandboxPolicy("unknown sandbox backend or network mode") from error
        object.__setattr__(self, "backend", backend)
        object.__setattr__(self, "network", network)

        if network is NetworkMode.INHERIT:
            if not self.acknowledge_network_inherit:
                raise DangerousSandboxPolicy(
                    "host network inheritance requires acknowledge_network_inherit=true"
                )
            if not self.egress_proxy_marker or not self.egress_proxy_marker.strip():
                raise DangerousSandboxPolicy(
                    "host network inheritance requires a non-empty egress proxy marker"
                )
            if any(unicodedata.category(char) == "Cc" for char in self.egress_proxy_marker):
                raise DangerousSandboxPolicy("egress proxy marker contains control characters")
        elif self.egress_proxy_marker is not None:
            raise DangerousSandboxPolicy("egress proxy marker is only valid with inherited network")

        if backend is SandboxBackend.PROCESS and not self.acknowledge_unsafe_host_execution:
            raise DangerousSandboxPolicy(
                "process-only execution requires acknowledge_unsafe_host_execution=true"
            )
        if self.bwrap_path is not None and backend is not SandboxBackend.BUBBLEWRAP:
            raise DangerousSandboxPolicy("bwrap_path is only valid for the bubblewrap backend")

    @classmethod
    def unsafe_process(
        cls,
        *,
        network: NetworkMode | str = NetworkMode.NONE,
        acknowledge_network_inherit: bool = False,
        egress_proxy_marker: str | None = None,
    ) -> SandboxPolicy:
        return cls(
            backend=SandboxBackend.PROCESS,
            network=network,
            acknowledge_unsafe_host_execution=True,
            acknowledge_network_inherit=acknowledge_network_inherit,
            egress_proxy_marker=egress_proxy_marker,
        )

    def validate_for_real_process(self) -> None:
        if self.backend is SandboxBackend.MOCK:
            raise DangerousSandboxPolicy("the mock policy cannot execute a real CLI")
        if self.backend is SandboxBackend.BUBBLEWRAP:
            self.resolve_bwrap()

    def resolve_bwrap(self) -> str:
        candidate = self.bwrap_path or shutil.which("bwrap")
        if not candidate:
            raise SandboxUnavailable("bubblewrap is not installed; refusing to run without it")
        path = Path(candidate).expanduser()
        if not path.is_absolute():
            raise SandboxUnavailable("bubblewrap path must be absolute")
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise SandboxUnavailable("bubblewrap executable cannot be resolved") from error
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise SandboxUnavailable("bubblewrap path is not an executable file")
        return str(resolved)


def _positive_int(name: str, value: int, *, minimum: int = 1) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    timeout_seconds: float = 60.0
    termination_grace_seconds: float = 1.0
    max_input_bytes: int = 65_536
    max_stdout_bytes: int = 65_536
    max_stderr_bytes: int = 16_384
    max_json_depth: int = 32
    max_safe_log_chars: int = 8_192
    cpu_seconds: int = 30
    address_space_bytes: int = 2 * 1024**3
    max_processes: int = 32
    max_open_files: int = 64
    max_file_bytes: int = 8 * 1024**2

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0 < self.timeout_seconds <= 86_400
        ):
            raise ValueError("timeout_seconds must be in (0, 86400]")
        if (
            isinstance(self.termination_grace_seconds, bool)
            or not isinstance(self.termination_grace_seconds, (int, float))
            or not 0 <= self.termination_grace_seconds <= 30
        ):
            raise ValueError("termination_grace_seconds must be in [0, 30]")
        _positive_int("max_input_bytes", self.max_input_bytes)
        _positive_int("max_stdout_bytes", self.max_stdout_bytes)
        _positive_int("max_stderr_bytes", self.max_stderr_bytes)
        _positive_int("max_json_depth", self.max_json_depth)
        _positive_int("max_safe_log_chars", self.max_safe_log_chars, minimum=0)
        _positive_int("cpu_seconds", self.cpu_seconds)
        _positive_int("address_space_bytes", self.address_space_bytes, minimum=16 * 1024**2)
        _positive_int("max_processes", self.max_processes)
        _positive_int("max_open_files", self.max_open_files, minimum=8)
        _positive_int("max_file_bytes", self.max_file_bytes)


_ENV_NAME_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_RESERVED_ENV: Final = frozenset({"HOME", "TMPDIR", "TMP", "TEMP", "PWD", "OLDPWD", "SHLVL", "_"})


@dataclass(frozen=True, slots=True)
class EnvironmentPolicy:
    """Exact-name environment allowlist; nothing is inherited by default."""

    allowed_names: frozenset[str] = field(default_factory=frozenset)
    inherit_names: tuple[str, ...] = ()
    values: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        allowed = frozenset(self.allowed_names)
        inherited = tuple(self.inherit_names)
        values = dict(self.values)
        for name in (*allowed, *inherited, *values.keys()):
            if not isinstance(name, str) or not _ENV_NAME_RE.fullmatch(name):
                raise ValueError("environment variable names must be valid ASCII identifiers")
            if name in _RESERVED_ENV:
                raise DangerousSandboxPolicy(f"environment variable {name} is runtime-reserved")
        if not set(inherited).issubset(allowed) or not set(values).issubset(allowed):
            raise DangerousSandboxPolicy("environment values must be present in allowed_names")
        for value in values.values():
            if not isinstance(value, str) or "\x00" in value:
                raise ValueError("environment values must be NUL-free strings")
        object.__setattr__(self, "allowed_names", allowed)
        object.__setattr__(self, "inherit_names", inherited)
        object.__setattr__(self, "values", MappingProxyType(values))

    def build(self, directories: _SandboxDirectories, *, bubblewrap: bool) -> dict[str, str]:
        environment: dict[str, str] = {}
        for name in self.inherit_names:
            value = os.environ.get(name)
            if value is not None and "\x00" not in value:
                environment[name] = value
        environment.update(self.values)
        if bubblewrap:
            # This is the private tmpfs path *inside* the bubblewrap namespace.
            home, work, temporary = "/home/agent", "/work", "/tmp"  # noqa: S108
        else:
            home, work, temporary = map(
                str, (directories.home, directories.work, directories.temporary)
            )
        environment.update(
            {
                "HOME": home,
                "PWD": work,
                "TMPDIR": temporary,
                "TMP": temporary,
                "TEMP": temporary,
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "NO_COLOR": "1",
                "TERM": "dumb",
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        return environment


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """An immutable argv and exact, canonical executable allowlist."""

    argv: tuple[str, ...]
    allowed_executables: frozenset[str]

    def __post_init__(self) -> None:
        argv = tuple(self.argv)
        if not argv or not all(isinstance(item, str) for item in argv):
            raise ValueError("argv must contain at least one string")
        if len(argv) > 256 or any("\x00" in item or len(item) > 16_384 for item in argv):
            raise ValueError("argv exceeds fixed safety bounds")

        executable = Path(argv[0]).expanduser()
        if not executable.is_absolute():
            raise CommandNotAllowed("executable must be an absolute path; PATH lookup is forbidden")
        try:
            resolved_executable = executable.resolve(strict=True)
        except OSError as error:
            raise CommandNotAllowed("executable cannot be resolved") from error
        try:
            mode = resolved_executable.stat().st_mode
        except OSError as error:
            raise CommandNotAllowed("executable cannot be inspected") from error
        if not stat.S_ISREG(mode) or not os.access(resolved_executable, os.X_OK):
            raise CommandNotAllowed("executable must be an executable regular file")

        resolved_allowlist: set[str] = set()
        for allowed in self.allowed_executables:
            path = Path(allowed).expanduser()
            if not path.is_absolute():
                raise CommandNotAllowed("allowlist entries must be absolute paths")
            try:
                resolved_allowlist.add(str(path.resolve(strict=True)))
            except OSError as error:
                raise CommandNotAllowed("allowlist entry cannot be resolved") from error
        if str(resolved_executable) not in resolved_allowlist:
            raise CommandNotAllowed("executable is not in the exact-path allowlist")

        object.__setattr__(self, "argv", (str(resolved_executable), *argv[1:]))
        object.__setattr__(self, "allowed_executables", frozenset(resolved_allowlist))

    @property
    def executable(self) -> str:
        return self.argv[0]


@dataclass(frozen=True, slots=True)
class _SandboxDirectories:
    root: Path
    home: Path
    work: Path
    temporary: Path


def _make_sandbox_directories() -> tuple[tempfile.TemporaryDirectory[str], _SandboxDirectories]:
    # The constant prefix deliberately contains no run or agent identifier.
    owner = tempfile.TemporaryDirectory(prefix="terrarium-runtime-", dir="/tmp")
    root = Path(owner.name)
    os.chmod(root, 0o700)
    home = root / "home"
    work = root / "work"
    temporary = root / "tmp"
    for directory in (home, work, temporary):
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
    return owner, _SandboxDirectories(root, home, work, temporary)


_BWRAP_SYSTEM_ROOTS: Final = (Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64"))


def _under_mounted_system_root(path: Path) -> bool:
    for root in _BWRAP_SYSTEM_ROOTS:
        if root.exists() and (path == root or root in path.parents):
            return True
    return False


def build_bubblewrap_argv(
    command: CommandSpec,
    directories: _SandboxDirectories,
    environment: Mapping[str, str],
    policy: SandboxPolicy,
    limits: RuntimeLimits | None = None,
) -> tuple[str, ...]:
    """Build a minimal bubblewrap filesystem; the repository is never mounted."""

    if policy.backend is not SandboxBackend.BUBBLEWRAP:
        raise DangerousSandboxPolicy("bubblewrap argv requested for a different backend")
    executable = Path(command.executable)
    if not _under_mounted_system_root(executable):
        raise DangerousSandboxPolicy("CLI executable is outside the read-only system roots")
    limits = limits or RuntimeLimits()
    # Kept in the signature for callers that prepare both backends uniformly.
    # Bubblewrap uses anonymous tmpfs mounts instead of exposing these host paths.
    _ = directories

    argv: list[str] = [
        policy.resolve_bwrap(),
        "--die-with-parent",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--disable-userns",
        "--hostname",
        "terrarium",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--size",
        str(limits.max_file_bytes),
        "--tmpfs",
        "/",
    ]
    if policy.network is NetworkMode.NONE:
        argv.append("--unshare-net")

    for system_root in _BWRAP_SYSTEM_ROOTS:
        if system_root.exists():
            argv.extend(("--ro-bind", str(system_root), str(system_root)))

    argv.extend(("--dir", "/etc"))
    for etc_path in (
        Path("/etc/passwd"),
        Path("/etc/group"),
        Path("/etc/nsswitch.conf"),
        Path("/etc/hosts"),
        Path("/etc/ssl"),
        Path("/etc/pki"),
        Path("/etc/ca-certificates"),
    ):
        if etc_path.exists():
            argv.extend(("--ro-bind", str(etc_path), str(etc_path)))
    if policy.network is NetworkMode.INHERIT and Path("/etc/resolv.conf").exists():
        argv.extend(("--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf"))

    argv.extend(
        (
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--dir",
            "/home",
            "--size",
            str(limits.max_file_bytes),
            "--tmpfs",
            "/home/agent",
            "--chmod",
            "0700",
            "/home/agent",
            "--size",
            str(limits.max_file_bytes),
            "--tmpfs",
            "/work",
            "--chmod",
            "0700",
            "/work",
            "--size",
            str(limits.max_file_bytes),
            "--tmpfs",
            "/tmp",  # noqa: S108 - private tmpfs inside the sandbox namespace
            "--chmod",
            "0700",
            "/tmp",  # noqa: S108 - private tmpfs inside the sandbox namespace
        )
    )
    for name, value in sorted(environment.items()):
        argv.extend(("--setenv", name, value))
    argv.extend(("--chdir", "/work", "--", *command.argv))
    return tuple(argv)


_PR_SET_NO_NEW_PRIVS: Final = 38
if sys.platform.startswith("linux"):
    _LIBC = ctypes.CDLL(None, use_errno=True)
else:  # pragma: no cover - Linux is the production target
    _LIBC = None


def _set_resource_limit(kind: int, requested: int) -> None:
    assert resource is not None
    _, current_hard = resource.getrlimit(kind)
    hard = requested if current_hard == resource.RLIM_INFINITY else min(requested, current_hard)
    resource.setrlimit(kind, (hard, hard))


def _uid_process_ceiling(requested_headroom: int, proc_root: Path = Path("/proc")) -> int:
    """Translate per-agent headroom to Linux's UID-wide ``RLIMIT_NPROC`` value.

    Linux counts every process owned by the real UID, including unrelated runner
    services.  Applying ``requested_headroom`` as an absolute value can therefore
    prevent the adapter itself from spawning even one child on a shared-UID host.
    The snapshot is intentionally conservative: concurrent host processes can only
    consume the remaining allowance earlier.  Strong aggregate enforcement remains
    the external cgroup/container boundary documented in ``SECURITY.md``.
    """

    if not sys.platform.startswith("linux"):
        return requested_headroom
    try:
        uid = os.getuid()
        current = 0
        with os.scandir(proc_root) as processes:
            for entry in processes:
                try:
                    if (
                        not entry.name.isdecimal()
                        or not entry.is_dir(follow_symlinks=False)
                        or entry.stat(follow_symlinks=False).st_uid != uid
                    ):
                        continue
                    # Linux documents RLIMIT_NPROC as a process limit, but the
                    # kernel accounting unit is a task (thread), not a PID.
                    with os.scandir(Path(entry.path) / "task") as tasks:
                        current += sum(
                            task.name.isdecimal() and task.is_dir(follow_symlinks=False)
                            for task in tasks
                        )
                except FileNotFoundError:
                    # A process may exit while procfs is being sampled.  Omitting
                    # it cannot create extra agent headroom: its tasks are gone.
                    continue
                except OSError:
                    # If task enumeration alone is hidden, conservatively count
                    # the visible process leader.
                    current += 1
    except OSError:
        # Retain fail-closed historical behavior if procfs is unavailable.
        return requested_headroom
    return current + requested_headroom


def _make_preexec(limits: RuntimeLimits):
    """Return the minimal POSIX child setup used immediately before exec."""

    if resource is None:
        return None
    nproc_ceiling = _uid_process_ceiling(limits.max_processes)

    def apply_limits() -> None:
        os.umask(0o077)
        _set_resource_limit(resource.RLIMIT_CPU, limits.cpu_seconds)
        _set_resource_limit(resource.RLIMIT_AS, limits.address_space_bytes)
        if hasattr(resource, "RLIMIT_NPROC"):
            _set_resource_limit(resource.RLIMIT_NPROC, nproc_ceiling)
        _set_resource_limit(resource.RLIMIT_NOFILE, limits.max_open_files)
        _set_resource_limit(resource.RLIMIT_FSIZE, limits.max_file_bytes)
        if hasattr(resource, "RLIMIT_CORE"):
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if _LIBC is not None and _LIBC.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, "PR_SET_NO_NEW_PRIVS failed")

    return apply_limits


class _OutputLimitExceeded(Exception):
    def __init__(self, stream_name: str, preview: bytes) -> None:
        super().__init__(stream_name)
        self.stream_name = stream_name
        self.preview = preview


async def _read_bounded(
    stream: asyncio.StreamReader | None,
    limit: int,
    stream_name: str,
) -> bytes:
    if stream is None:
        return b""
    chunks = bytearray()
    while True:
        chunk = await stream.read(min(65_536, limit - len(chunks) + 1))
        if not chunk:
            return bytes(chunks)
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise _OutputLimitExceeded(stream_name, bytes(chunks[:limit]))


async def _feed_stdin(writer: asyncio.StreamWriter | None, payload: bytes) -> None:
    if writer is None:
        return
    try:
        writer.write(payload)
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await writer.wait_closed()


async def _exchange(
    process: asyncio.subprocess.Process,
    payload: bytes,
    limits: RuntimeLimits,
) -> tuple[bytes, bytes, int]:
    stdout_task = asyncio.create_task(
        _read_bounded(process.stdout, limits.max_stdout_bytes, "stdout")
    )
    stderr_task = asyncio.create_task(
        _read_bounded(process.stderr, limits.max_stderr_bytes, "stderr")
    )
    stdin_task = asyncio.create_task(_feed_stdin(process.stdin, payload))
    wait_task = asyncio.create_task(process.wait())
    tasks: set[asyncio.Task[Any]] = {stdout_task, stderr_task, stdin_task, wait_task}
    try:
        while tasks:
            done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                error = task.exception()
                if error is not None:
                    raise error
        return stdout_task.result(), stderr_task.result(), wait_task.result()
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _process_group_exists(process_group: int) -> bool:
    if os.name != "posix":  # pragma: no cover - production target is POSIX
        return False
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _terminate_process_group(
    process: asyncio.subprocess.Process,
    grace_seconds: float,
) -> None:
    """TERM then KILL the dedicated session and always reap its leader."""

    # Never address a numeric process group after asyncio has observed/reaped
    # its leader. The kernel may already have recycled that PID/PGID for an
    # unrelated same-UID process group.
    if os.name == "posix" and process.returncode is None:
        process_group = process.pid  # start_new_session=True makes pid == pgid.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process_group, signal.SIGTERM)
        if grace_seconds and _process_group_exists(process_group):
            await asyncio.sleep(grace_seconds)
        if _process_group_exists(process_group):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process_group, signal.SIGKILL)
    elif os.name != "posix" and process.returncode is None:  # pragma: no cover
        process.kill()
    with contextlib.suppress(ProcessLookupError):
        await process.wait()
    # asyncio.subprocess.Process has no public transport-close API.  Explicitly
    # closing CPython's transport prevents canceled pipe readers from surviving
    # until event-loop teardown after a timeout/output flood.
    transport = getattr(process, "_transport", None)
    if transport is not None:
        transport.close()
        await asyncio.sleep(0)


def _encode_request(request: dict[str, JSONValue], limits: RuntimeLimits) -> bytes:
    try:
        encoded = json.dumps(
            request,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError) as error:
        raise RuntimeSecurityError("request is not valid finite JSON") from error
    if len(encoded) > limits.max_input_bytes:
        raise RuntimeSecurityError("request exceeds input byte limit")
    validation = parse_structured_json(
        encoded,
        max_bytes=limits.max_input_bytes,
        max_depth=limits.max_json_depth,
    )
    if validation.error is not None:
        raise RuntimeSecurityError(validation.error.message)
    return encoded


def _parse_usage(value: JSONValue) -> Mapping[str, int]:
    if not isinstance(value, dict):
        raise ValueError("usage must be an object")
    allowed = {"input_tokens", "output_tokens", "total_tokens"}
    if not set(value).issubset(allowed):
        raise ValueError("usage has unknown fields")
    if "input_tokens" not in value or "output_tokens" not in value:
        raise ValueError("usage must report input_tokens and output_tokens")
    parsed: dict[str, int] = {}
    for key, item in value.items():
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError("usage values must be non-negative integers")
        parsed[key] = item
    expected_total = parsed["input_tokens"] + parsed["output_tokens"]
    if "total_tokens" in parsed and parsed["total_tokens"] != expected_total:
        raise ValueError("usage total_tokens is inconsistent")
    parsed.setdefault("total_tokens", expected_total)
    return parsed


def _unwrap_response(value: JSONValue) -> tuple[JSONValue, Mapping[str, int] | None]:
    assert isinstance(value, dict)
    if "payload" not in value:
        return value, None
    if not set(value).issubset({"payload", "usage"}):
        raise ValueError("response envelope has unknown fields")
    payload = value["payload"]
    if not isinstance(payload, dict):
        raise ValueError("response payload must be an object")
    usage = _parse_usage(value["usage"]) if "usage" in value else None
    return payload, usage


class SubprocessAgentAdapter:
    """One-shot JSON subprocess adapter with bounded I/O and strict supervision."""

    def __init__(
        self,
        command: CommandSpec,
        *,
        sandbox: SandboxPolicy | None = None,
        limits: RuntimeLimits | None = None,
        environment: EnvironmentPolicy | None = None,
    ) -> None:
        self.command = command
        self.sandbox = sandbox or SandboxPolicy()
        self.limits = limits or RuntimeLimits()
        self.environment = environment or EnvironmentPolicy()
        self.sandbox.validate_for_real_process()
        if os.name != "posix" and self.sandbox.backend is SandboxBackend.BUBBLEWRAP:
            raise SandboxUnavailable("bubblewrap isolation is supported only on POSIX")
        if os.name != "posix" and self.sandbox.backend is SandboxBackend.PROCESS:
            raise SandboxUnavailable("unsafe process mode currently requires POSIX supervision")
        self._closed = False
        self._state_lock = asyncio.Lock()
        self._call_lock = asyncio.Lock()
        self._active: set[asyncio.subprocess.Process] = set()

    @property
    def unsafe_host_execution(self) -> bool:
        return self.sandbox.backend is SandboxBackend.PROCESS

    def _metadata(self) -> dict[str, JSONScalar]:
        process_mode = self.sandbox.backend is SandboxBackend.PROCESS
        metadata: dict[str, JSONScalar] = {
            "adapter": "subprocess",
            "sandbox_backend": str(self.sandbox.backend),
            "filesystem_isolated": not process_mode,
            "network_isolated": (not process_mode and self.sandbox.network is NetworkMode.NONE),
            "resource_limits_best_effort": process_mode,
        }
        if self.sandbox.egress_proxy_marker is not None:
            # This is an audit marker, not a claim that this module enforces a proxy.
            metadata["egress_proxy_marker"] = self.sandbox.egress_proxy_marker
        return metadata

    def _failure(
        self,
        status: AdapterStatus,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        raw_text: bytes | str | None = None,
    ) -> AdapterResult:
        safe_raw = (
            None
            if raw_text is None
            else sanitize_terminal_text(raw_text, max_chars=self.limits.max_safe_log_chars)
        )
        return AdapterResult.failure(
            status,
            code,
            message,
            retryable=retryable,
            raw_text=safe_raw,
            usage=None,
            unsafe_host_execution=self.unsafe_host_execution,
            metadata=self._metadata(),
        )

    async def _spawn(
        self,
        argv: Sequence[str],
        directories: _SandboxDirectories,
        environment: Mapping[str, str],
    ) -> asyncio.subprocess.Process:
        async with self._state_lock:
            if self._closed:
                raise RuntimeError("adapter is closed")
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(directories.work),
                env=dict(environment),
                close_fds=True,
                start_new_session=True,
                preexec_fn=_make_preexec(self.limits),
            )
            self._active.add(process)
            return process

    async def _invoke(self, operation: str, payload: JSONValue) -> AdapterResult:
        async with self._call_lock:
            if self._closed:
                return self._failure(AdapterStatus.CLOSED, "adapter_closed", "adapter is closed")
            try:
                request = _encode_request(
                    {"operation": operation, "payload": payload},
                    self.limits,
                )
            except RuntimeSecurityError as error:
                return self._failure(
                    AdapterStatus.SECURITY_ERROR,
                    "invalid_request",
                    sanitize_terminal_text(str(error), max_chars=256),
                )

            owner, directories = _make_sandbox_directories()
            process: asyncio.subprocess.Process | None = None
            try:
                is_bubblewrap = self.sandbox.backend is SandboxBackend.BUBBLEWRAP
                environment = self.environment.build(directories, bubblewrap=is_bubblewrap)
                argv: Sequence[str]
                if is_bubblewrap:
                    argv = build_bubblewrap_argv(
                        self.command,
                        directories,
                        environment,
                        self.sandbox,
                        self.limits,
                    )
                else:
                    argv = self.command.argv
                try:
                    process = await self._spawn(argv, directories, environment)
                except RuntimeError:
                    return self._failure(
                        AdapterStatus.CLOSED, "adapter_closed", "adapter is closed"
                    )
                except (OSError, ValueError):
                    return self._failure(
                        AdapterStatus.PROCESS_ERROR,
                        "launch_failed",
                        "process could not be started",
                        retryable=False,
                    )

                try:
                    stdout, stderr, returncode = await asyncio.wait_for(
                        _exchange(process, request, self.limits),
                        timeout=self.limits.timeout_seconds,
                    )
                except TimeoutError:
                    await _terminate_process_group(process, self.limits.termination_grace_seconds)
                    return self._failure(
                        AdapterStatus.TIMEOUT,
                        "process_timeout",
                        "process exceeded its wall-clock timeout",
                        retryable=True,
                    )
                except _OutputLimitExceeded as error:
                    await _terminate_process_group(process, self.limits.termination_grace_seconds)
                    return self._failure(
                        AdapterStatus.OUTPUT_LIMIT,
                        f"{error.stream_name}_limit",
                        f"process {error.stream_name} exceeded its byte limit",
                        raw_text=error.preview,
                    )
                except (OSError, BrokenPipeError, ConnectionError):
                    await _terminate_process_group(process, self.limits.termination_grace_seconds)
                    return self._failure(
                        AdapterStatus.PROCESS_ERROR,
                        "process_io_error",
                        "process I/O failed",
                        retryable=True,
                    )

                # If the leader is still live, terminate its complete group. If
                # asyncio has already reaped it, _terminate_process_group only
                # closes transport state: signalling a recycled numeric PGID
                # would risk killing an unrelated process.
                await _terminate_process_group(process, self.limits.termination_grace_seconds)
                if returncode != 0:
                    raw = stderr if stderr else stdout
                    return self._failure(
                        AdapterStatus.PROCESS_ERROR,
                        "nonzero_exit",
                        f"process exited with status {returncode}",
                        raw_text=raw,
                    )

                parsed = parse_structured_json(
                    stdout,
                    max_bytes=self.limits.max_stdout_bytes,
                    max_depth=self.limits.max_json_depth,
                    require_object=True,
                )
                if parsed.error is not None:
                    return self._failure(
                        AdapterStatus.INVALID_OUTPUT,
                        str(parsed.error.code),
                        parsed.error.message,
                        raw_text=stdout,
                    )
                assert parsed.value is not None
                try:
                    response_payload, usage = _unwrap_response(parsed.value)
                except ValueError:
                    return self._failure(
                        AdapterStatus.INVALID_OUTPUT,
                        "invalid_response_envelope",
                        "response envelope does not match the strict schema",
                        raw_text=stdout,
                    )
                return AdapterResult.success(
                    response_payload,
                    raw_text=sanitize_terminal_text(
                        stdout, max_chars=self.limits.max_safe_log_chars
                    ),
                    usage=usage,
                    unsafe_host_execution=self.unsafe_host_execution,
                    metadata=self._metadata(),
                )
            finally:
                if process is not None:
                    async with self._state_lock:
                        self._active.discard(process)
                    if process.returncode is None:
                        await _terminate_process_group(
                            process, self.limits.termination_grace_seconds
                        )
                owner.cleanup()

    async def act(self, observation: dict[str, Any]) -> AdapterResult:
        return await self._invoke("act", cast(JSONValue, observation))

    async def write_legacy(
        self,
        budget_tokens: int,
        context: dict[str, Any] | None = None,
    ) -> AdapterResult:
        if isinstance(budget_tokens, bool) or not isinstance(budget_tokens, int):
            return self._failure(
                AdapterStatus.SECURITY_ERROR,
                "invalid_budget",
                "budget_tokens must be an integer",
            )
        if budget_tokens < 0:
            return self._failure(
                AdapterStatus.SECURITY_ERROR,
                "invalid_budget",
                "budget_tokens must be non-negative",
            )
        return await self._invoke(
            "write_legacy",
            cast(JSONValue, {"budget_tokens": budget_tokens, "context": context}),
        )

    async def retell(self, record: dict[str, Any]) -> AdapterResult:
        return await self._invoke("retell", cast(JSONValue, record))

    async def answer_survey(self, probe: dict[str, Any]) -> AdapterResult:
        return await self._invoke("answer_survey", cast(JSONValue, probe))

    async def close(self) -> None:
        async with self._state_lock:
            if self._closed:
                return
            self._closed = True
            active = tuple(self._active)
        if active:
            await asyncio.gather(
                *(
                    _terminate_process_group(process, self.limits.termination_grace_seconds)
                    for process in active
                ),
                return_exceptions=True,
            )


SubprocessAdapter = SubprocessAgentAdapter


__all__ = [
    "CommandNotAllowed",
    "CommandSpec",
    "DangerousSandboxPolicy",
    "EnvironmentPolicy",
    "JSONFailureCode",
    "JSONParseFailure",
    "JSONParseResult",
    "NetworkMode",
    "RuntimeLimits",
    "RuntimeSecurityError",
    "SandboxBackend",
    "SandboxPolicy",
    "SandboxUnavailable",
    "StrictJSONError",
    "SubprocessAdapter",
    "SubprocessAgentAdapter",
    "build_bubblewrap_argv",
    "parse_structured_json",
    "sanitize_terminal_text",
    "strict_json_loads",
]
