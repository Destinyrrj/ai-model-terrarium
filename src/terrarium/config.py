"""Strict, versioned experiment configuration.

Configuration is part of the experimental record.  Unknown fields, duplicate
YAML keys and unsafe YAML tags are rejected instead of being guessed at.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_CONFIG_BYTES = 256 * 1024


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader variant which rejects duplicate mapping keys."""


def _construct_unique_mapping(loader: yaml.Loader, node: yaml.MappingNode, deep: bool = False):
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key: {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class WorldConfig(StrictModel):
    valley: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    initial_location: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_./:-]{0,95}$")
    rain_probability: float = Field(ge=0.0, le=1.0)
    initial_food_per_location: int = Field(ge=0, le=1_000_000)
    hunger_per_tick: int = Field(ge=0, le=100)
    starvation_damage: int = Field(ge=1, le=100)
    starvation_lethal: bool
    poison_damage: int = Field(ge=1, le=100)
    collapse_probability: float = Field(ge=0.0, le=1.0)
    collapse_lethal: bool
    decoy_birth_tick: int = Field(ge=1)


class PopulationConfig(StrictModel):
    size: int = Field(ge=1, le=128)
    generations: int = Field(ge=1, le=10_000)
    lifespan_ticks: int = Field(ge=1, le=1_000_000)
    legacy_tokens: int = Field(ge=1, le=100_000)
    inherited_legacies: int = Field(ge=0, le=32)
    tokenizer: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")


