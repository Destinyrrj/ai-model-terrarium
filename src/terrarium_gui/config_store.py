"""Symlink-safe, bounded storage for editable experiment configurations."""

from __future__ import annotations

import errno
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from terrarium.config import MAX_CONFIG_BYTES, RunConfig, load_config

from .schemas import ConfigSummary, ValidationIssue

_CONFIG_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")


class ConfigStoreError(RuntimeError):
    """Base class for stable API-facing configuration errors."""


class InvalidConfigName(ConfigStoreError):
    pass


class ConfigNotFound(ConfigStoreError):
    pass


class UnsafeConfigFile(ConfigStoreError):
    pass


class ConfigTooLarge(ConfigStoreError):
    pass


@dataclass(frozen=True, slots=True)
class ConfigValidationFailure(ConfigStoreError):
    issues: tuple[ValidationIssue, ...]

    def __str__(self) -> str:
        return "configuration validation failed"


@dataclass(frozen=True, slots=True)
class StoredDocument:
    summary: ConfigSummary
    text: str


class ConfigStore:
    """Own config files beneath one non-symlink directory.

    Public names are tokens, never client-provided paths.  The on-disk
    extension is fixed to ``.yaml``.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root).absolute()
        self._ensure_root()
        self._root_fd = self._open_root()
        info = os.fstat(self._root_fd)
        self._root_identity = (info.st_dev, info.st_ino)

    def close(self) -> None:
        descriptor = getattr(self, "_root_fd", -1)
        if descriptor >= 0:
            os.close(descriptor)
            self._root_fd = -1

    def __del__(self) -> None:
        try:
            self.close()
        except OSError:
            pass

    @staticmethod
    def validate_name(name: str) -> str:
        if not isinstance(name, str) or _CONFIG_NAME.fullmatch(name) is None:
            raise InvalidConfigName(
                "config name must match [A-Za-z0-9][A-Za-z0-9_-]{0,95}"
            )
        return name

    def list(self) -> list[ConfigSummary]:
        root_fd = self._checked_root_fd()
        summaries: list[ConfigSummary] = []
        with os.scandir(root_fd) as entries:
            for entry in entries:
                if not entry.name.endswith(".yaml") or entry.name.startswith("."):
                    continue
                name = entry.name.removesuffix(".yaml")
                if _CONFIG_NAME.fullmatch(name) is None or entry.is_symlink():
                    continue
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_CONFIG_BYTES:
                    continue
                summaries.append(_summary(name, metadata))
        return sorted(summaries, key=lambda item: item.name.casefold())

    def get(self, name: str) -> StoredDocument:
        name = self.validate_name(name)
        raw, metadata = self._read_regular(self._filename(name))
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UnsafeConfigFile("configuration is not valid UTF-8") from exc
        return StoredDocument(summary=_summary(name, metadata), text=text)

    def validate(self, text: str) -> RunConfig:
        raw = _bounded_utf8(text)
        temp_name = self._write_temp(raw, prefix=".validate-")
        try:
            return _load_or_failure(self._descriptor_path(temp_name))
        finally:
            self._unlink_temp(temp_name)

    def put(self, name: str, text: str) -> tuple[ConfigSummary, RunConfig]:
        name = self.validate_name(name)
        raw = _bounded_utf8(text)
        target = self._filename(name)
        self._reject_unsafe_existing(target)

        temp_name = self._write_temp(raw, prefix=f".{name}-")
        try:
            config = _load_or_failure(self._descriptor_path(temp_name))
            # Re-check immediately before replacement.  os.replace never
            # follows the destination entry, and turns a concurrent symlink
            # into an ordinary file rather than writing through it.
            self._reject_unsafe_existing(target)
            root_fd = self._checked_root_fd()
            os.replace(temp_name, target, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            os.fsync(root_fd)
        finally:
            self._unlink_temp(temp_name)

        metadata = os.stat(target, dir_fd=self._checked_root_fd(), follow_symlinks=False)
        return _summary(name, metadata), config

    def delete(self, name: str) -> None:
        name = self.validate_name(name)
        target = self._filename(name)
        self._reject_unsafe_existing(target, missing_is_error=True)
        try:
            os.unlink(target, dir_fd=self._checked_root_fd())
        except FileNotFoundError as exc:
            raise ConfigNotFound(f"config {name!r} does not exist") from exc
        os.fsync(self._checked_root_fd())

    @staticmethod
    def _filename(name: str) -> str:
        return f"{name}.yaml"

    def _ensure_root(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except FileExistsError as exc:
            raise UnsafeConfigFile("configs directory is not a directory") from exc
        self._check_root()

    def _check_root(self) -> None:
        try:
            metadata = os.lstat(self.root)
        except FileNotFoundError as exc:
            raise UnsafeConfigFile("configs directory disappeared") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise UnsafeConfigFile("configs directory must be a non-symlink directory")

    def _open_root(self) -> int:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            descriptor = os.open(self.root, flags)
        except OSError as exc:
            raise UnsafeConfigFile("configs directory cannot be opened safely") from exc
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            os.close(descriptor)
            raise UnsafeConfigFile("configs directory must be a real directory")
        return descriptor

    def _checked_root_fd(self) -> int:
        try:
            descriptor_info = os.fstat(self._root_fd)
            path_info = os.lstat(self.root)
        except OSError as exc:
            raise UnsafeConfigFile("configs directory disappeared") from exc
        descriptor_identity = (descriptor_info.st_dev, descriptor_info.st_ino)
        path_identity = (path_info.st_dev, path_info.st_ino)
        if (
            not stat.S_ISDIR(descriptor_info.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or not stat.S_ISDIR(path_info.st_mode)
            or descriptor_identity != self._root_identity
            or path_identity != self._root_identity
        ):
            raise UnsafeConfigFile("configs directory identity changed")
        return self._root_fd

    def _descriptor_path(self, filename: str) -> Path:
        return Path(f"/proc/self/fd/{self._checked_root_fd()}/{filename}")

    def _read_regular(self, filename: str) -> tuple[bytes, os.stat_result]:
        root_fd = self._checked_root_fd()
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(filename, flags, dir_fd=root_fd)
        except FileNotFoundError as exc:
            raise ConfigNotFound(
                f"config {filename.removesuffix('.yaml')!r} does not exist"
            ) from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENXIO}:
                raise UnsafeConfigFile("config must be a regular non-symlink file") from exc
            raise
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise UnsafeConfigFile("config must be a regular file")
            if metadata.st_size > MAX_CONFIG_BYTES:
                raise ConfigTooLarge(f"configuration exceeds {MAX_CONFIG_BYTES} bytes")
            chunks: list[bytes] = []
            remaining = MAX_CONFIG_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > MAX_CONFIG_BYTES:
                raise ConfigTooLarge(f"configuration exceeds {MAX_CONFIG_BYTES} bytes")
            return raw, metadata
        finally:
            os.close(descriptor)

    def _write_temp(self, raw: bytes, *, prefix: str) -> str:
        root_fd = self._checked_root_fd()
        temp_name = f"{prefix}{secrets.token_hex(16)}.yaml"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(temp_name, flags, 0o600, dir_fd=root_fd)
        try:
            view = memoryview(raw)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write while storing configuration")
                view = view[written:]
            os.fsync(descriptor)
        except BaseException:
            try:
                os.unlink(temp_name, dir_fd=root_fd)
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(descriptor)
        return temp_name

    def _unlink_temp(self, temp_name: str) -> None:
        try:
            os.unlink(temp_name, dir_fd=self._root_fd)
        except FileNotFoundError:
            pass

    def _reject_unsafe_existing(
        self, filename: str, *, missing_is_error: bool = False
    ) -> None:
        root_fd = self._checked_root_fd()
        try:
            metadata = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            if missing_is_error:
                raise ConfigNotFound(
                    f"config {filename.removesuffix('.yaml')!r} does not exist"
                ) from None
            return
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise UnsafeConfigFile("config must be a regular non-symlink file")


def _bounded_utf8(text: str) -> bytes:
    if not isinstance(text, str):
        raise ConfigValidationFailure(
            (ValidationIssue(loc=[], msg="configuration body must be text", type="string_type"),)
        )
    raw = text.encode("utf-8")
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigTooLarge(f"configuration exceeds {MAX_CONFIG_BYTES} bytes")
    return raw


def _load_or_failure(path: Path) -> RunConfig:
    try:
        return load_config(path)
    except ValidationError as exc:
        issues = tuple(
            ValidationIssue(
                loc=list(error["loc"]),
                msg=str(error["msg"]),
                type=str(error["type"]),
            )
            for error in exc.errors(include_url=False, include_context=False, include_input=False)
        )
        raise ConfigValidationFailure(issues) from exc
    except yaml.MarkedYAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location: list[str | int] = ["yaml"]
        if mark is not None:
            location.extend((int(mark.line) + 1, int(mark.column) + 1))
        message = getattr(exc, "problem", None) or "invalid YAML"
        raise ConfigValidationFailure(
            (ValidationIssue(loc=location, msg=str(message), type="yaml_parse"),)
        ) from exc
    except (UnicodeError, ValueError) as exc:
        raise ConfigValidationFailure(
            (ValidationIssue(loc=[], msg=str(exc), type="config_value"),)
        ) from exc


def _summary(name: str, metadata: os.stat_result) -> ConfigSummary:
    return ConfigSummary(name=name, size_bytes=metadata.st_size, modified_ns=metadata.st_mtime_ns)


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "ConfigNotFound",
    "ConfigStore",
    "ConfigStoreError",
    "ConfigTooLarge",
    "ConfigValidationFailure",
    "InvalidConfigName",
    "StoredDocument",
    "UnsafeConfigFile",
]
