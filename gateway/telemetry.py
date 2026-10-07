"""Prometheus metrics plus an in-memory usage log that backs the dashboard."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass

from prometheus_client import CollectorRegistry, Counter, Histogram

from gateway.config import ModelPrice


def cost_usd(price: ModelPrice | None, input_tokens: int, output_tokens: int) -> float:
    if price is None:
        return 0.0
    return (input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok) / 1_000_000


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "llmgw_requests_total", "Requests by outcome",
            ["tenant", "route", "provider", "model", "status"], registry=self.registry,
        )
        self.tokens = Counter(
            "llmgw_tokens_total", "Tokens processed", ["tenant", "model", "direction"],
            registry=self.registry,
        )
        self.cost = Counter(
            "llmgw_cost_usd_total", "Estimated spend in USD", ["tenant", "model"],
            registry=self.registry,
        )
        self.cache_hits = Counter(
            "llmgw_cache_hits_total", "Cache hits", ["route"], registry=self.registry
        )
        self.fallbacks = Counter(
            "llmgw_fallbacks_total", "Requests served by a non-primary target", ["route"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "llmgw_upstream_latency_seconds", "Upstream latency", ["provider", "model"],
            buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120), registry=self.registry,
        )


@dataclass
class UsageRecord:
    ts: float
    tenant: str
    route: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    cached: bool
    status: str


class UsageStore:
    def __init__(self, max_records: int = 10_000):
        self.records: deque[UsageRecord] = deque(maxlen=max_records)

    def add(self, record: UsageRecord) -> None:
        self.records.append(record)

    def summary(self, window_minutes: int = 60) -> dict:
        now = time.time()
        since = now - window_minutes * 60
        recent = [r for r in self.records if r.ts >= since]

        def group(key: str) -> dict[str, dict]:
            out: dict[str, dict] = defaultdict(lambda: {"requests": 0, "cost_usd": 0.0, "tokens": 0})
            for r in recent:
                bucket = out[getattr(r, key)]
                bucket["requests"] += 1
                bucket["cost_usd"] += r.cost_usd
                bucket["tokens"] += r.input_tokens + r.output_tokens
            return dict(out)

        per_minute: dict[int, dict] = defaultdict(lambda: {"requests": 0, "cost_usd": 0.0, "errors": 0})
        for r in recent:
            minute = int(r.ts // 60 * 60)
            per_minute[minute]["requests"] += 1
            per_minute[minute]["cost_usd"] += r.cost_usd
            per_minute[minute]["errors"] += r.status != "ok"

        ok = [r for r in recent if r.status == "ok"]
        latencies = sorted(r.latency_ms for r in ok if not r.cached)
        return {
            "window_minutes": window_minutes,
            "requests": len(recent),
            "errors": len(recent) - len(ok),
            "cache_hit_rate": (sum(r.cached for r in ok) / len(ok)) if ok else 0.0,
            "cost_usd": sum(r.cost_usd for r in recent),
            "p50_latency_ms": _percentile(latencies, 0.5),
            "p95_latency_ms": _percentile(latencies, 0.95),
            "by_model": group("model"),
            "by_tenant": group("tenant"),
            "timeseries": [{"ts": ts, **v} for ts, v in sorted(per_minute.items())],
            "recent": [asdict(r) for r in list(recent)[-25:]][::-1],
        }


def _percentile(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, int(round(q * (len(sorted_values) - 1))))
    return sorted_values[index]
