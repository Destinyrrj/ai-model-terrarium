"""Stable JSON contracts exposed by the GUI backend."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfigDocument(ApiModel):
    yaml: str = Field(max_length=256 * 1024)


class ConfigSummary(ApiModel):
    name: str
    size_bytes: int = Field(ge=0)
    modified_ns: int = Field(ge=0)


class ConfigList(ApiModel):
    configs: list[ConfigSummary]


class StoredConfig(ConfigSummary):
    yaml: str


class ValidationIssue(ApiModel):
    loc: list[str | int]
    msg: str
    type: str


class ConfigValidationResult(ApiModel):
    valid: bool
    digest: str | None = None
    config: dict[str, object] | None = None
    errors: list[ValidationIssue] = Field(default_factory=list)


class ConfigWriteResult(ApiModel):
    name: str
    digest: str
    size_bytes: int = Field(ge=0)
    modified_ns: int = Field(ge=0)
