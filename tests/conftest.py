import pytest
from fastapi.testclient import TestClient

from gateway.config import GatewayConfig, hash_key
from gateway.main import create_app
from gateway.providers import MockProvider

USER_KEY = "sk-test-user"
ADMIN_KEY = "sk-test-admin"


def make_config(**overrides) -> GatewayConfig:
    base = {
        "providers": {"primary": {"type": "mock"}, "backup": {"type": "mock"}},
        "pricing": {
            "big": {"input_per_mtok": 10.0, "output_per_mtok": 50.0},
            "small": {"input_per_mtok": 1.0, "output_per_mtok": 5.0},
        },
        "routes": {
            "smart": {"strategy": "fallback", "targets": ["primary/big", "backup/big"]},
            "cheap": {"strategy": "cost", "targets": ["primary/big", "backup/small"]},
        },
        "tenants": [
            {"name": "user", "api_key_sha256": hash_key(USER_KEY), "allowed_routes": ["smart", "cheap"]},
            {"name": "admin", "api_key_sha256": hash_key(ADMIN_KEY), "admin": True},
        ],
        "retry": {"max_attempts_per_target": 2, "backoff_base_seconds": 0},
    }
    base.update(overrides)
    return GatewayConfig.model_validate(base)


@pytest.fixture
def providers():
    return {"primary": MockProvider("primary"), "backup": MockProvider("backup")}


@pytest.fixture
def client(providers):
    return TestClient(create_app(make_config(), providers))


def auth(key: str = USER_KEY) -> dict:
    return {"Authorization": f"Bearer {key}"}


def chat_body(text: str = "hello", **extra) -> dict:
    return {"model": "smart", "messages": [{"role": "user", "content": text}], "max_tokens": 64, **extra}