class SandboxConfig(StrictModel):
    backend: Literal["mock", "bubblewrap", "process"]
    network: Literal["none", "provider-proxy", "inherit"] = "none"
    egress_proxy: str | None = Field(default=None, max_length=2048)
    bwrap_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    external_egress_enforced: bool = False
    acknowledge_unsafe_host_execution: bool = False

    @field_validator("egress_proxy")
    @classmethod
    def validate_secret_free_proxy(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
            raise ValueError("egress_proxy contains control characters")
        try:
            parsed = urlsplit(value)
            # Accessing port performs its own range/syntax validation.
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("egress_proxy is not a valid proxy URL") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("egress_proxy must be an absolute http(s) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("egress_proxy must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("egress_proxy must not contain query or fragment data")
        if parsed.path not in {"", "/"}:
            raise ValueError("egress_proxy must not contain a path")
        return value

    @model_validator(mode="after")
    def validate_boundary(self) -> SandboxConfig:
        if self.backend == "process" and not self.acknowledge_unsafe_host_execution:
            raise ValueError("process sandbox requires explicit unsafe-host acknowledgement")
        if self.network == "inherit" and not self.acknowledge_unsafe_host_execution:
            raise ValueError("inherited network requires explicit acknowledgement")
        if self.network == "provider-proxy":
            if not self.egress_proxy:
                raise ValueError("provider-proxy network requires egress_proxy")
            if not self.external_egress_enforced:
                raise ValueError(
                    "provider-proxy requires an externally enforced network boundary"
                )
        elif self.external_egress_enforced:
            raise ValueError(
                "external_egress_enforced is only valid with provider-proxy"
            )
        if self.network == "none" and self.egress_proxy is not None:
            raise ValueError("egress_proxy is invalid when network is disabled")
        if self.backend == "mock" and self.network != "none":
            raise ValueError("mock adapters never receive network access")
        if self.backend == "bubblewrap" and self.bwrap_sha256 is None:
            raise ValueError("bubblewrap backend requires a pinned bwrap_sha256")
        if self.backend != "bubblewrap" and self.bwrap_sha256 is not None:
            raise ValueError("bwrap_sha256 is only valid for the bubblewrap backend")
        return self


class RuntimeConfig(StrictModel):
    adapter: Literal["mock", "subprocess", "claude-code"]
    argv: tuple[str, ...] = ()
    executable_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    provider: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    model_id: str = Field(default="deterministic-mock-v1", min_length=1, max_length=256)
    timeout_seconds: float = Field(gt=0, le=3600)
    max_input_bytes: int = Field(ge=1024, le=16 * 1024 * 1024)
    max_output_bytes: int = Field(ge=256, le=16 * 1024 * 1024)
    max_stderr_bytes: int = Field(ge=1, le=16 * 1024 * 1024)
    sandbox: SandboxConfig

    @field_validator("argv", mode="before")
    @classmethod
    def argv_from_yaml(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_adapter(self) -> RuntimeConfig:
        if self.adapter == "mock":
            if self.argv or self.executable_sha256 is not None:
                raise ValueError("mock adapter must not configure an executable")
            if self.sandbox.backend != "mock":
                raise ValueError("mock adapter requires the mock sandbox")
        else:
            if not self.argv:
                raise ValueError("real adapter requires a fixed argv")
            if self.executable_sha256 is None:
                raise ValueError("real adapter executable must be pinned by SHA-256")
            if self.sandbox.backend == "mock":
                raise ValueError("subprocess adapter cannot use the mock sandbox")
            if self.adapter == "claude-code":
                if len(self.argv) != 1:
                    raise ValueError("claude-code argv may contain only the executable")
                if self.sandbox.backend != "process":
                    raise ValueError("claude-code OAuth requires the acknowledged process backend")
                if self.sandbox.network != "inherit":
                    raise ValueError("claude-code requires inherited network access")
        return self


class BudgetConfig(StrictModel):
    max_calls: int = Field(ge=1)
    max_input_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    max_failures: int = Field(ge=0)


class StorageConfig(StrictModel):
    raw_responses: bool = False
    max_raw_bytes: int = Field(ge=0, le=64 * 1024 * 1024)


class KnowledgeConfig(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    statement: str = Field(min_length=1, max_length=4096)
    kind: Literal["true_rule", "probabilistic_rule", "decoy"]
    keywords: tuple[str, ...] = Field(min_length=1, max_length=32)

    @field_validator("keywords", mode="before")
    @classmethod
    def keywords_from_yaml(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class RunConfig(StrictModel):
    schema_version: Literal[1]
    run_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,96}$")
    seed: int = Field(ge=0, le=2**63 - 1)
    world: WorldConfig
    population: PopulationConfig
    runtime: RuntimeConfig
    budget: BudgetConfig
    storage: StorageConfig
    knowledge: tuple[KnowledgeConfig, ...]

    @field_validator("knowledge", mode="before")
    @classmethod
    def knowledge_from_yaml(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def unique_knowledge_ids(self) -> RunConfig:
        ids = [item.id for item in self.knowledge]
        if len(ids) != len(set(ids)):
            raise ValueError("knowledge IDs must be unique")
        return self

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def load_config(path: str | Path) -> RunConfig:
    config_path = Path(path)
    raw = _read_regular_bounded(config_path)
    if b"\x00" in raw:
        raise ValueError("configuration contains NUL bytes")
    try:
        # The custom loader subclasses SafeLoader solely to add duplicate-key
        # rejection; it has no Python-object constructors.
        document = yaml.load(
            raw.decode("utf-8"), Loader=_UniqueKeySafeLoader  # noqa: S506
        )
    except UnicodeDecodeError as exc:
        raise ValueError("configuration must be valid UTF-8") from exc
    if not isinstance(document, dict):
        raise ValueError("configuration root must be a mapping")
    return RunConfig.model_validate(document)


def _read_regular_bounded(path: Path) -> bytes:
    """Read one regular config file without following its final symlink.

    ``O_NONBLOCK`` prevents a malicious FIFO from hanging before ``fstat`` can
    reject it.  The read is bounded independently of the initial file size so a
    concurrently growing file cannot bypass the limit.
    """

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("configuration must be a regular file")
        if metadata.st_size > MAX_CONFIG_BYTES:
            raise ValueError(f"configuration exceeds {MAX_CONFIG_BYTES} bytes")
        chunks: list[bytes] = []
        remaining = MAX_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError(f"configuration exceeds {MAX_CONFIG_BYTES} bytes")
        return raw
    finally:
        os.close(fd)
