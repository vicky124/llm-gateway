"""Claude via the Anthropic API (and, by subclass, via Amazon Bedrock)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anthropic

from gateway.providers.base import ProviderError, ProviderResult, StreamItem
from gateway.schemas import ChatRequest

# Models that accept the server-side `fallbacks: "default"` parameter on the Claude API.
# On a safety-classifier refusal the API re-runs the request on Anthropic's recommended
# fallback model inside the same call, so the gateway never sees the refusal.
SERVER_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-1"}
SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def map_error(exc: anthropic.APIError) -> ProviderError:
    # Most specific first: RateLimitError is an APIStatusError subclass.
    if isinstance(exc, anthropic.RateLimitError):
        return ProviderError("upstream rate limited", retryable=True, status=429)
    if isinstance(exc, anthropic.APIStatusError):
        retryable = exc.status_code >= 500 or exc.status_code in (408, 409)
        return ProviderError(exc.message, retryable=retryable, status=exc.status_code)
    if isinstance(exc, anthropic.APIConnectionError):  # includes APITimeoutError
        return ProviderError("upstream connection error", retryable=True)
    return ProviderError(str(exc), retryable=False)


class AnthropicProvider:
    supports_server_fallback = True

    def __init__(self, name: str, client: Any | None = None):
        self.name = name
        # The gateway owns retries and fallback, so the SDK's own retries are disabled.
        self.client = client or anthropic.AsyncAnthropic(max_retries=0)

    def _request(self, model: str, req: ChatRequest) -> tuple[Any, dict[str, Any]]:
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": req.max_tokens,
            "messages": [m.model_dump() for m in req.messages],
        }
        if req.system:
            params["system"] = req.system
        if req.effort:
            params["output_config"] = {"effort": req.effort}
        if self.supports_server_fallback and model in SERVER_FALLBACK_MODELS:
            params["betas"] = [SERVER_FALLBACK_BETA]
            params["fallbacks"] = "default"
            return self.client.beta.messages, params
        return self.client.messages, params

    @staticmethod
    def _result(message: Any) -> ProviderResult:
        # Thinking blocks are skipped: callers get the final answer text only.
        text = "".join(b.text for b in message.content if b.type == "text")
        return ProviderResult(
            text=text,
            stop_reason=message.stop_reason,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            model=message.model,
        )

    async def complete(self, model: str, req: ChatRequest) -> ProviderResult:
        api, params = self._request(model, req)
        try:
            message = await api.create(**params)
        except anthropic.APIError as exc:
            raise map_error(exc) from exc
        return self._result(message)

    async def stream(self, model: str, req: ChatRequest) -> AsyncIterator[StreamItem]:
        api, params = self._request(model, req)
        try:
            async with api.stream(**params) as stream:
                async for text in stream.text_stream:
                    yield text
                message = await stream.get_final_message()
        except anthropic.APIError as exc:
            raise map_error(exc) from exc
        yield self._result(message)


class BedrockProvider(AnthropicProvider):
    """Claude on Amazon Bedrock through the Messages-API (Mantle) endpoint.

    Bedrock model IDs carry an `anthropic.` prefix, e.g. `anthropic.claude-opus-5-5`.
    Server-side `fallbacks` is a Claude API feature, so it is not sent here; the
    gateway's own cross-provider fallback covers this path instead.
    """

    supports_server_fallback = False

    def __init__(self, name: str, region: str, client: Any | None = None):
        super().__init__(
            name, client or anthropic.AsyncAnthropicBedrockMantle(aws_region=region, max_retries=0)
        )
