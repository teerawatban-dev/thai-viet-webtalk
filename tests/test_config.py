"""Unit tests for ``backend/config.py`` (env parsing, key handling)."""

from __future__ import annotations

import pytest

from backend.config import Settings, get_settings


def make_settings(**overrides) -> Settings:
    # ``_env_file=None`` keeps a developer's local ``.env`` out of the test.
    return Settings(_env_file=None, **overrides)


# ---------------------------------------------------------------------------
# targets parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("th,vi,en", ["th", "vi", "en"]),
        (" th , VI , en ", ["th", "vi", "en"]),
        ("th,th,vi,vi,en,en", ["th", "vi", "en"]),
        ("en,th", ["en", "th"]),  # order is preserved
        ("xx,th,bad,vi", ["th", "vi"]),  # unsupported codes dropped
        ("en", ["en"]),
        ("", ["th"]),  # fallback to default_src_lang
        ("xx,yy", ["th"]),
        (" , , ", ["th"]),
    ],
)
def test_targets_parsing(raw, expected):
    assert make_settings(target_langs=raw).targets == expected


def test_targets_fallback_uses_configured_default_language():
    settings = make_settings(target_langs="zz", default_src_lang="en")
    assert settings.targets == ["en"]


# ---------------------------------------------------------------------------
# key_for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "placeholder",
    [
        "",
        "   ",
        "changeme",
        "ChangeMe",
        "change-me",
        " your_key_here ",
        "xxx",
        "XXX",
    ],
)
def test_key_for_treats_placeholders_as_missing(placeholder):
    settings = make_settings(stt_api_key=placeholder, translate_api_key=placeholder)

    assert settings.key_for("soniox") == ""
    assert settings.key_for("deepgram") == ""
    assert settings.key_for("llm") == ""
    assert settings.key_for("google") == ""


def test_key_for_returns_trimmed_real_keys():
    settings = make_settings(
        stt_api_key="  stt-secret  ",
        translate_api_key="mt-secret",
    )

    assert settings.key_for("soniox") == "stt-secret"
    assert settings.key_for("deepgram") == "stt-secret"
    assert settings.key_for("llm") == "mt-secret"
    assert settings.key_for("google") == "mt-secret"
    # Unknown providers never get a key.
    assert settings.key_for("nope") == ""


# ---------------------------------------------------------------------------
# is_usable
# ---------------------------------------------------------------------------


def test_is_usable_for_offline_providers():
    settings = make_settings(stt_api_key="", translate_api_key="")

    assert settings.is_usable("mock") is True
    assert settings.is_usable("passthrough") is True


def test_is_usable_requires_a_real_key_for_network_providers():
    missing = make_settings(stt_api_key="", translate_api_key="changeme")
    assert missing.is_usable("soniox") is False
    assert missing.is_usable("deepgram") is False
    assert missing.is_usable("llm") is False
    assert missing.is_usable("google") is False

    configured = make_settings(stt_api_key="k1", translate_api_key="k2")
    assert configured.is_usable("soniox") is True
    assert configured.is_usable("deepgram") is True
    assert configured.is_usable("llm") is True
    assert configured.is_usable("google") is True


# ---------------------------------------------------------------------------
# get_settings caching
# ---------------------------------------------------------------------------


def test_get_settings_is_cached_until_cache_clear(monkeypatch):
    monkeypatch.setenv("MAX_ROOM_CLIENTS", "2")
    get_settings.cache_clear()
    assert get_settings().max_room_clients == 2

    # Still cached, so the changed env is not picked up yet.
    monkeypatch.setenv("MAX_ROOM_CLIENTS", "3")
    assert get_settings().max_room_clients == 2

    get_settings.cache_clear()
    assert get_settings().max_room_clients == 3
