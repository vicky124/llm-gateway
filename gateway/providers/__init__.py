from gateway.config import GatewayConfig
from gateway.providers.base import Provider, ProviderError, ProviderResult
from gateway.providers.mock_provider import MockProvider


def build_providers(cfg: GatewayConfig) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    for name, pc in cfg.providers.items():
        if pc.type == "anthropic":
            from gateway.providers.anthropic_provider import AnthropicProvider

            providers[name] = AnthropicProvider(name)
        elif pc.type == "bedrock":
            from gateway.providers.anthropic_provider import BedrockProvider

            providers[name] = BedrockProvider(name, region=pc.region or "us-east-1")
        else:
            providers[name] = MockProvider(name, **pc.options)
    return providers


__all__ = ["Provider", "ProviderError", "ProviderResult", "MockProvider", "build_providers"]
