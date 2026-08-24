"""Tests for the Meta Insights ingest layer.

All HTTP calls are mocked — no real API calls.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import httpx

from app_dashboard.meta_insights import MetaInsightsClient
from app_dashboard.ingest_meta import sync_ad_spend, _SYNC_SOURCE


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _meta_row(
    campaign_id: str = "camp_001",
    campaign_name: str = "Meta – Prospecting",
    spend: str = "123.45",
    date_start: str = "2026-08-01",
    date_stop: str = "2026-08-01",
) -> dict:
    return {
        "campaign_id": campaign_id,
        "campaign_name": campaign_name,
        "spend": spend,
        "date_start": date_start,
        "date_stop": date_stop,
    }


def _meta_response(rows: list[dict], next_url: str | None = None) -> dict:
    paging: dict = {}
    if next_url:
        paging["next"] = next_url
    return {"data": rows, "paging": paging}


# ── Unit tests — MetaInsightsClient ──────────────────────────────────────────

def test_client_raises_on_empty_account_id():
    with pytest.raises(ValueError, match="account_id"):
        MetaInsightsClient(account_id="", access_token="token")


def test_client_raises_on_empty_token():
    with pytest.raises(ValueError, match="access_token"):
        MetaInsightsClient(account_id="12345", access_token="")


def test_fetch_daily_spend_returns_rows():
    body = _meta_response([_meta_row()])
    mock_resp = MagicMock()
    mock_resp.json.return_value = body
    mock_resp.raise_for_status.return_value = None

    client = MetaInsightsClient(account_id="12345", access_token="token")
    with patch.object(client._client, "get", return_value=mock_resp):
        rows = client.fetch_daily_spend(
            date_start=date(2026, 8, 1),
            date_end=date(2026, 8, 7),
        )

    assert len(rows) == 1
    assert rows[0]["campaign_id"] == "camp_001"
    assert rows[0]["spend"] == Decimal("123.45")
    assert isinstance(rows[0]["date"], date)


def test_fetch_daily_spend_normalises_spend_to_decimal():
    body = _meta_response([_meta_row(spend="0.50")])
    mock_resp = MagicMock()
    mock_resp.json.return_value = body
    mock_resp.raise_for_status.return_value = None

    client = MetaInsightsClient(account_id="12345", access_token="token")
    with patch.object(client._client, "get", return_value=mock_resp):
        rows = client.fetch_daily_spend(date(2026, 8, 1), date(2026, 8, 1))

    assert type(rows[0]["spend"]) is Decimal


def test_fetch_daily_spend_raises_on_http_error():
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "401", request=MagicMock(), response=MagicMock(status_code=401, text="Unauthorized", json=MagicMock(return_value={"error": {"message": "Token expired"}}))
    )
    # Make json() on response return the error dict
    err_response = MagicMock()
    err_response.status_code = 401
    err_response.text = "Unauthorized"
    err_response.json.return_value = {"error": {"message": "Token expired"}}
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "401", request=MagicMock(), response=err_response
    )

    client = MetaInsightsClient(account_id="12345", access_token="bad_token")
    with patch.object(client._client, "get", return_value=mock_resp):
        with pytest.raises(RuntimeError, match="401"):
            client.fetch_daily_spend(date(2026, 8, 1), date(2026, 8, 7))


def test_fetch_daily_spend_raises_on_meta_api_error():
    """Meta returns error JSON even on 200 when the token is expired."""
    body = {"error": {"code": 190, "message": "Invalid OAuth access token."}}
    mock_resp = MagicMock()
    mock_resp.json.return_value = body
    mock_resp.raise_for_status.return_value = None

    client = MetaInsightsClient(account_id="12345", access_token="bad_token")
    with patch.object(client._client, "get", return_value=mock_resp):
        with pytest.raises(RuntimeError, match="190"):
            client.fetch_daily_spend(date(2026, 8, 1), date(2026, 8, 7))


def test_fetch_daily_spend_empty_response():
    body = _meta_response([])
    mock_resp = MagicMock()
    mock_resp.json.return_value = body
    mock_resp.raise_for_status.return_value = None

    client = MetaInsightsClient(account_id="12345", access_token="token")
    with patch.object(client._client, "get", return_value=mock_resp):
        rows = client.fetch_daily_spend(date(2026, 8, 1), date(2026, 8, 7))

    assert rows == []


# ── Integration tests — sync_ad_spend against real DB ────────────────────────

def test_sync_ad_spend_inserts_rows(db):
    rows = [
        _meta_row("camp_001", "Prospecting", "100.00", "2026-08-01", "2026-08-01"),
        _meta_row("camp_002", "Retargeting", "80.00", "2026-08-01", "2026-08-01"),
    ]
    body = _meta_response(rows)
    mock_resp = MagicMock()
    mock_resp.json.return_value = body
    mock_resp.raise_for_status.return_value = None

    client = MetaInsightsClient(account_id="12345", access_token="token")
    with patch.object(client._client, "get", return_value=mock_resp):
        n = sync_ad_spend(db, client, lookback_days=7)

    assert n == 2
    count = db.execute("select count(*) from ad_spend").fetchone()[0]
    assert count == 2


def test_sync_ad_spend_upserts_spend(db):
    """ON CONFLICT should update spend to the latest value."""
    rows_v1 = [_meta_row("camp_001", "Prospecting", "100.00", "2026-08-01", "2026-08-01")]
    rows_v2 = [_meta_row("camp_001", "Prospecting", "115.00", "2026-08-01", "2026-08-01")]

    client = MetaInsightsClient(account_id="12345", access_token="token")

    mock_resp1 = MagicMock()
    mock_resp1.json.return_value = _meta_response(rows_v1)
    mock_resp1.raise_for_status.return_value = None
    with patch.object(client._client, "get", return_value=mock_resp1):
        sync_ad_spend(db, client, lookback_days=1)

    mock_resp2 = MagicMock()
    mock_resp2.json.return_value = _meta_response(rows_v2)
    mock_resp2.raise_for_status.return_value = None
    with patch.object(client._client, "get", return_value=mock_resp2):
        sync_ad_spend(db, client, lookback_days=1)

    row = db.execute(
        "select spend from ad_spend where campaign_id = 'camp_001'"
    ).fetchone()
    assert Decimal(str(row[0])) == Decimal("115.00")


def test_sync_ad_spend_platform_is_meta(db):
    """platform column must be 'meta' for all rows from MetaInsightsClient."""
    rows = [_meta_row()]
    mock_resp = MagicMock()
    mock_resp.json.return_value = _meta_response(rows)
    mock_resp.raise_for_status.return_value = None

    client = MetaInsightsClient(account_id="12345", access_token="token")
    with patch.object(client._client, "get", return_value=mock_resp):
        sync_ad_spend(db, client, lookback_days=1)

    platform = db.execute("select platform from ad_spend limit 1").fetchone()[0]
    assert platform == "meta"


def test_sync_ad_spend_updates_sync_state(db):
    mock_resp = MagicMock()
    mock_resp.json.return_value = _meta_response([])
    mock_resp.raise_for_status.return_value = None

    client = MetaInsightsClient(account_id="12345", access_token="token")
    with patch.object(client._client, "get", return_value=mock_resp):
        sync_ad_spend(db, client, lookback_days=1)

    row = db.execute(
        "select last_synced_at from sync_state where source = %s",
        (_SYNC_SOURCE,),
    ).fetchone()
    assert row is not None and row[0] is not None


def test_sync_ad_spend_no_duplicate_rows(db):
    """Two rows with the same (date, campaign_id) must produce one DB row."""
    rows = [
        _meta_row("camp_001", "Prospecting", "100.00", "2026-08-01", "2026-08-01"),
        _meta_row("camp_001", "Prospecting", "110.00", "2026-08-01", "2026-08-01"),
    ]
    mock_resp = MagicMock()
    mock_resp.json.return_value = _meta_response(rows)
    mock_resp.raise_for_status.return_value = None

    client = MetaInsightsClient(account_id="12345", access_token="token")
    with patch.object(client._client, "get", return_value=mock_resp):
        sync_ad_spend(db, client, lookback_days=1)

    count = db.execute("select count(*) from ad_spend").fetchone()[0]
    assert count == 1
