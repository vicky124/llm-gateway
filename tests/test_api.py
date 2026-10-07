import json

from fastapi.testclient import TestClient

from gateway.main import create_app
from gateway.providers import MockProvider
from tests.conftest import ADMIN_KEY, auth, chat_body, make_config


def test_requires_api_key(client):
    assert client.post("/v1/chat", json=chat_body()).status_code == 401
    assert client.post("/v1/chat", json=chat_body(), headers=auth("wrong")).status_code == 401


def test_chat_success_reports_cost_and_usage(client):
    res = client.post("/v1/chat", json=chat_body("hello world"), headers=auth())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["provider"] == "primary"
    assert body["route"] == "smart"
    assert body["cached"] is False
    assert body["cost_usd"] > 0
    assert res.headers["x-request-id"]


def test_second_identical_request_is_cached(client, providers):
    client.post("/v1/chat", json=chat_body("same"), headers=auth())
    second = client.post("/v1/chat", json=chat_body("same"), headers=auth()).json()
    assert second["cached"] is True
    assert second["cost_usd"] == 0
    assert providers["primary"].calls == 1


def test_cache_can_be_bypassed(client, providers):
    client.post("/v1/chat", json=chat_body("x", cache=False), headers=auth())
    client.post("/v1/chat", json=chat_body("x", cache=False), headers=auth())
    assert providers["primary"].calls == 2


def test_falls_back_to_backup_when_primary_is_down():
    providers = {"primary": MockProvider("primary", fail_times=99), "backup": MockProvider("backup")}
    client = TestClient(create_app(make_config(), providers))
    body = client.post("/v1/chat", json=chat_body(), headers=auth()).json()
    assert body["provider"] == "backup"
    # two retries on primary, then success on backup
    assert [a["ok"] for a in body["attempts"]] == [False, False, True]


def test_retries_transient_error_on_same_target():
    providers = {"primary": MockProvider("primary", fail_times=1), "backup": MockProvider("backup")}
    client = TestClient(create_app(make_config(), providers))
    body = client.post("/v1/chat", json=chat_body(), headers=auth()).json()
    assert body["provider"] == "primary"
    assert providers["backup"].calls == 0


def test_non_retryable_error_skips_straight_to_next_target():
    providers = {"primary": MockProvider("primary", fail_times=99, fail_status=403),
                 "backup": MockProvider("backup")}
    client = TestClient(create_app(make_config(), providers))
    body = client.post("/v1/chat", json=chat_body(), headers=auth()).json()
    assert providers["primary"].calls == 1
    assert body["provider"] == "backup"


def test_bad_request_is_not_retried_anywhere():
    providers = {"primary": MockProvider("primary", fail_times=99, fail_status=400),
                 "backup": MockProvider("backup")}
    client = TestClient(create_app(make_config(), providers))
    res = client.post("/v1/chat", json=chat_body(), headers=auth())
    assert res.status_code == 400
    assert providers["backup"].calls == 0


def test_all_targets_failing_returns_502_with_attempts():
    providers = {"primary": MockProvider("primary", fail_times=99),
                 "backup": MockProvider("backup", fail_times=99)}
    client = TestClient(create_app(make_config(), providers))
    res = client.post("/v1/chat", json=chat_body(), headers=auth())
    assert res.status_code == 502
    assert len(res.json()["attempts"]) == 4


def test_unknown_route_is_404(client):
    res = client.post("/v1/chat", json=chat_body() | {"model": "nope"}, headers=auth())
    assert res.status_code == 404


def test_tenant_route_allow_list(client):
    res = client.post("/v1/chat", json=chat_body() | {"model": "primary/big"}, headers=auth())
    assert res.status_code == 403  # 'direct' is not in the user's allowed_routes
    res = client.post("/v1/chat", json=chat_body() | {"model": "primary/big"}, headers=auth(ADMIN_KEY))
    assert res.status_code == 200


def test_rate_limit_returns_429_with_retry_after(providers):
    cfg = make_config()
    cfg.tenants[0].requests_per_minute = 2
    client = TestClient(create_app(cfg, providers))
    codes = [client.post("/v1/chat", json=chat_body(f"m{i}"), headers=auth()).status_code
             for i in range(3)]
    assert codes == [200, 200, 429]


def test_streaming_emits_deltas_then_done(client):
    with client.stream("POST", "/v1/chat", json=chat_body("stream me", stream=True),
                       headers=auth()) as res:
        assert res.status_code == 200
        raw = "".join(res.iter_text())
    events = [block.split("\n") for block in raw.strip().split("\n\n")]
    names = [e[0].removeprefix("event: ") for e in events]
    assert names[-1] == "done" and set(names[:-1]) == {"delta"}
    text = "".join(json.loads(e[1].removeprefix("data: "))["text"] for e in events[:-1])
    assert "stream me" in text


def test_streaming_rate_limit_is_a_real_http_error(providers):
    cfg = make_config()
    cfg.tenants[0].requests_per_minute = 1
    client = TestClient(create_app(cfg, providers))
    client.post("/v1/chat", json=chat_body("a"), headers=auth())
    res = client.post("/v1/chat", json=chat_body("b", stream=True), headers=auth())
    assert res.status_code == 429


def test_admin_endpoints_require_admin(client):
    client.post("/v1/chat", json=chat_body(), headers=auth())
    assert client.get("/admin/usage", headers=auth()).status_code == 403
    usage = client.get("/admin/usage", headers=auth(ADMIN_KEY)).json()
    assert usage["requests"] == 1
    assert "big" in usage["by_model"]


def test_metrics_endpoint_exposes_counters(client):
    client.post("/v1/chat", json=chat_body(), headers=auth())
    text = client.get("/metrics").text
    assert 'llmgw_requests_total{model="big",provider="primary",route="smart",status="ok",tenant="user"} 1.0' in text
