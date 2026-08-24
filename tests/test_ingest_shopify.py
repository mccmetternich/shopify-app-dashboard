"""Tests for the Shopify ingest layer.

All HTTP calls are mocked — no real API calls. Fixtures provide canned
GraphQL responses that match the shape returned by the Shopify Admin API.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app_dashboard.shopify_admin import (
    ShopifyAdminClient,
    _extract_utm,
    _normalize_order,
    _parse_utc,
)
from app_dashboard.ingest_shopify import sync_orders, _SYNC_SOURCE


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _shopify_order_node(
    order_id: str = "gid://shopify/Order/1",
    customer_id: str = "gid://shopify/Customer/100",
    n_orders: int = 1,
    total: str = "149.00",
    currency: str = "USD",
    refunded: str = "0.00",
    created_at: str = "2026-08-01T10:00:00Z",
    custom_attributes: list | None = None,
) -> dict:
    return {
        "id": order_id,
        "createdAt": created_at,
        "totalPriceSet": {"shopMoney": {"amount": total, "currencyCode": currency}},
        "customer": {"id": customer_id, "numberOfOrders": n_orders},
        "lineItems": {
            "nodes": [
                {
                    "sku": "HAIR-SERUM-50ML",
                    "title": "TRICHOGENESIS Hair Serum",
                    "quantity": 1,
                    "originalUnitPriceSet": {"shopMoney": {"amount": "149.00"}},
                }
            ]
        },
        "customAttributes": custom_attributes or [],
        "landingSite": "/products/hair-serum",
        "referringSite": "https://instagram.com",
        "totalRefundedSet": {"shopMoney": {"amount": refunded}},
    }


def _gql_page(nodes: list[dict], has_next: bool = False, end_cursor: str | None = None) -> dict:
    return {
        "orders": {
            "edges": [
                {"cursor": f"cursor_{i}", "node": node}
                for i, node in enumerate(nodes)
            ],
            "pageInfo": {
                "hasNextPage": has_next,
                "endCursor": end_cursor,
            },
        }
    }


# ── Unit tests — shopify_admin helpers ────────────────────────────────────────

def test_parse_utc_accepts_z_suffix():
    dt = _parse_utc("2026-08-01T10:00:00Z")
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0
    assert dt.year == 2026


def test_parse_utc_rejects_naive():
    with pytest.raises(ValueError, match="UTC"):
        _parse_utc("2026-08-01T10:00:00")  # no timezone


def test_extract_utm_returns_none_for_empty():
    assert _extract_utm([]) is None


def test_extract_utm_extracts_keys():
    attrs = [
        {"key": "utm_source", "value": "meta"},
        {"key": "utm_medium", "value": "paid_social"},
        {"key": "utm_campaign", "value": "prospe_01"},
        {"key": "other_key", "value": "ignored"},
    ]
    utm = _extract_utm(attrs)
    assert utm == {
        "utm_source": "meta",
        "utm_medium": "paid_social",
        "utm_campaign": "prospe_01",
    }


def test_extract_utm_returns_none_when_no_utm_keys():
    attrs = [{"key": "discount_code", "value": "SAVE10"}]
    assert _extract_utm(attrs) is None


def test_normalize_order_new_customer():
    node = _shopify_order_node(n_orders=1)
    order = _normalize_order(node)
    assert order["is_new_customer"] is True


def test_normalize_order_returning_customer():
    node = _shopify_order_node(n_orders=3)
    order = _normalize_order(node)
    assert order["is_new_customer"] is False


def test_normalize_order_refunded():
    node = _shopify_order_node(total="149.00", refunded="149.00")
    order = _normalize_order(node)
    assert order["refunded"] == Decimal("149.00")


def test_normalize_order_line_items():
    node = _shopify_order_node()
    order = _normalize_order(node)
    assert len(order["line_items"]) == 1
    assert order["line_items"][0]["sku"] == "HAIR-SERUM-50ML"


def test_normalize_order_utm():
    node = _shopify_order_node(custom_attributes=[
        {"key": "utm_source", "value": "meta"},
        {"key": "utm_campaign", "value": "prospe_01"},
    ])
    order = _normalize_order(node)
    assert order["source_utm"] == {"utm_source": "meta", "utm_campaign": "prospe_01"}


def test_normalize_order_no_utm():
    node = _shopify_order_node(custom_attributes=[])
    order = _normalize_order(node)
    assert order["source_utm"] is None  # never {} — spec requirement


# ── ShopifyAdminClient unit tests (mocked HTTP) ───────────────────────────────

def test_client_raises_on_empty_domain():
    with pytest.raises(ValueError, match="shop_domain"):
        ShopifyAdminClient(shop_domain="", access_token="tok")


def test_client_raises_on_empty_token():
    with pytest.raises(ValueError, match="access_token"):
        ShopifyAdminClient(shop_domain="example.myshopify.com", access_token="")


def test_fetch_orders_returns_normalized_orders():
    page = _gql_page([_shopify_order_node()], has_next=False)
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": page}
    mock_resp.raise_for_status.return_value = None

    client = ShopifyAdminClient(
        shop_domain="densologie.myshopify.com",
        access_token="token",
        throttle_seconds=0,
    )
    with patch.object(client._client, "post", return_value=mock_resp):
        orders, next_cursor = client.fetch_orders(
            created_at_min=datetime(2026, 8, 1, tzinfo=timezone.utc)
        )

    assert len(orders) == 1
    assert orders[0]["id"] == "gid://shopify/Order/1"
    assert next_cursor is None


def test_fetch_orders_pagination():
    page1 = _gql_page(
        [_shopify_order_node("gid://shopify/Order/1")],
        has_next=True,
        end_cursor="cur_abc",
    )
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": page1}
    mock_resp.raise_for_status.return_value = None

    client = ShopifyAdminClient(
        shop_domain="densologie.myshopify.com",
        access_token="token",
        throttle_seconds=0,
    )
    with patch.object(client._client, "post", return_value=mock_resp):
        orders, next_cursor = client.fetch_orders(
            created_at_min=datetime(2026, 8, 1, tzinfo=timezone.utc)
        )

    assert next_cursor == "cur_abc"


def test_fetch_refunds_returns_decimal():
    refund_data = {
        "order": {
            "id": "gid://shopify/Order/1",
            "totalRefundedSet": {"shopMoney": {"amount": "49.00"}},
        }
    }
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": refund_data}
    mock_resp.raise_for_status.return_value = None

    client = ShopifyAdminClient(
        shop_domain="densologie.myshopify.com",
        access_token="token",
        throttle_seconds=0,
    )
    with patch.object(client._client, "post", return_value=mock_resp):
        refunds = client.fetch_refunds(["gid://shopify/Order/1"])

    assert refunds["gid://shopify/Order/1"] == Decimal("49.00")


def test_client_raises_on_graphql_errors():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"errors": [{"message": "Token expired"}]}
    mock_resp.raise_for_status.return_value = None

    client = ShopifyAdminClient(
        shop_domain="densologie.myshopify.com",
        access_token="token",
        throttle_seconds=0,
    )
    with patch.object(client._client, "post", return_value=mock_resp):
        with pytest.raises(RuntimeError, match="GraphQL errors"):
            client.fetch_orders(
                created_at_min=datetime(2026, 8, 1, tzinfo=timezone.utc)
            )


# ── Integration tests — sync_orders against real DB ──────────────────────────

def test_sync_orders_inserts_rows(db):
    """sync_orders should insert customer + order rows into the DB."""
    page = _gql_page([
        _shopify_order_node("gid://shopify/Order/1", n_orders=1),
        _shopify_order_node("gid://shopify/Order/2",
                            customer_id="gid://shopify/Customer/101",
                            n_orders=2),
    ], has_next=False)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": page}
    mock_resp.raise_for_status.return_value = None

    client = ShopifyAdminClient(
        shop_domain="densologie.myshopify.com",
        access_token="token",
        throttle_seconds=0,
    )
    with patch.object(client._client, "post", return_value=mock_resp):
        n = sync_orders(db, client, state={})

    assert n == 2
    order_count = db.execute("select count(*) from orders").fetchone()[0]
    assert order_count == 2
    customer_count = db.execute("select count(*) from customers").fetchone()[0]
    assert customer_count == 2


def test_sync_orders_is_new_customer_correct(db):
    """is_new_customer must reflect numberOfOrders at ingest time."""
    page = _gql_page([
        _shopify_order_node("gid://shopify/Order/1", n_orders=1),
        _shopify_order_node("gid://shopify/Order/2",
                            customer_id="gid://shopify/Customer/101",
                            n_orders=5),
    ], has_next=False)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": page}
    mock_resp.raise_for_status.return_value = None

    client = ShopifyAdminClient(
        shop_domain="densologie.myshopify.com",
        access_token="token",
        throttle_seconds=0,
    )
    with patch.object(client._client, "post", return_value=mock_resp):
        sync_orders(db, client, state={})

    rows = db.execute(
        "select id, is_new_customer from orders order by id"
    ).fetchall()
    by_id = {r[0]: r[1] for r in rows}
    assert by_id["gid://shopify/Order/1"] is True
    assert by_id["gid://shopify/Order/2"] is False


def test_sync_orders_refunds_stored(db):
    """refunded amount from totalRefundedSet must be stored."""
    page = _gql_page([
        _shopify_order_node("gid://shopify/Order/1", refunded="149.00"),
    ], has_next=False)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": page}
    mock_resp.raise_for_status.return_value = None

    client = ShopifyAdminClient(
        shop_domain="densologie.myshopify.com",
        access_token="token",
        throttle_seconds=0,
    )
    with patch.object(client._client, "post", return_value=mock_resp):
        sync_orders(db, client, state={})

    row = db.execute(
        "select refunded from orders where id = 'gid://shopify/Order/1'"
    ).fetchone()
    assert row is not None
    assert Decimal(str(row[0])) == Decimal("149.00")


def test_sync_orders_no_orphan_customers(db):
    """Every orders.customer_id must exist in customers (FK constraint)."""
    page = _gql_page([
        _shopify_order_node("gid://shopify/Order/1",
                            customer_id="gid://shopify/Customer/999"),
    ], has_next=False)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": page}
    mock_resp.raise_for_status.return_value = None

    client = ShopifyAdminClient(
        shop_domain="densologie.myshopify.com",
        access_token="token",
        throttle_seconds=0,
    )
    with patch.object(client._client, "post", return_value=mock_resp):
        sync_orders(db, client, state={})

    # If FK constraint is satisfied, this returns 1
    count = db.execute(
        """select count(*) from orders o
           join customers c on c.id = o.customer_id"""
    ).fetchone()[0]
    assert count == 1


def test_sync_orders_updates_sync_state(db):
    """sync_state must be updated after a successful sync."""
    page = _gql_page([_shopify_order_node()], has_next=False)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": page}
    mock_resp.raise_for_status.return_value = None

    client = ShopifyAdminClient(
        shop_domain="densologie.myshopify.com",
        access_token="token",
        throttle_seconds=0,
    )
    with patch.object(client._client, "post", return_value=mock_resp):
        sync_orders(db, client, state={})

    row = db.execute(
        "select last_synced_at from sync_state where source = %s",
        (_SYNC_SOURCE,),
    ).fetchone()
    assert row is not None
    assert row[0] is not None


def test_sync_orders_idempotent(db):
    """Running sync_orders twice with the same data must not duplicate rows."""
    page = _gql_page([_shopify_order_node()], has_next=False)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": page}
    mock_resp.raise_for_status.return_value = None

    client = ShopifyAdminClient(
        shop_domain="densologie.myshopify.com",
        access_token="token",
        throttle_seconds=0,
    )
    with patch.object(client._client, "post", return_value=mock_resp):
        sync_orders(db, client, state={})
    with patch.object(client._client, "post", return_value=mock_resp):
        sync_orders(db, client, state={})

    count = db.execute("select count(*) from orders").fetchone()[0]
    assert count == 1


def test_sync_orders_is_new_customer_not_updated_on_conflict(db):
    """is_new_customer must not be overwritten once set (C1 trap)."""
    # First sync: customer has 1 order → is_new_customer = True
    page1 = _gql_page([_shopify_order_node(n_orders=1)], has_next=False)
    mock_resp1 = MagicMock()
    mock_resp1.json.return_value = {"data": page1}
    mock_resp1.raise_for_status.return_value = None

    client = ShopifyAdminClient(
        shop_domain="densologie.myshopify.com",
        access_token="token",
        throttle_seconds=0,
    )
    with patch.object(client._client, "post", return_value=mock_resp1):
        sync_orders(db, client, state={})

    # Simulate a second sync where the same order now shows numberOfOrders = 3
    page2 = _gql_page([_shopify_order_node(n_orders=3)], has_next=False)
    mock_resp2 = MagicMock()
    mock_resp2.json.return_value = {"data": page2}
    mock_resp2.raise_for_status.return_value = None

    with patch.object(client._client, "post", return_value=mock_resp2):
        sync_orders(db, client, state={})

    # is_new_customer must still be True — set at first ingest, never overwritten
    row = db.execute(
        "select is_new_customer from orders where id = 'gid://shopify/Order/1'"
    ).fetchone()
    assert row[0] is True
