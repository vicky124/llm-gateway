"""Gateway configuration, loaded from YAML with ${ENV_VAR} expansion."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class ProviderConfig(BaseModel):
    type: Literal["anthropic", "bedrock", "mock"]
    region: str | None = None
    options: dict = Field(default_factory=dict)


class ModelPrice(BaseModel):
    input_per_mtok: float
    output_per_mtok: float


class RouteConfig(BaseModel):
    strategy: Literal["fallback", "cost", "latency", "weighted"] = "fallback"
    targets: list[str] = Field(min_length=1)
    weights: list[float] | None = None

    @model_validator(mode="after")
    def _weights_match(self) -> RouteConfig:
        if self.strategy == "weighted":
            if not self.weights or len(self.weights) != len(self.targets):
                raise ValueError("weighted routes need one weight per target")
        return self


class TenantConfig(BaseModel):
    name: str
    api_key_sha256: str
    requests_per_minute: int = 60
    tokens_per_minute: int = 200_000
    admin: bool = False
    allowed_routes: list[str] | None = None  # None = every route


class CacheConfig(BaseModel):
    enabled: bool = True
    ttl_seconds: int = 300
    max_entries: int = 1_000
    redis_url: str | None = None


class RetryConfig(BaseModel):
    max_attempts_per_target: int = 2
    backoff_base_seconds: float = 0.2


class CircuitConfig(BaseModel):
    failure_threshold: int = 5
    reset_timeout_seconds: float = 30.0


class GatewayConfig(BaseModel):
    providers: dict[str, ProviderConfig]
    pricing: dict[str, ModelPrice] = Field(default_factory=dict)
    routes: dict[str, RouteConfig]
    tenants: list[TenantConfig]
    cache: CacheConfig = CacheConfig()
    retry: RetryConfig = RetryConfig()
    circuit: CircuitConfig = CircuitConfig()

    @model_validator(mode="after")
    def _targets_reference_providers(self) -> GatewayConfig:
        for name, route in self.routes.items():
            for target in route.targets:
                provider = target.split("/", 1)[0]
                if provider not in self.providers:
                    raise ValueError(f"route {name!r} targets unknown provider {provider!r}")
        return self


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


_ENV_REF = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


def expand_env(text: str) -> str:
    """Expand ${VAR} and ${VAR:-default}; an unset VAR without a default becomes ''."""
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), text)


def load_config(path: str | Path) -> GatewayConfig:
    raw = expand_env(Path(path).read_text(encoding="utf-8"))
    return GatewayConfig.model_validate(yaml.safe_load(raw))
