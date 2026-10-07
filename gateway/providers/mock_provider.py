"""Deterministic provider for local development, demos and tests (no API key needed)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from gateway.providers.base import ProviderError, ProviderResult, StreamItem
from gateway.schemas import ChatRequest


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class MockProvider:
    def __init__(
        self,
        name: str,
        *,
        latency_ms: float = 0,
        fail_times: int = 0,
        fail_status: int = 503,
        reply: str | None = None,
    ):
        self.name = name
        self.latency_ms = latency_ms
        self.fail_times = fail_times
        self.fail_status = fail_status
        self.reply = reply
        self.calls = 0

    async def complete(self, model: str, req: ChatRequest) -> ProviderResult:
        self.calls += 1
        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000)
        if self.calls <= self.fail_times:
            retryable = self.fail_status >= 500 or self.fail_status == 429
            raise ProviderError(
                f"mock failure {self.calls}", retryable=retryable, status=self.fail_status
            )
        prompt = req.messages[-1].content
        text = self.reply or f"[{self.name}:{model}] echo: {prompt[:200]}"
        prompt_chars = sum(len(m.content) for m in req.messages) + len(req.system or "")
        return ProviderResult(
            text=text,
            stop_reason="end_turn",
            input_tokens=max(1, prompt_chars // 4),
            output_tokens=estimate_tokens(text),
            model=model,
        )

    async def stream(self, model: str, req: ChatRequest) -> AsyncIterator[StreamItem]:
        result = await self.complete(model, req)
        for word in result.text.split(" "):
            yield word + " "
        yield result
