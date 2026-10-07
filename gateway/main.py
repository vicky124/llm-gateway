"""FastAPI application: HTTP surface of the gateway."""

from __future__ import annotations

import hmac
import os
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from gateway.cache import MemoryCache, RedisCache
from gateway.config import GatewayConfig, TenantConfig, hash_key, load_config
from gateway.providers import Provider, build_providers
from gateway.ratelimit import RateLimited
from gateway.router import UnknownRoute
from gateway.schemas import ChatRequest, ChatResponse
from gateway.service import AllTargetsFailed, Gateway, RouteForbidden, UpstreamClientError

DASHBOARD = Path(__file__).parent / "dashboard" / "index.html"


def build_cache(cfg: GatewayConfig):
    if not cfg.cache.enabled:
        return None
    if cfg.cache.redis_url:
        return RedisCache(cfg.cache.redis_url, cfg.cache.ttl_seconds)
    return MemoryCache(cfg.cache.ttl_seconds, cfg.cache.max_entries)


def create_app(
    cfg: GatewayConfig | None = None, providers: dict[str, Provider] | None = None
) -> FastAPI:
    cfg = cfg or load_config(os.environ.get("GATEWAY_CONFIG", "config/gateway.yaml"))
    gateway = Gateway(cfg, providers or build_providers(cfg), cache=build_cache(cfg))
    app = FastAPI(title="LLM Gateway", version="0.1.0")
    app.state.gateway = gateway

    def tenant_from_key(authorization: str = Header(default="")) -> TenantConfig:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(401, "missing bearer token")
        digest = hash_key(token)
        for tenant in cfg.tenants:
            if hmac.compare_digest(tenant.api_key_sha256, digest):
                return tenant
        raise HTTPException(401, "invalid API key")

    def admin(tenant: TenantConfig = Depends(tenant_from_key)) -> TenantConfig:
        if not tenant.admin:
            raise HTTPException(403, "admin key required")
        return tenant

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response

    @app.exception_handler(RateLimited)
    async def _rate_limited(_: Request, exc: RateLimited):
        return JSONResponse({"error": exc.reason}, status_code=429,
                            headers={"Retry-After": str(max(1, round(exc.retry_after)))})

    @app.exception_handler(UnknownRoute)
    async def _unknown(_: Request, exc: UnknownRoute):
        return JSONResponse({"error": f"unknown model or route {exc}"}, status_code=404)

    @app.exception_handler(RouteForbidden)
    async def _forbidden(_: Request, exc: RouteForbidden):
        return JSONResponse({"error": f"route {exc} not allowed for this key"}, status_code=403)

    @app.exception_handler(UpstreamClientError)
    async def _client_error(_: Request, exc: UpstreamClientError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(AllTargetsFailed)
    async def _all_failed(_: Request, exc: AllTargetsFailed):
        return JSONResponse(
            {"error": "all upstream targets failed",
             "attempts": [a.model_dump() for a in exc.attempts]},
            status_code=502,
        )

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(gateway.metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/v1/routes")
    async def routes(tenant: TenantConfig = Depends(tenant_from_key)):
        return {
            name: route.model_dump()
            for name, route in cfg.routes.items()
            if tenant.allowed_routes is None or name in tenant.allowed_routes
        }

    @app.post("/v1/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest, tenant: TenantConfig = Depends(tenant_from_key)):
        if req.stream:
            return StreamingResponse(gateway.chat_stream(tenant, req),
                                     media_type="text/event-stream")
        return await gateway.chat(tenant, req)

    @app.get("/admin/usage")
    async def usage(window_minutes: int = 60, _: TenantConfig = Depends(admin)):
        return gateway.usage.summary(window_minutes)

    @app.get("/admin/circuits")
    async def circuits(_: TenantConfig = Depends(admin)):
        return gateway.circuits.snapshot()

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard():
        return FileResponse(DASHBOARD)

    return app


def app_factory() -> FastAPI:  # uvicorn gateway.main:app_factory --factory
    return create_app()
