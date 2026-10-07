import random

import pytest

from gateway.cache import MemoryCache
from gateway.config import GatewayConfig, expand_env
from gateway.ratelimit import RateLimited, RateLimiter, TokenBucket
from gateway.resilience import CircuitBreaker, CircuitRegistry, CircuitState, LatencyTracker
from gateway.router import Router
from tests.conftest import make_config


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


# ---- circuit breaker ----------------------------------------------------------

def test_circuit_opens_after_threshold_and_half_opens_after_timeout():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout=10, clock=clock)
    breaker.record_failure()
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN and not breaker.allow()
    clock.now = 10
    assert breaker.allow() and breaker.state is CircuitState.HALF_OPEN
    breaker.record_failure()  # failed probe re-opens immediately
    assert breaker.state is CircuitState.OPEN
    clock.now = 20
    breaker.allow()
    breaker.record_success()
    assert breaker.state is CircuitState.CLOSED and breaker.failures == 0


# ---- router -------------------------------------------------------------------

def router_for(cfg: GatewayConfig, **kw) -> Router:
    return Router(cfg, kw.get("circuits", CircuitRegistry()), kw.get("latency", LatencyTracker()),
                  kw.get("rng"))


def test_cost_strategy_orders_cheapest_first():
    cfg = make_config()
    _, route = router_for(cfg).resolve("cheap")
    assert [t.key for t in router_for(cfg).order(route)] == ["backup/small", "primary/big"]


def test_latency_strategy_prefers_fastest_measured_target():
    cfg = make_config(routes={"r": {"strategy": "latency", "targets": ["primary/big", "backup/big"]}})
    latency = LatencyTracker()
    latency.record("primary/big", 900)
    latency.record("backup/big", 100)
    router = router_for(cfg, latency=latency)
    assert router.order(cfg.routes["r"])[0].key == "backup/big"


def test_open_circuit_target_is_skipped_unless_all_are_open():
    cfg = make_config()
    circuits = CircuitRegistry(failure_threshold=1)
    router = router_for(cfg, circuits=circuits)
    circuits.get("primary/big").record_failure()
    assert [t.key for t in router.order(cfg.routes["smart"])] == ["backup/big"]
    circuits.get("backup/big").record_failure()
    assert len(router.order(cfg.routes["smart"])) == 2


def test_weighted_strategy_respects_weights():
    cfg = make_config(routes={"r": {"strategy": "weighted", "targets": ["primary/big", "backup/big"],
                                    "weights": [0.9, 0.1]}})
    router = router_for(cfg, rng=random.Random(7))
    firsts = [router.order(cfg.routes["r"])[0].provider for _ in range(1000)]
    assert 850 < firsts.count("primary") < 950


def test_weighted_route_requires_matching_weights():
    with pytest.raises(ValueError):
        make_config(routes={"r": {"strategy": "weighted", "targets": ["primary/big"]}})


def test_config_rejects_route_to_unknown_provider():
    with pytest.raises(ValueError):
        make_config(routes={"r": {"targets": ["ghost/model"]}})


# ---- rate limiting ------------------------------------------------------------

def test_token_bucket_refills_over_time():
    clock = FakeClock()
    bucket = TokenBucket(capacity=2, refill_per_second=1, clock=clock)
    assert bucket.try_consume(2) == 0
    assert bucket.try_consume(1) == pytest.approx(1.0)
    clock.now = 1
    assert bucket.try_consume(1) == 0


def test_limiter_refunds_request_when_token_budget_rejects():
    limiter = RateLimiter(clock=FakeClock())
    with pytest.raises(RateLimited, match="tokens"):
        limiter.acquire("t", rpm=10, tpm=100, estimated_tokens=100)
        limiter.acquire("t", rpm=10, tpm=100, estimated_tokens=50)
    requests_bucket = limiter._buckets["t"][0]
    assert requests_bucket.tokens == 9  # first request only


def test_limiter_reconcile_credits_unused_reservation():
    limiter = RateLimiter(clock=FakeClock())
    limiter.acquire("t", rpm=10, tpm=1000, estimated_tokens=800)
    limiter.reconcile("t", estimated_tokens=800, actual_tokens=100, tpm=1000)
    limiter.acquire("t", rpm=10, tpm=1000, estimated_tokens=800)  # fits again


# ---- cache --------------------------------------------------------------------

async def test_memory_cache_expires_and_evicts():
    clock = FakeClock()
    cache = MemoryCache(ttl_seconds=10, max_entries=2, clock=clock)
    await cache.set("a", {"v": 1})
    await cache.set("b", {"v": 2})
    await cache.get("a")  # 'a' becomes most recently used
    await cache.set("c", {"v": 3})
    assert await cache.get("b") is None
    assert await cache.get("a") == {"v": 1}
    clock.now = 11
    assert await cache.get("a") is None


def test_env_expansion_supports_defaults(monkeypatch):
    monkeypatch.setenv("SET_VAR", "x")
    monkeypatch.delenv("UNSET_VAR", raising=False)
    assert expand_env("${SET_VAR} ${UNSET_VAR:-fallback} [${UNSET_VAR}]") == "x fallback []"
