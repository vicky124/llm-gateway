"""Resolve a request's `model` field to an ordered list of provider targets."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from gateway.config import GatewayConfig, RouteConfig
from gateway.resilience import CircuitRegistry, LatencyTracker


class UnknownRoute(Exception):
    pass


@dataclass(frozen=True)
class Target:
    provider: str
    model: str

    @classmethod
    def parse(cls, spec: str) -> Target:
        provider, _, model = spec.partition("/")
        if not model:
            raise ValueError(f"target must look like 'provider/model', got {spec!r}")
        return cls(provider, model)

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}"


class Router:
    def __init__(
        self,
        cfg: GatewayConfig,
        circuits: CircuitRegistry,
        latency: LatencyTracker,
        rng: random.Random | None = None,
    ):
        self.cfg = cfg
        self.circuits = circuits
        self.latency = latency
        self.rng = rng or random.Random()

    def resolve(self, model_field: str) -> tuple[str, RouteConfig]:
        if model_field in self.cfg.routes:
            return model_field, self.cfg.routes[model_field]
        provider = model_field.partition("/")[0]
        if "/" in model_field and provider in self.cfg.providers:
            return "direct", RouteConfig(strategy="fallback", targets=[model_field])
        raise UnknownRoute(model_field)

    def order(self, route: RouteConfig) -> list[Target]:
        targets = [Target.parse(t) for t in route.targets]
        if route.strategy == "cost":
            targets.sort(key=self._price)
        elif route.strategy == "latency":
            # Unmeasured targets sort first so every target gets explored.
            targets.sort(key=lambda t: self.latency.get(t.key) or 0.0)
        elif route.strategy == "weighted":
            targets = self._weighted_shuffle(targets, list(route.weights or []))

        healthy = [t for t in targets if self.circuits.get(t.key).allow()]
        # If every circuit is open, try them anyway rather than failing without a call.
        return healthy or targets

    def _price(self, target: Target) -> float:
        price = self.cfg.pricing.get(target.model)
        return price.input_per_mtok + price.output_per_mtok if price else math.inf

    def _weighted_shuffle(self, targets: list[Target], weights: list[float]) -> list[Target]:
        # Weighted sampling without replacement: primary pick by weight, rest as fallbacks.
        pool = list(zip(targets, weights, strict=True))
        ordered: list[Target] = []
        while pool:
            pick = self.rng.choices(range(len(pool)), weights=[w for _, w in pool])[0]
            ordered.append(pool.pop(pick)[0])
        return ordered
