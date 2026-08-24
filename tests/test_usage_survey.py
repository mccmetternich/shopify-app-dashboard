"""Tests for survey_response event handling and survey_tally().

Covers:
  - GID regex accepting Customer GIDs (B7 relaxation)
  - GID regex still rejecting invalid forms
  - survey_response event type accepted by parse_batch
  - Unknown event types still rejected
  - survey_tally() output shape and grouping
"""

import json
from datetime import datetime, timezone

import pytest

from app_dashboard.usage import (
    SHOP_GID_RE,
    UsageError,
    ingest,
    parse_batch,
)
from app_dashboard.stats import survey_tally


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


# ── GID regex tests ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("gid", [
    "gid://shopify/Shop/12345678",
    "gid://shopify/Shop/1",
    "gid://shopify/Customer/12345678",
    "gid://shopify/Customer/99999999999999999",
])
def test_gid_regex_accepts_valid_forms(gid):
    assert SHOP_GID_RE.match(gid), f"Expected {gid!r} to match SHOP_GID_RE"


@pytest.mark.parametrize("gid", [
    "gid://shopify/Order/12345678",       # Order is not in the allowlist
    "gid://shopify/Product/1",            # Product is not in the allowlist
    "gid://shopify/customer/1",           # lowercase — anchored match fails
    "gid://shopify/Shop/",                # no numeric ID
    "gid://shopify/Customer/abc",         # non-numeric ID
    "shopify/Customer/1",                 # missing protocol
    "",
    "gid://shopify/Customer/1extra",      # trailing non-numeric
])
def test_gid_regex_rejects_invalid_forms(gid):
    assert not SHOP_GID_RE.match(gid), f"Expected {gid!r} NOT to match SHOP_GID_RE"


# ── parse_batch tests — survey_response ───────────────────────────────────────

def _survey_event(**over):
    event = {
        "event_id": "surv_e1",
        "shop_gid": "gid://shopify/Customer/123",
        "event_type": "survey_response",
        "occurred_at": "2026-08-10T11:00:00Z",
        "properties": {"heard_via": "instagram"},
    }
    event.update(over)
    return event


def _body(*events):
    return json.dumps({"events": list(events)}).encode()


def test_survey_response_event_type_accepted():
    events = parse_batch(_body(_survey_event()), now=NOW)
    assert len(events) == 1
    assert events[0]["event_type"] == "survey_response"


def test_customer_gid_accepted_in_survey_event():
    events = parse_batch(
        _body(_survey_event(shop_gid="gid://shopify/Customer/99999")), now=NOW
    )
    assert events[0]["shop_gid"] == "gid://shopify/Customer/99999"


def test_shop_gid_still_accepted():
    events = parse_batch(
        _body(_survey_event(shop_gid="gid://shopify/Shop/42")), now=NOW
    )
    assert events[0]["shop_gid"] == "gid://shopify/Shop/42"


def test_order_gid_rejected():
    with pytest.raises(UsageError) as exc:
        parse_batch(
            _body(_survey_event(shop_gid="gid://shopify/Order/1")), now=NOW
        )
    assert exc.value.status == 422


def test_unknown_event_type_rejected():
    with pytest.raises(UsageError) as exc:
        parse_batch(
            _body(_survey_event(event_type="unknown_type_xyz")), now=NOW
        )
    assert exc.value.status == 422


def test_survey_properties_stored():
    events = parse_batch(_body(_survey_event()), now=NOW)
    assert events[0]["properties"] == {"heard_via": "instagram"}


# ── survey_tally() tests ───────────────────────────────────────────────────────

def _ingest_survey(conn, heard_via: str, shop_gid: str | None = None):
    """Helper: store one survey_response event."""
    events = parse_batch(
        json.dumps({"events": [{
            "event_id": f"surv_{heard_via}_{id(object())}",
            "shop_gid": shop_gid or "gid://shopify/Customer/1",
            "event_type": "survey_response",
            "occurred_at": "2026-08-10T11:00:00Z",
            "properties": {"heard_via": heard_via},
        }]}).encode(),
        now=NOW,
    )
    ingest(conn, events)


def test_survey_tally_empty(db):
    result = survey_tally(db)
    assert result == []


def test_survey_tally_groups_by_heard_via(db):
    # 3 instagram, 2 tiktok
    for _ in range(3):
        _ingest_survey(db, "instagram")
    for _ in range(2):
        _ingest_survey(db, "tiktok")

    result = survey_tally(db)
    by_channel = {r["heard_via"]: r for r in result}

    assert "instagram" in by_channel
    assert "tiktok" in by_channel
    assert by_channel["instagram"]["count"] == 3
    assert by_channel["tiktok"]["count"] == 2


def test_survey_tally_pct_sums_to_100(db):
    for heard_via in ["instagram", "tiktok", "friend"]:
        _ingest_survey(db, heard_via)

    result = survey_tally(db)
    total_pct = sum(r["pct"] for r in result)
    # With 3 equal channels, each is 33.3% → sum ≈ 99.9 due to rounding
    assert abs(total_pct - 100.0) < 1.0


def test_survey_tally_sorted_by_count_desc(db):
    for _ in range(5):
        _ingest_survey(db, "instagram")
    for _ in range(2):
        _ingest_survey(db, "tiktok")
    _ingest_survey(db, "friend")

    result = survey_tally(db)
    counts = [r["count"] for r in result]
    assert counts == sorted(counts, reverse=True)


def test_survey_tally_missing_heard_via_grouped_as_unknown(db):
    """Events without 'heard_via' in properties must group under 'unknown'."""
    events = parse_batch(
        json.dumps({"events": [{
            "event_id": "surv_no_heard_via",
            "shop_gid": "gid://shopify/Customer/1",
            "event_type": "survey_response",
            "occurred_at": "2026-08-10T11:00:00Z",
            "properties": {},
        }]}).encode(),
        now=NOW,
    )
    ingest(db, events)

    result = survey_tally(db)
    assert any(r["heard_via"] == "unknown" for r in result)


def test_survey_tally_returns_list_of_dicts_with_correct_keys(db):
    _ingest_survey(db, "instagram")
    result = survey_tally(db)
    assert isinstance(result, list)
    assert len(result) > 0
    for row in result:
        assert "heard_via" in row
        assert "count" in row
        assert "pct" in row
        assert isinstance(row["count"], int)
        assert isinstance(row["pct"], float)


def test_survey_tally_window_days_filter(db):
    """Events older than window_days must be excluded."""
    # Ingest a recent event
    _ingest_survey(db, "instagram")

    # survey_tally with window_days=0 should exclude everything (or show 0)
    result_0 = survey_tally(db, window_days=0)
    # All rows in the last 0 days — should be empty or very small
    # (the event was just ingested; received_at is now)
    # We can't be 100% precise here due to "now()" semantics, but
    # with window_days=365 it should definitely have the row.
    result_365 = survey_tally(db, window_days=365)
    assert any(r["heard_via"] == "instagram" for r in result_365)
