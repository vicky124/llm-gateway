"""Provider adapter tests against a stubbed SDK client (no network)."""

from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from gateway.providers.anthropic_provider import AnthropicProvider, BedrockProvider, map_error
from gateway.schemas import ChatRequest


def fake_message(model="claude-opus-5-5"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text="hi")],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=11, output_tokens=3),
        model=model,
    )


class Recorder:
    def __init__(self):
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return fake_message(kwargs["model"])


def stub_client():
    return SimpleNamespace(messages=Recorder(), beta=SimpleNamespace(messages=Recorder()))


REQ = ChatRequest(model="x", messages=[{"role": "user", "content": "yo"}], system="be brief",
                  effort="low", max_tokens=100)


async def test_opus_uses_server_side_fallback_on_beta_endpoint():
    client = stub_client()
    result = await AnthropicProvider("anthropic", client).complete("claude-opus-5-5", REQ)
    sent = client.beta.messages.kwargs
    assert sent["fallbacks"] == "default"
    assert sent["betas"] == ["server-side-fallback-2026-07-01"]
    assert sent["output_config"] == {"effort": "low"}
    assert sent["system"] == "be brief"
    assert result.text == "hi"  # thinking blocks dropped
    assert (result.input_tokens, result.output_tokens) == (11, 3)


async def test_haiku_uses_plain_messages_endpoint():
    client = stub_client()
    await AnthropicProvider("anthropic", client).complete("claude-haiku-4-5", REQ)
    assert client.messages.kwargs is not None
    assert "fallbacks" not in client.messages.kwargs


async def test_bedrock_never_sends_server_fallback():
    client = stub_client()
    await BedrockProvider("bedrock", "us-east-1", client).complete("anthropic.claude-opus-5-5", REQ)
    assert "fallbacks" not in client.messages.kwargs


def _status_error(cls, code):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(code, request=request), body=None)


@pytest.mark.parametrize(
    ("exc", "retryable", "status"),
    [
        (_status_error(anthropic.RateLimitError, 429), True, 429),
        (_status_error(anthropic.InternalServerError, 529), True, 529),
        (_status_error(anthropic.BadRequestError, 400), False, 400),
        (_status_error(anthropic.PermissionDeniedError, 403), False, 403),
    ],
)
def test_error_mapping(exc, retryable, status):
    err = map_error(exc)
    assert (err.retryable, err.status) == (retryable, status)


def test_connection_errors_are_retryable():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    assert map_error(anthropic.APIConnectionError(request=request)).retryable
