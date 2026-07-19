"""Immutable written inheritance with explicit provenance."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from enum import StrEnum
from typing import Protocol

import tiktoken
from pydantic import BaseModel, ConfigDict, Field, field_validator


class LegacyChannel(StrEnum):
    WRITTEN = "written"
    ORAL = "oral"


class Legacy(BaseModel):
    """An immutable cultural record; text is never rewritten in place."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(pattern=r"^leg_[a-f0-9]{24}$")
    author: str = Field(pattern=r"^[A-Za-z0-9_-]{1,96}$")
    generation: int = Field(ge=0)
    valley: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    channel: LegacyChannel
    text: str = Field(min_length=1, max_length=1_000_000)
    parent_legacy_ids: tuple[str, ...] = Field(max_length=32)

    @field_validator("parent_legacy_ids")
    @classmethod
    def unique_parents(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("parent legacy IDs must be unique")
        if any(not item.startswith("leg_") for item in value):
            raise ValueError("invalid parent legacy ID")
        return value


class TokenCodec(Protocol):
    def truncate(self, text: str, max_tokens: int) -> tuple[str, int]: ...
    def count(self, text: str) -> int: ...


class TiktokenCodec:
    """Provider-pinned tokenizer used for hard, token-level deathbed limits."""

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        self.encoding_name = encoding_name
        self._encoding = tiktoken.get_encoding(encoding_name)

    def truncate(self, text: str, max_tokens: int) -> tuple[str, int]:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        encoded = self._encoding.encode(text, disallowed_special=())
        shortened = encoded[:max_tokens]
        # decode is tokenizer-aware; it does not split by Python characters.
        return self._encoding.decode(shortened), len(shortened)

    def count(self, text: str) -> int:
        return len(self._encoding.encode(text, disallowed_special=()))


class InheritanceManager:
    """Creates immutable legacies and selects parents without owning an RNG.

    `draw_index` must be supplied by the World Engine so inheritance does not
    accidentally introduce a second random source.
    """

    def __init__(self, codec: TokenCodec) -> None:
        self.codec = codec
        self._records: dict[str, Legacy] = {}

    @property
    def records(self) -> tuple[Legacy, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    def register(self, legacy: Legacy) -> None:
        if legacy.id in self._records:
            if self._records[legacy.id] != legacy:
                raise RuntimeError("immutable legacy collision")
            return
        missing = set(legacy.parent_legacy_ids) - self._records.keys()
        if missing:
            raise ValueError(f"unknown parent legacies: {sorted(missing)}")
        self._records[legacy.id] = legacy

    def create(
        self,
        *,
        author: str,
        generation: int,
        valley: str,
        channel: LegacyChannel,
        text: str,
        parent_legacy_ids: Iterable[str],
        max_tokens: int,
    ) -> Legacy:
        parents = tuple(parent_legacy_ids)
        missing = set(parents) - self._records.keys()
        if missing:
            raise ValueError(f"unknown parent legacies: {sorted(missing)}")
        truncated, token_count = self.codec.truncate(text, max_tokens)
        if not truncated.strip() or token_count == 0:
            raise ValueError("empty deathbed text does not create a legacy")
        identity = {
            "author": author,
            "generation": generation,
            "valley": valley,
            "channel": channel.value,
            "text": truncated,
            "parents": parents,
        }
        digest = hashlib.sha256(
            json.dumps(
                identity,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()[:24]
        legacy = Legacy(
            id=f"leg_{digest}",
            author=author,
            generation=generation,
            valley=valley,
            channel=channel,
            text=truncated,
            parent_legacy_ids=parents,
        )
        self.register(legacy)
        return legacy

    def select_parent_ids(
        self,
        *,
        preferred_ids: Iterable[str],
        candidate_ids: Iterable[str],
        count: int,
        draw_index: Callable[[int], int],
    ) -> tuple[str, ...]:
        """Take up to ``count - 1`` recent own records, then one RNG-drawn foreign."""

        if count < 0:
            raise ValueError("count cannot be negative")
        preferred = [item for item in preferred_ids if item in self._records]
        candidates = sorted(
            set(item for item in candidate_ids if item in self._records) - set(preferred)
        )
        selected: list[str] = []
        # Reserve one slot for cross-lineage culture whenever any foreign record
        # exists.  Without this reservation, a mature lineage fills every slot with
        # its own history and the planned "own line + one random foreign" crossover
        # silently stops after a few generations.
        own_limit = count - 1 if candidates and count > 0 else count
        for item in reversed(preferred):
            if item not in selected and len(selected) < own_limit:
                selected.append(item)
        if candidates and len(selected) < count:
            index = draw_index(len(candidates))
            if not 0 <= index < len(candidates):
                raise RuntimeError("world RNG selector returned an invalid index")
            selected.append(candidates.pop(index))
        return tuple(selected)

    @classmethod
    def from_records(cls, codec: TokenCodec, records: Iterable[Legacy]) -> InheritanceManager:
        manager = cls(codec)
        remaining = {record.id: record for record in records}
        while remaining:
            progressed = False
            for legacy_id in sorted(tuple(remaining)):
                record = remaining[legacy_id]
                if set(record.parent_legacy_ids) <= manager._records.keys():
                    manager.register(record)
                    del remaining[legacy_id]
                    progressed = True
            if not progressed:
                raise ValueError("legacy graph contains missing parents or a cycle")
        return manager
