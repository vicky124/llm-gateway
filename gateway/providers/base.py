"""Provider contract shared by every model backend."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from gateway.schemas import ChatRequest


@dataclass
class ProviderResult:
    text: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    model: str


class ProviderError(Exception):
    """A failed provider call. `retryable` drives retry/fallback decisions."""

    def __init__(self, message: str, *, retryable: bool, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


# A stream yields text deltas (str) and ends with exactly one ProviderResult.
StreamItem = str | ProviderResult


class Provider(Protocol):
    name: str

    async def complete(self, model: str, req: ChatRequest) -> ProviderResult: ...

    def stream(self, model: str, req: ChatRequest) -> AsyncIterator[StreamItem]: ...
