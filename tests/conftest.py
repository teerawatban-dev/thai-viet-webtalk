"""Shared fixtures for the THAI_VIET_WEBTALK test-suite.

The app is exercised with the offline providers only (``STT_PROVIDER=mock`` and
``TRANSLATE_PROVIDER=passthrough``) so the tests never touch the network and
never need an API key.

``backend.config.get_settings`` is ``lru_cache``-d, so every fixture that
changes the environment must clear that cache *before* the FastAPI lifespan
runs (the lifespan is what builds the provider/translator/registry).
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from backend.config import get_settings

#: Every env var ``backend.config.Settings`` reads. Wiped before each test so a
#: developer's shell or a stray ``.env`` cannot leak into the assertions.
APP_ENV_KEYS = (
    "STT_PROVIDER",
    "STT_API_KEY",
    "STT_MODEL",
    "STT_WS_URL",
    "TRANSLATE_PROVIDER",
    "TRANSLATE_API_KEY",
    "TRANSLATE_MODEL",
    "TRANSLATE_BASE_URL",
    "TRANSLATE_TIMEOUT_S",
    "DEFAULT_SRC_LANG",
    "TARGET_LANGS",
    "MAX_ROOM_CLIENTS",
    "MAX_ROOMS",
    "ROOM_TTL_SECONDS",
    "LOG_LEVEL",
    "TTS_ENABLED",
)

DEFAULT_TEST_ENV = {
    "STT_PROVIDER": "mock",
    "STT_API_KEY": "",
    "TRANSLATE_PROVIDER": "passthrough",
    "TRANSLATE_API_KEY": "",
    "DEFAULT_SRC_LANG": "th",
    "TARGET_LANGS": "th,vi,en",
    "MAX_ROOM_CLIENTS": "6",
    "MAX_ROOMS": "100",
    "ROOM_TTL_SECONDS": "300",
    "LOG_LEVEL": "WARNING",
    "TTS_ENABLED": "false",
}


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """Reset app env vars + the settings cache around every test."""
    for key in APP_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture
def api_client(monkeypatch):
    """Factory returning ``TestClient`` context managers with custom env.

    Usage::

        with api_client(MAX_ROOM_CLIENTS=1) as client:
            ...

    The app is imported inside the factory (after the env vars are in place and
    the settings cache is cleared) so its lifespan picks up the overrides.
    """

    @contextmanager
    def factory(**env_overrides):
        merged = dict(DEFAULT_TEST_ENV)
        merged.update({key: str(value) for key, value in env_overrides.items()})
        for key, value in merged.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

        from backend.main import app

        with TestClient(app) as client:
            yield client

        get_settings.cache_clear()

    return factory


@pytest.fixture
def client(api_client):
    """Plain ``TestClient`` with the default offline test settings."""
    with api_client() as test_client:
        yield test_client
