"""The JSON export is the one artifact that leaves this app as a file.

Phase A: tests updated to match the reduced export structure.
"""

import json
from datetime import datetime, timezone

from app_dashboard import export
from app_dashboard.config import get_settings


def _payload(db):
    return export.full_export(db, get_settings())


def test_every_section_is_present(db):
    """A missing key reads as zero to whatever consumes this, so the shape is
    fixed even on an empty database."""
    payload = _payload(db)
    assert set(payload) == {
        "meta", "definitions", "sync_health", "annotations",
        "overview", "cohorts", "survey", "faq",
    }


def test_it_serialises_without_a_custom_encoder_at_the_call_site(db):
    """Decimals and dates come straight out of psycopg. If render() did not
    handle them the route would 500."""
    text = export.render(db, get_settings())
    assert json.loads(text)["meta"]["source"]


def test_definitions_travel_with_the_numbers(db):
    """A file opened a year from now has no scoreboard beside it, so it has to
    say what each number counted."""
    from app_dashboard.metrics import METRICS
    payload = _payload(db)
    assert set(payload["definitions"]) == set(METRICS)
    assert payload["definitions"]["revenue"]["rule"] == METRICS["revenue"].rule


def test_the_limits_it_used_are_written_into_the_file(db):
    """Every section has a cap somewhere. A reader has to be able to tell a real
    end from a ceiling, so the ceilings are stated rather than implied."""
    assert _payload(db)["meta"]["windows"] == export.LIMITS


def test_generated_at_is_the_time_it_was_asked_for(db):
    stamped = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    payload = export.full_export(db, get_settings(), now=stamped)
    assert payload["meta"]["generated_at"].startswith("2026-03-01T12:00:00")
    assert export.filename(stamped, slug="densologie") == "densologie-2026-03-01.json"


def test_export_filename_slug_comes_from_the_app_name(monkeypatch):
    monkeypatch.setenv("APP_NAME", "Densologie")
    assert get_settings().slug == "densologie"
    monkeypatch.setenv("APP_SLUG", "den")
    get_settings.cache_clear()
    assert get_settings().slug == "den"
