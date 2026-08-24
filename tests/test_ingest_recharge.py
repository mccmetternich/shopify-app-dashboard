"""Tests for the Recharge subscription ingest layer.

All HTTP calls are mocked — no real API calls.
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app_dashboard.recharge import RechargeClient
from app_dashboard.ingest_recharge import sync_subscription_revenue, _SYNC_SOURCE


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _recharge_charge(
    charge_id: str = "rc_001",
    customer_id: str = "cust_001",
    subscription_id: str = "sub_001",
    total_price: str = "99.00",
    currency: str = "USD",
    scheduled_at: str = "2026-08-01T10:00:00+00:00",
    status: str = "SUCCESS",
    is_test: bool = False,
    ext_tx_id: str = "ch_3Ptest",
) -> dict:
    return {
        "id": charge_id,
        "customer_id": customer_id,
        "subscription_id": subscription_id,
        "total_price": total_price,
        "currency": currency,
        "scheduled_at": scheduled_at,
        "status": status,
        "test": is_test,
        "external_transaction_id": ext_tx_id,
    }


def _recharge_response(charges: list[dict], next_cursor: str | None = None) -> dict:
    return {
        "charges": charges,
        "next_cursor": next_cursor,
    }


# ── Unit tests — RechargeClient ───────────────────────────────────────────────

def test_client_raises_on_empty_token():
    with pytest.raises(ValueError, match="api_token"):
        RechargeClient(api_token="")


def test_fetch_charges_returns_real_charges():
    body = _recharge_response([_recharge_charge()])
    mock_resp = MagicMock()
    mock_resp.json.return_value = body
    mock_resp.raise_for_status.return_value = None

    client = RechargeClient(api_token="token")
    with patch.object(client._client, "get", return_value=mock_resp):
        charges, next_cursor = client.fetch_charges(
            updated_at_min=datetime(2026, 8, 1, tzinfo=timezone.utc)
        )

    assert len(charges) == 1
    assert charges[0]["id"] == "rc_001"
    assert next_cursor is None


def test_fetch_charges_filters_test_charges():
    """test=True charges must be silently filtered out (C1 trap)."""
    body = _recharge_response([
        _recharge_charge("real_001", is_test=False),
        _recharge_charge("test_001", is_test=True),
    ])
    mock_resp = MagicMock()
    mock_resp.json.return_value = body
    mock_resp.raise_for_status.return_value = None

    client = RechargeClient(api_token="token")
    with patch.object(client._client, "get", return_value=mock_resp):
        charges, _ = client.fetch_charges(
            updated_at_min=datetime(2026, 8, 1, tzinfo=timezone.utc)
        )

    assert len(charges) == 1
    assert charges[0]["id"] == "real_001"


def test_fetch_charges_non_usd_raises():
    """Non-USD charges must raise AssertionError — never silently convert."""
    body = _recharge_response([
        _recharge_charge(currency="GBP"),
    ])
    mock_resp = MagicMock()
    mock_resp.json.return_value = body
    mock_resp.raise_for_status.return_value = None

    client = RechargeClient(api_token="token")
    with patch.object(client._client, "get", return_value=mock_resp):
        with pytest.raises(AssertionError, match="USD"):
            client.fetch_charges(
                updated_at_min=datetime(2026, 8, 1, tzinfo=timezone.utc)
            )


def test_fetch_charges_total_price_is_decimal():
    body = _recharge_response([_recharge_charge(total_price="149.00")])
    mock_resp = MagicMock()
    mock_resp.json.return_value = body
    mock_resp.raise_for_status.return_value = None

    client = RechargeClient(api_token="token")
    with patch.object(client._client, "get", return_value=mock_resp):
        charges, _ = client.fetch_charges(
            updated_at_min=datetime(2026, 8, 1, tzinfo=timezone.utc)
        )

    assert type(charges[0]["total_price"]) is Decimal
    assert charges[0]["total_price"] == Decimal("149.00")


def test_fetch_charges_pagination():
    body = _recharge_response([_recharge_charge()], next_cursor="next_abc")
    mock_resp = MagicMock()
    mock_resp.json.return_value = body
    mock_resp.raise_for_status.return_value = None

    client = RechargeClient(api_token="token")
    with patch.object(client._client, "get", return_value=mock_resp):
        charges, next_cursor = client.fetch_charges(
            updated_at_min=datetime(2026, 8, 1, tzinfo=timezone.utc)
        )

    assert next_cursor == "next_abc"


# ── Integration tests — sync_subscription_revenue against real DB ────────────

def test_sync_subscription_revenue_inserts_rows(db):
    charges = [
        _recharge_charge("rc_001", "cust_001", "sub_001", "99.00"),
        _recharge_charge("rc_002", "cust_002", "sub_002", "149.00",
                         customer_id="cust_002"),
    ]
    body = _recharge_response(charges)
    mock_resp = MagicMock()
    mock_resp.json.return_value = body
    mock_resp.raise_for_status.return_value = None

    client = RechargeClient(api_token="token")
    with patch.object(client._client, "get", return_value=mock_resp):
        n = sync_subscription_revenue(db, client, state={})

    sub_count = db.execute(
        "select count(*) from subscription_revenue"
    ).fetchone()[0]
    assert sub_count == 2


def test_sync_subscription_revenue_creates_stub_customers(db):
    """If customer is not in customers table, a stub row is inserted."""
    charges = [_recharge_charge("rc_001", "cust_recharge_only", "sub_001")]
    body = _recharge_response(charges)
    mock_resp = MagicMock()
    mock_resp.json.return_value = body
    mock_resp.raise_for_status.return_value = None

    client = RechargeClient(api_token="token")
    with patch.object(client._client, "get", return_value=mock_resp):
        sync_subscription_revenue(db, client, state={})

    # Customer stub must exist for FK to pass
    row = db.execute(
        "select id from customers where id = 'cust_recharge_only'"
    ).fetchone()
    assert row is not None


def test_sync_subscription_revenue_converted_at_first_seen(db):
    """converted_at must not be updated once set (first-seen wins)."""
    charge_v1 = _recharge_charge(
        "rc_001", "cust_001", "sub_001",
        scheduled_at="2026-07-01T10:00:00+00:00"
    )
    charge_v2 = _recharge_charge(
        "rc_002", "cust_001", "sub_001",
        scheduled_at="2026-08-01T10:00:00+00:00"
    )

    client = RechargeClient(api_token="token")

    mock_resp1 = MagicMock()
    mock_resp1.json.return_value = _recharge_response([charge_v1])
    mock_resp1.raise_for_status.return_value = None
    with patch.object(client._client, "get", return_value=mock_resp1):
        sync_subscription_revenue(db, client, state={})

    mock_resp2 = MagicMock()
    mock_resp2.json.return_value = _recharge_response([charge_v2])
    mock_resp2.raise_for_status.return_value = None
    with patch.object(client._client, "get", return_value=mock_resp2):
        sync_subscription_revenue(db, client, state={})

    row = db.execute(
        "select converted_at from subscription_revenue where id = 'sub_001'"
    ).fetchone()
    # converted_at should remain the first date (July, not August)
    assert row[0].month == 7


def test_sync_subscription_revenue_no_orphan_customers(db):
    """Every subscription_revenue.customer_id must exist in customers."""
    charges = [_recharge_charge("rc_001", "new_cust_abc", "sub_abc")]
    body = _recharge_response(charges)
    mock_resp = MagicMock()
    mock_resp.json.return_value = body
    mock_resp.raise_for_status.return_value = None

    client = RechargeClient(api_token="token")
    with patch.object(client._client, "get", return_value=mock_resp):
        sync_subscription_revenue(db, client, state={})

    count = db.execute(
        """select count(*) from subscription_revenue sr
           join customers c on c.id = sr.customer_id"""
    ).fetchone()[0]
    assert count == 1


def test_sync_subscription_revenue_updates_sync_state(db):
    body = _recharge_response([_recharge_charge()])
    mock_resp = MagicMock()
    mock_resp.json.return_value = body
    mock_resp.raise_for_status.return_value = None

    client = RechargeClient(api_token="token")
    with patch.object(client._client, "get", return_value=mock_resp):
        sync_subscription_revenue(db, client, state={})

    row = db.execute(
        "select last_synced_at from sync_state where source = %s",
        (_SYNC_SOURCE,),
    ).fetchone()
    assert row is not None and row[0] is not None


def test_sync_subscription_revenue_idempotent(db):
    """Running twice with the same charge must produce one subscription row."""
    charges = [_recharge_charge("rc_001", "cust_001", "sub_001")]
    body = _recharge_response(charges)

    client = RechargeClient(api_token="token")

    for _ in range(2):
        mock_resp = MagicMock()
        mock_resp.json.return_value = body
        mock_resp.raise_for_status.return_value = None
        with patch.object(client._client, "get", return_value=mock_resp):
            sync_subscription_revenue(db, client, state={})

    count = db.execute(
        "select count(*) from subscription_revenue"
    ).fetchone()[0]
    assert count == 1
