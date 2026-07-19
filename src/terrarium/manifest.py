"""Creation and verification of a reproducibility/supply-chain manifest."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict

from .config import RunConfig
from .prompting import prompt_template_sha256

MANIFEST_SCHEMA_VERSION = 2


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2] = MANIFEST_SCHEMA_VERSION
    config_sha256: str
    config: dict[str, Any]
    python_version: str
    python_implementation: str
    numpy_version: str
    pydantic_version: str
    pyyaml_version: str
    tiktoken_version: str
    bit_generator: str
    tokenizer: str
    prompt_template_sha256: str
    event_schema_version: int
    action_schema_version: int
    model_id: str
    provider: str
    adapter: str
    executable_path: str | None
    executable_sha256: str | None
    argv_file_sha256: dict[str, str]
    sandbox_policy_sha256: str
    package_code_sha256: str
    dependency_lock_sha256: str | None

    @classmethod
    def from_config(cls, config: RunConfig) -> RunManifest:
        executable_path: str | None = None
        executable_hash: str | None = None
        argv_file_hashes: dict[str, str] = {}
        if config.runtime.adapter in {"subprocess", "claude-code"}:
            resolved = shutil.which(config.runtime.argv[0])
            if resolved is None:
                raise FileNotFoundError(f"adapter executable not found: {config.runtime.argv[0]}")
            executable_path = str(Path(resolved).resolve())
            executable_hash = _sha256_file(Path(executable_path))
            if executable_hash != config.runtime.executable_sha256:
                raise RuntimeError("adapter executable digest does not match pinned configuration")
            argv_file_hashes = _hash_absolute_argv_files(config.runtime.argv[1:])

        sandbox_json = json.dumps(
            config.runtime.sandbox.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        return cls(
            config_sha256=config.digest(),
            config=config.model_dump(mode="json"),
            python_version=platform.python_version(),
            python_implementation=platform.python_implementation(),
            numpy_version=np.__version__,
            pydantic_version=importlib.metadata.version("pydantic"),
            pyyaml_version=importlib.metadata.version("PyYAML"),
            tiktoken_version=importlib.metadata.version("tiktoken"),
            bit_generator="numpy.random.PCG64",
            tokenizer=config.population.tokenizer,
            prompt_template_sha256=prompt_template_sha256(),
            event_schema_version=1,
            action_schema_version=1,
            model_id=config.runtime.model_id,
            provider=config.runtime.provider,
            adapter=config.runtime.adapter,
            executable_path=executable_path,
            executable_sha256=executable_hash,
            argv_file_sha256=argv_file_hashes,
            sandbox_policy_sha256=hashlib.sha256(sandbox_json).hexdigest(),
            package_code_sha256=_package_tree_sha256(),
            dependency_lock_sha256=_dependency_lock_sha256(),
        )

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    def sha256(self) -> str:
        """Digest used to bind the first committed event to this manifest."""

        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def assert_current_replay_environment(self) -> None:
        """Refuse exact replay under a different mechanics/runtime code identity."""

        current = {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "numpy_version": np.__version__,
            "pydantic_version": importlib.metadata.version("pydantic"),
            "pyyaml_version": importlib.metadata.version("PyYAML"),
            "tiktoken_version": importlib.metadata.version("tiktoken"),
            "bit_generator": "numpy.random.PCG64",
            "prompt_template_sha256": prompt_template_sha256(),
            "package_code_sha256": _package_tree_sha256(),
            "dependency_lock_sha256": _dependency_lock_sha256(),
        }
        drift = [name for name, value in current.items() if getattr(self, name) != value]
        if self.executable_path is not None:
            executable = Path(self.executable_path)
            try:
                executable_hash = _sha256_file(executable)
            except (OSError, RuntimeError):
                drift.append("executable_sha256")
            else:
                if executable_hash != self.executable_sha256:
                    drift.append("executable_sha256")
        for artifact, expected_hash in sorted(self.argv_file_sha256.items()):
            try:
                artifact_hash = _sha256_file(Path(artifact))
            except (OSError, RuntimeError):
                drift.append(f"argv_file_sha256:{artifact}")
            else:
                if artifact_hash != expected_hash:
                    drift.append(f"argv_file_sha256:{artifact}")
        if drift:
            raise RuntimeError(
                "replay environment drift detected in: " + ", ".join(sorted(drift))
            )

    def write_new(self, path: str | Path) -> None:
        """Publish once without an exists/replace race, or verify the winner.

        A manifest is the run's identity.  Concurrent starters may agree on that
        identity, but a later starter must never replace an earlier manifest with a
        different configuration.  Hard-link publication is atomic and fails when the
        destination already exists; that winner is then opened without following a
        symlink and compared byte-for-byte.
        """

        destination = Path(path)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".manifest-", dir=destination.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(self.canonical_bytes() + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(tmp_name, destination, follow_symlinks=False)
            except FileExistsError:
                existing = _read_manifest_no_follow(destination)
                self.assert_compatible(existing)
                return
            os.unlink(tmp_name)
            tmp_name = ""
            _fsync_directory(destination.parent)
        finally:
            if tmp_name and os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def assert_compatible(self, existing: RunManifest) -> None:
        if self.canonical_bytes() != existing.canonical_bytes():
            raise RuntimeError(
                "run manifest drift detected; resume is refused (start a new run_id instead)"
            )


def _sha256_file(path: Path) -> str:
    """Hash one regular file through a no-follow descriptor."""

    digest = hashlib.sha256()
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = os.open(path, flags)
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(fd)
        raise RuntimeError(f"manifest artifact is not a regular file: {path}")
    with os.fdopen(fd, "rb", closefd=True) as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_absolute_argv_files(arguments: tuple[str, ...]) -> dict[str, str]:
    """Seal interpreter scripts/config artifacts passed as absolute argv files."""

    result: dict[str, str] = {}
    for argument in arguments:
        candidate = Path(argument)
        if not candidate.is_absolute():
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file():
            result[str(resolved)] = _sha256_file(resolved)
    return result


def _read_manifest_no_follow(path: Path) -> RunManifest:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("manifest must be a regular file")
        if metadata.st_size > 1024 * 1024:
            raise RuntimeError("manifest exceeds the 1 MiB safety limit")
        chunks: list[bytes] = []
        remaining = metadata.st_size + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return RunManifest.model_validate_json(b"".join(chunks))
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _package_tree_sha256() -> str:
    """Hash every shipped Python source path and byte, including world mechanics."""

    package_root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(package_root.rglob("*.py")):
        relative = path.relative_to(package_root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _dependency_lock_sha256() -> str | None:
    """Seal uv.lock when running from a source/editable checkout."""

    candidate = Path(__file__).resolve().parents[2] / "uv.lock"
    return _sha256_file(candidate) if candidate.is_file() else None
