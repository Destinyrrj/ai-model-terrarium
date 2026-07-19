"""Common, provider-independent agent runtime contracts.

The world treats every model response as untrusted input.  Adapters therefore
return an :class:`AdapterResult` instead of domain objects directly: only the
orchestrator/domain validation boundary may turn ``payload`` into an action.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

type JSONScalar = None | bool | int | float | str
type JSONValue = JSONScalar | list[JSONValue] | dict[str, JSONValue]


class AdapterStatus(StrEnum):
    """Machine-readable outcome of one adapter call."""

    OK = "ok"
    INVALID_OUTPUT = "invalid_output"
    TIMEOUT = "timeout"
    OUTPUT_LIMIT = "output_limit"
    PROCESS_ERROR = "process_error"
    SECURITY_ERROR = "security_error"
    CLOSED = "closed"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class AdapterError:
    """A safe-to-log adapter failure.

    ``message`` must not contain unsanitized terminal output or environment
    values.  Detailed raw stderr belongs in ``AdapterResult.raw_text`` after
    terminal sanitization and size limiting.
    """

    code: str
    message: str
    retryable: bool = False


def _freeze_int_mapping(value: Mapping[str, int] | None) -> Mapping[str, int] | None:
    if value is None:
        return None
    copied: dict[str, int] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError("usage keys must be strings")
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError("usage values must be non-negative integers")
        copied[key] = item
    return MappingProxyType(copied)


def _freeze_metadata(value: Mapping[str, JSONScalar]) -> Mapping[str, JSONScalar]:
    copied: dict[str, JSONScalar] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError("metadata keys must be strings")
        if isinstance(item, (dict, list)):
            raise TypeError("metadata values must be JSON scalars")
        copied[key] = item
    return MappingProxyType(copied)


@dataclass(frozen=True, slots=True)
class AdapterResult:
    """Typed boundary between an adapter and the deterministic world.

    ``usage=None`` is deliberately different from zero usage.  A real CLI that
    omits token accounting must leave it as ``None`` so the budget governor can
    fail closed.  The deterministic mock always reports explicit usage.
    """

    payload: JSONValue | None = None
    raw_text: str | None = None
    usage: Mapping[str, int] | None = None
    status: AdapterStatus = AdapterStatus.OK
    error: AdapterError | None = None
    unsafe_host_execution: bool = False
    metadata: Mapping[str, JSONScalar] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "usage", _freeze_int_mapping(self.usage))
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))
        if self.status is AdapterStatus.OK and self.error is not None:
            raise ValueError("successful adapter results cannot contain an error")
        if self.status is not AdapterStatus.OK and self.error is None:
            raise ValueError("failed adapter results must contain an error")

    @property
    def ok(self) -> bool:
        return self.status is AdapterStatus.OK

    @property
    def parsed_payload(self) -> JSONValue | None:
        """Explicit alias used by callers that distinguish raw and parsed data."""

        return self.payload

    @property
    def parsed(self) -> JSONValue | None:
        """Short compatibility alias for the parsed structured payload."""

        return self.payload

    @classmethod
    def success(
        cls,
        payload: JSONValue,
        *,
        raw_text: str | None = None,
        usage: Mapping[str, int] | None = None,
        unsafe_host_execution: bool = False,
        metadata: Mapping[str, JSONScalar] | None = None,
    ) -> AdapterResult:
        return cls(
            payload=payload,
            raw_text=raw_text,
            usage=usage,
            unsafe_host_execution=unsafe_host_execution,
            metadata=metadata or {},
        )

    @classmethod
    def failure(
        cls,
        status: AdapterStatus,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        raw_text: str | None = None,
        usage: Mapping[str, int] | None = None,
        unsafe_host_execution: bool = False,
        metadata: Mapping[str, JSONScalar] | None = None,
    ) -> AdapterResult:
        if status is AdapterStatus.OK:
            raise ValueError("failure status cannot be 'ok'")
        return cls(
            raw_text=raw_text,
            usage=usage,
            status=status,
            error=AdapterError(code=code, message=message, retryable=retryable),
            unsafe_host_execution=unsafe_host_execution,
            metadata=metadata or {},
        )


@runtime_checkable
class AgentAdapter(Protocol):
    """The complete model-facing interface used by the orchestrator."""

    async def act(self, observation: dict[str, Any]) -> AdapterResult:
        """Return one structured action payload for an observation."""

    async def write_legacy(
        self,
        budget_tokens: int,
        context: dict[str, Any] | None = None,
    ) -> AdapterResult:
        """Return a structured ``{"text": ...}`` deathbed record."""

    async def retell(self, record: dict[str, Any]) -> AdapterResult:
        """Return a structured ``{"text": ...}`` oral retelling."""

    async def answer_survey(self, probe: dict[str, Any]) -> AdapterResult:
        """Answer a structured, out-of-world measurement probe."""

    async def close(self) -> None:
        """Release resources.  Implementations must make this idempotent."""


__all__ = [
    "AdapterError",
    "AdapterResult",
    "AdapterStatus",
    "AgentAdapter",
    "JSONScalar",
    "JSONValue",
]
