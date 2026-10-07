"""Public request/response shapes of the gateway API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    model: str = Field(description="Route alias (e.g. 'smart') or 'provider/model-id'")
    messages: list[Message] = Field(min_length=1)
    system: str | None = None
    max_tokens: int = Field(1024, ge=1, le=128_000)
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    stream: bool = False
    cache: bool = True


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int


class Attempt(BaseModel):
    target: str
    ok: bool
    latency_ms: float
    error: str | None = None


class ChatResponse(BaseModel):
    id: str
    route: str
    provider: str
    model: str
    content: str
    stop_reason: str | None
    usage: Usage
    cost_usd: float
    cached: bool
    latency_ms: float
    attempts: list[Attempt]
