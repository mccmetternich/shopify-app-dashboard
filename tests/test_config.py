"""Phase A: config tests updated for Densologie Scoreboard settings."""

import pytest
from pydantic import ValidationError

from app_dashboard.config import Settings, get_settings


def _settings(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    return get_settings()


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("DASHBOARD_USERS", "ada:pw,grace:pw2")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("GOOGLE_ALLOWED_DOMAINS", "example.com")
    get_settings.cache_clear()
    s = Settings()
    assert s.database_url == "postgresql://x"
    assert s.dashboard_users_map == {"ada": "pw", "grace": "pw2"}


def test_app_name_default_is_densologie():
    """The scoreboard defaults to Densologie, not the old Shopify placeholder."""
    s = get_settings()
    assert s.app_name == "Densologie"
    assert s.app_slug == "densologie"


def test_dashboard_name_says_scoreboard_not_analytics():
    s = get_settings()
    assert "Scoreboard" in s.dashboard_name
    assert "Analytics" not in s.dashboard_name


def test_a_password_containing_a_comma_is_refused(monkeypatch):
    """"admin:pa,ssword" parsed as {"admin": "pa"} and logging in with "pa"
    worked. An operator who generated a random password got a 2-character one."""
    with pytest.raises(ValidationError):
        _settings(monkeypatch, DASHBOARD_USERS="admin:pa,ssword")


def test_the_activation_event_must_be_one_the_endpoint_accepts(monkeypatch):
    """Otherwise ingest 422s every event of that name."""
    with pytest.raises(ValidationError):
        _settings(monkeypatch, USAGE_EVENT_TYPES="a,b",
                  USAGE_ACTIVATION_EVENT="not_in_list", USAGE_LIVE_EVENT="a")
    with pytest.raises(ValidationError):
        _settings(monkeypatch, USAGE_EVENT_TYPES="a,b",
                  USAGE_ACTIVATION_EVENT="a", USAGE_LIVE_EVENT="not_in_list")


def test_slug_falls_back_to_app_name(monkeypatch):
    monkeypatch.setenv("APP_SLUG", "")  # empty triggers fallback; default is non-empty
    monkeypatch.setenv("APP_NAME", "My Brand")
    get_settings.cache_clear()
    s = get_settings()
    assert s.slug == "my-brand"
