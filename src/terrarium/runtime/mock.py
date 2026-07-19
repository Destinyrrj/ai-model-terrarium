"""Deterministic in-process adapter used for safe end-to-end runs and tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from .base import AdapterResult, AdapterStatus, JSONValue


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _estimate_tokens(text: str) -> int:
    """Stable accounting estimate for the mock; it is not a provider tokenizer."""

    if not text:
        return 0
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


class DeterministicMockAdapter:
    """A pure-Python adapter that never starts a process or accesses the network.

    The mock deliberately emits ``noop`` actions.  This exercises the complete
    orchestration/storage path without making an invented policy part of a
    scientific run.  Its output depends only on constructor context and method
    input, not on call ordering or wall-clock state.
    """

    def __init__(
        self,
        agent_id: str,
        inherited_legacies: tuple[str, ...] = (),
        *,
        seed: int = 0,
    ) -> None:
        if not agent_id or not isinstance(agent_id, str):
            raise ValueError("agent_id must be a non-empty string")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise TypeError("seed must be an integer")
        if not all(isinstance(item, str) for item in inherited_legacies):
            raise TypeError("inherited_legacies must contain only strings")
        self.agent_id = agent_id
        self.inherited_legacies = tuple(inherited_legacies)
        self.seed = seed
        self._closed = False

    def set_context(self, inherited_legacies: Sequence[str]) -> None:
        """Replace immutable cultural context before the next call.

        This convenience exists for checkpoint/resume code.  It performs no I/O.
        """

        if self._closed:
            raise RuntimeError("adapter is closed")
        if not all(isinstance(item, str) for item in inherited_legacies):
            raise TypeError("inherited_legacies must contain only strings")
        self.inherited_legacies = tuple(inherited_legacies)

    def _closed_result(self) -> AdapterResult:
        return AdapterResult.failure(
            AdapterStatus.CLOSED,
            "adapter_closed",
            "adapter is closed",
            usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            metadata={"adapter": "mock"},
        )

    def _digest(self, operation: str, value: Any) -> str:
        material = _canonical_json(
            {
                "agent_id": self.agent_id,
                "context": self.inherited_legacies,
                "operation": operation,
                "seed": self.seed,
                "value": value,
            }
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _result(self, operation: str, request: Any, payload: JSONValue) -> AdapterResult:
        request_text = _canonical_json({"operation": operation, "payload": request})
        raw_text = _canonical_json({"payload": payload})
        input_tokens = _estimate_tokens(request_text)
        output_tokens = _estimate_tokens(raw_text)
        return AdapterResult.success(
            payload,
            raw_text=raw_text,
            usage={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
            metadata={"adapter": "mock", "deterministic": True},
        )

    async def act(self, observation: dict[str, Any]) -> AdapterResult:
        if self._closed:
            return self._closed_result()
        # Hashing validates that every input is canonical JSON and makes malformed
        # fixture data fail as a typed result rather than leaking into later code.
        try:
            self._digest("act", observation)
            return self._result(
                "act",
                observation,
                {"agent_id": self.agent_id, "type": "noop"},
            )
        except (TypeError, ValueError, RecursionError):
            return AdapterResult.failure(
                AdapterStatus.INVALID_OUTPUT,
                "invalid_mock_input",
                "mock input is not bounded JSON data",
                usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                metadata={"adapter": "mock"},
            )

    async def write_legacy(
        self,
        budget_tokens: int,
        context: dict[str, Any] | None = None,
    ) -> AdapterResult:
        if self._closed:
            return self._closed_result()
        if isinstance(budget_tokens, bool) or not isinstance(budget_tokens, int):
            raise TypeError("budget_tokens must be an integer")
        if budget_tokens < 0:
            raise ValueError("budget_tokens must be non-negative")

        context_summary = context.get("memory_summary") if isinstance(context, dict) else None
        if isinstance(context_summary, str) and context_summary:
            text = f"Remembered experience: {context_summary}"
        elif self.inherited_legacies:
            source = " | ".join(self.inherited_legacies)
            text = f"Inherited record retained: {source}"
        else:
            text = "No ancestral observations were available."

        # The mock estimate is four UTF-8 bytes per token, matching its accounting
        # function.  Real adapters must truncate with their provider tokenizer.
        max_bytes = budget_tokens * 4
        encoded = text.encode("utf-8")[:max_bytes]
        while True:
            try:
                text = encoded.decode("utf-8")
                break
            except UnicodeDecodeError:
                encoded = encoded[:-1]
        request = {"budget_tokens": budget_tokens, "context": context}
        try:
            self._digest("write_legacy", request)
            return self._result("write_legacy", request, {"text": text})
        except (TypeError, ValueError, RecursionError):
            return AdapterResult.failure(
                AdapterStatus.INVALID_OUTPUT,
                "invalid_mock_input",
                "mock input is not bounded JSON data",
                usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                metadata={"adapter": "mock"},
            )

    async def retell(self, record: dict[str, Any]) -> AdapterResult:
        if self._closed:
            return self._closed_result()
        try:
            mark = self._digest("retell", record)[:12]
        except (TypeError, ValueError, RecursionError):
            return AdapterResult.failure(
                AdapterStatus.INVALID_OUTPUT,
                "invalid_mock_input",
                "mock input is not bounded JSON data",
                usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                metadata={"adapter": "mock"},
            )
        event = record.get("type", "event")
        subject = record.get("agent", record.get("agent_id", "someone"))
        text = f"I witnessed {event} involving {subject}. Record {mark}."
        return self._result("retell", record, {"text": text})

    async def answer_survey(self, probe: dict[str, Any]) -> AdapterResult:
        if self._closed:
            return self._closed_result()
        try:
            mark = self._digest("answer_survey", probe)[:12]
        except (TypeError, ValueError, RecursionError):
            return AdapterResult.failure(
                AdapterStatus.INVALID_OUTPUT,
                "invalid_mock_input",
                "mock input is not bounded JSON data",
                usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                metadata={"adapter": "mock"},
            )
        probe_id = probe.get("id")
        payload: dict[str, JSONValue] = {
            "answer": "unknown",
            "confidence": 0.0,
            "record": mark,
        }
        if isinstance(probe_id, (str, int)) and not isinstance(probe_id, bool):
            payload["probe_id"] = probe_id
        return self._result("answer_survey", probe, payload)

    async def close(self) -> None:
        self._closed = True


# Concise alias for config/factory code.
MockAdapter = DeterministicMockAdapter


__all__ = ["DeterministicMockAdapter", "MockAdapter"]
