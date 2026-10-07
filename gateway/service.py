"""Request orchestration: auth scope -> rate limit -> cache -> route -> retry/fallback."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator

from gateway.cache import Cache, cache_key
from gateway.config import GatewayConfig, TenantConfig
from gateway.providers.base import Provider, ProviderError, ProviderResult
from gateway.ratelimit import RateLimiter
from gateway.resilience import CircuitRegistry, LatencyTracker
from gateway.router import Router, Target
from gateway.schemas import Attempt, ChatRequest, ChatResponse, Usage
from gateway.telemetry import Metrics, UsageRecord, UsageStore, cost_usd


class RouteForbidden(Exception):
    pass


class UpstreamClientError(Exception):
    """The request itself is invalid (HTTP 400 upstream); retrying elsewhere won't help."""


class AllTargetsFailed(Exception):
    def __init__(self, attempts: list[Attempt]):
        super().__init__("all targets failed")
        self.attempts = attempts


def estimate_tokens(req: ChatRequest) -> int:
    prompt_chars = sum(len(m.content) for m in req.messages) + len(req.system or "")
    return prompt_chars // 4 + req.max_tokens  # reserve the full output budget


class Gateway:
    def __init__(
        self,
        cfg: GatewayConfig,
        providers: dict[str, Provider],
        cache: Cache | None = None,
        limiter: RateLimiter | None = None,
        circuits: CircuitRegistry | None = None,
        sleep=asyncio.sleep,
    ):
        self.cfg = cfg
        self.providers = providers
        self.cache = cache
        self.limiter = limiter or RateLimiter()
        self.circuits = circuits or CircuitRegistry(
            cfg.circuit.failure_threshold, cfg.circuit.reset_timeout_seconds
        )
        self.latency = LatencyTracker()
        self.router = Router(cfg, self.circuits, self.latency)
        self.metrics = Metrics()
        self.usage = UsageStore()
        self._sleep = sleep

    # ---- shared steps -------------------------------------------------------

    def _admit(self, tenant: TenantConfig, req: ChatRequest):
        route_name, route = self.router.resolve(req.model)
        if tenant.allowed_routes is not None and route_name not in tenant.allowed_routes:
            raise RouteForbidden(route_name)
        estimate = estimate_tokens(req)
        self.limiter.acquire(tenant.name, tenant.requests_per_minute, tenant.tokens_per_minute, estimate)
        return route_name, route, estimate

    def _finish(
        self, tenant: TenantConfig, route_name: str, target: Target, result: ProviderResult,
        estimate: int, started: float, attempts: list[Attempt], cached: bool,
    ) -> ChatResponse:
        cost = 0.0 if cached else cost_usd(
            self.cfg.pricing.get(target.model), result.input_tokens, result.output_tokens
        )
        if not cached:
            self.limiter.reconcile(
                tenant.name, estimate, result.input_tokens + result.output_tokens,
                tenant.tokens_per_minute,
            )
        latency_ms = (time.perf_counter() - started) * 1000
        self._record(tenant, route_name, target, "ok", result.input_tokens, result.output_tokens,
                     cost, latency_ms, cached)
        if len(attempts) > 1 and not cached:
            self.metrics.fallbacks.labels(route_name).inc()
        return ChatResponse(
            id=f"gw_{uuid.uuid4().hex[:16]}",
            route=route_name,
            provider=target.provider,
            model=result.model,
            content=result.text,
            stop_reason=result.stop_reason,
            usage=Usage(input_tokens=result.input_tokens, output_tokens=result.output_tokens),
            cost_usd=round(cost, 8),
            cached=cached,
            latency_ms=round(latency_ms, 2),
            attempts=attempts,
        )

    def _record(self, tenant, route_name, target, status, tin, tout, cost, latency_ms, cached):
        m = self.metrics
        m.requests.labels(tenant.name, route_name, target.provider, target.model, status).inc()
        m.tokens.labels(tenant.name, target.model, "input").inc(tin)
        m.tokens.labels(tenant.name, target.model, "output").inc(tout)
        m.cost.labels(tenant.name, target.model).inc(cost)
        self.usage.add(UsageRecord(
            ts=time.time(), tenant=tenant.name, route=route_name, provider=target.provider,
            model=target.model, input_tokens=tin, output_tokens=tout, cost_usd=cost,
            latency_ms=latency_ms, cached=cached, status=status,
        ))

    def _on_failure(self, target: Target, err: ProviderError, latency_ms: float,
                    attempts: list[Attempt]) -> None:
        self.circuits.get(target.key).record_failure()
        attempts.append(Attempt(target=target.key, ok=False, latency_ms=round(latency_ms, 2),
                                error=f"{err.status or ''} {err}".strip()))
        if err.status == 400:
            raise UpstreamClientError(str(err))

    def _on_success(self, target: Target, latency_ms: float, attempts: list[Attempt]) -> None:
        self.circuits.get(target.key).record_success()
        self.latency.record(target.key, latency_ms)
        self.metrics.latency.labels(target.provider, target.model).observe(latency_ms / 1000)
        attempts.append(Attempt(target=target.key, ok=True, latency_ms=round(latency_ms, 2)))

    def _fail_all(self, tenant, route_name, route, attempts):
        first = Target.parse(route.targets[0])
        self._record(tenant, route_name, first, "error", 0, 0, 0.0, 0.0, False)
        return AllTargetsFailed(attempts)

    # ---- non-streaming ------------------------------------------------------

    async def chat(self, tenant: TenantConfig, req: ChatRequest) -> ChatResponse:
        started = time.perf_counter()
        route_name, route, estimate = self._admit(tenant, req)

        use_cache = self.cache is not None and self.cfg.cache.enabled and req.cache
        key = cache_key(route_name, req) if use_cache else ""
        if use_cache and (hit := await self.cache.get(key)):
            self.metrics.cache_hits.labels(route_name).inc()
            self.limiter.reconcile(tenant.name, estimate, 0, tenant.tokens_per_minute)
            target = Target(hit["provider"], hit["target_model"])
            result = ProviderResult(hit["text"], hit["stop_reason"], hit["input_tokens"],
                                    hit["output_tokens"], hit["model"])
            return self._finish(tenant, route_name, target, result, estimate, started, [], True)

        attempts: list[Attempt] = []
        for target in self.router.order(route):
            provider = self.providers[target.provider]
            for attempt in range(self.cfg.retry.max_attempts_per_target):
                t0 = time.perf_counter()
                try:
                    result = await provider.complete(target.model, req)
                except ProviderError as err:
                    self._on_failure(target, err, (time.perf_counter() - t0) * 1000, attempts)
                    if not err.retryable:
                        break  # e.g. 403/404 on this provider: move to the next target
                    if attempt + 1 < self.cfg.retry.max_attempts_per_target:
                        await self._sleep(self.cfg.retry.backoff_base_seconds * 2**attempt)
                    continue
                self._on_success(target, (time.perf_counter() - t0) * 1000, attempts)
                if use_cache and result.stop_reason != "refusal":
                    await self.cache.set(key, {
                        "provider": target.provider, "target_model": target.model,
                        "model": result.model, "text": result.text,
                        "stop_reason": result.stop_reason, "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                    })
                return self._finish(tenant, route_name, target, result, estimate, started,
                                    attempts, False)
        raise self._fail_all(tenant, route_name, route, attempts)

    # ---- streaming (Server-Sent Events) -------------------------------------

    def chat_stream(self, tenant: TenantConfig, req: ChatRequest) -> AsyncIterator[str]:
        """Admit eagerly (so 403/404/429 become real HTTP errors), then stream SSE frames."""
        started = time.perf_counter()
        admitted = self._admit(tenant, req)
        return self._stream(tenant, req, started, *admitted)

    async def _stream(self, tenant, req, started, route_name, route, estimate) -> AsyncIterator[str]:
        # Fallback to the next target is only possible before the first byte is sent.
        attempts: list[Attempt] = []

        for target in self.router.order(route):
            provider = self.providers[target.provider]
            t0 = time.perf_counter()
            sent_any = False
            try:
                async for item in provider.stream(target.model, req):
                    if isinstance(item, str):
                        sent_any = True
                        yield _sse("delta", {"text": item})
                    else:
                        self._on_success(target, (time.perf_counter() - t0) * 1000, attempts)
                        done = self._finish(tenant, route_name, target, item, estimate, started,
                                            attempts, False)
                        yield _sse("done", done.model_dump(exclude={"content"}))
                        return
            except ProviderError as err:
                try:
                    self._on_failure(target, err, (time.perf_counter() - t0) * 1000, attempts)
                except UpstreamClientError as client_err:
                    yield _sse("error", {"error": str(client_err)})
                    return
                if sent_any:
                    yield _sse("error", {"error": "upstream failed mid-stream", "attempts":
                                         [a.model_dump() for a in attempts]})
                    return
        failure = self._fail_all(tenant, route_name, route, attempts)
        yield _sse("error", {"error": str(failure), "attempts": [a.model_dump() for a in attempts]})


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
