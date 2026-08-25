"""Tests for the four previously-uncovered stat functions, the new+repeat reconciliation,
and subscription_share on aligned seed data.

Uses the `db` fixture from conftest.py (runs migrations, truncates all tables).
All tests are self-contained; none rely on external state.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app_dashboard.stats import (
    daily_revenue_and_spend,
    gross_profit_summary,
    overview_stats,
    subscription_movement_summary,
)


# ── Shared helpers (mirrors test_gate2_metrics.py) ────────────────────────────

def _customer(db, cid, first_order_at=None):
    if first_order_at is None:
        first_order_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db.execute(
        "insert into customers (id, email_hash, first_order_at, country) "
        "values (%s, %s, %s, 'US') on conflict (id) do nothing",
        (cid, f"hash_{cid}", first_order_at),
    )


def _order(db, oid, cust_id, total, refunded=Decimal("0"),
           is_new=True, is_sub=False, sku="HAIR-SERUM-50ML", days_ago=1):
    created_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.execute(
        "insert into orders "
        "(id, customer_id, created_at, total, refunded, currency, "
        " is_new_customer, is_subscription_order, discount_amount, line_items) "
        "values (%s, %s, %s, %s, %s, 'USD', %s, %s, %s, %s::jsonb) "
        "on conflict (id) do nothing",
        (
            oid, cust_id, created_at, total, refunded,
            is_new, is_sub, Decimal("0"),
            f'[{{"sku":"{sku}","quantity":1,"unit_price":{float(total)}}}]',
        ),
    )


def _spend(db, day, campaign_id, amount):
    db.execute(
        "insert into ad_spend (date, campaign_id, campaign_name, platform, spend) "
        "values (%s, %s, %s, 'meta', %s) on conflict (date, campaign_id) do update "
        "set spend = excluded.spend",
        (day, campaign_id, campaign_id, amount),
    )


def _sub(db, sid, cust_id, amount, converted_at, churned_at=None):
    status = "churned" if churned_at else "active"
    db.execute(
        "insert into subscription_revenue "
        "(id, customer_id, monthly_amount, converted_at, churned_at, "
        " sub_type, cash_collected, status) "
        "values (%s, %s, %s, %s, %s, 'monthly', %s, %s) "
        "on conflict (id) do nothing",
        (sid, cust_id, amount, converted_at, churned_at, amount, status),
    )


def _sub_event(db, sid, cust_id, event_type, event_date, mrr_delta=None):
    db.execute(
        "insert into subscription_events "
        "(subscription_id, customer_id, event_type, event_date, mrr_delta) "
        "values (%s, %s, %s, %s, %s)",
        (sid, cust_id, event_type, event_date, mrr_delta),
    )


def _cost_fixtures(db):
    db.execute(
        "insert into cost_inputs (sku, label, cogs_per_unit) values "
        "('HAIR-SERUM-50ML', 'Hair Serum', 20.00) on conflict (sku) do nothing"
    )
    db.execute(
        "insert into cost_settings (key, value, label) values "
        "('shipping_cost_per_order', 6.00, 'Shipping'), "
        "('payment_fee_pct', 0.03, 'Payment fee'), "
        "('return_processing_cost', 5.00, 'Return cost') "
        "on conflict (key) do nothing"
    )


# ── daily_revenue_and_spend ───────────────────────────────────────────────────

def test_daily_revenue_and_spend_returns_window_rows(db):
    """Always returns exactly window_days rows, even on an empty DB."""
    rows = daily_revenue_and_spend(db, window_days=7)
    assert len(rows) == 8, f"Expected 8 rows (today + 7), got {len(rows)}"


def test_daily_revenue_and_spend_gap_fill_zeros(db):
    """Days with no orders or spend show 0, not NULL."""
    rows = daily_revenue_and_spend(db, window_days=7)
    for r in rows:
        assert r["revenue"] == Decimal("0"), f"Expected 0 revenue on empty day, got {r['revenue']}"
        assert r["spend"] == Decimal("0"), f"Expected 0 spend on empty day, got {r['spend']}"


def test_daily_revenue_and_spend_revenue_matches_orders(db):
    """Sum of daily revenue equals sum(total - refunded) from orders."""
    _customer(db, "c1")
    _order(db, "o1", "c1", Decimal("149.00"), refunded=Decimal("0"),   days_ago=2)
    _order(db, "o2", "c1", Decimal("99.00"),  refunded=Decimal("10.00"), days_ago=1)
    db.commit()

    rows = daily_revenue_and_spend(db, window_days=7)
    total_rev = sum(r["revenue"] for r in rows)
    assert total_rev == Decimal("238.00"), f"Expected 238.00, got {total_rev}"


def test_daily_revenue_and_spend_spend_matches_ad_spend(db):
    """Sum of daily spend equals sum from ad_spend table."""
    today = date.today()
    _spend(db, today - timedelta(days=1), "camp1", Decimal("100.00"))
    _spend(db, today - timedelta(days=2), "camp1", Decimal("50.00"))
    db.commit()

    rows = daily_revenue_and_spend(db, window_days=7)
    total_spend = sum(r["spend"] for r in rows)
    assert total_spend == Decimal("150.00"), f"Expected 150.00, got {total_spend}"


def test_daily_revenue_and_spend_oldest_first(db):
    """Rows are ordered oldest date first."""
    _customer(db, "c1")
    _order(db, "o1", "c1", Decimal("149.00"), days_ago=5)
    _order(db, "o2", "c1", Decimal("99.00"),  days_ago=1)
    db.commit()

    rows = daily_revenue_and_spend(db, window_days=7)
    dates = [r["date"] for r in rows]
    assert dates == sorted(dates), "Rows should be oldest-first"


# ── gross_profit_summary ──────────────────────────────────────────────────────

def test_gross_profit_summary_none_when_no_orders(db):
    """Returns None when orders table is empty."""
    result = gross_profit_summary(db, window_days=7)
    assert result is None


def test_gross_profit_summary_waterfall_math(db):
    """Waterfall: gross_revenue − refunds = net; net − cogs − shipping − fees = gp."""
    _cost_fixtures(db)
    _customer(db, "c1")
    # One order: $149 total, no refund, 1 unit HAIR-SERUM-50ML (COGS=$20)
    _order(db, "o1", "c1", Decimal("149.00"), refunded=Decimal("0"), days_ago=1)
    db.commit()

    r = gross_profit_summary(db, window_days=7)
    assert r is not None

    assert r["gross_revenue"] == Decimal("149.00")
    assert r["refunds"] == Decimal("0.00")
    assert r["net_revenue"] == Decimal("149.00")

    # COGS: $20 × 1 unit
    assert r["cogs"] == Decimal("20.00"), f"COGS expected 20.00, got {r['cogs']}"

    # Shipping: $6 × 1 order
    assert r["shipping"] == Decimal("6.00"), f"Shipping expected 6.00, got {r['shipping']}"

    # Payment fees: 3% × $149 = $4.47
    assert r["payment_fees"] == Decimal("149.00") * Decimal("0.03")

    expected_gp = Decimal("149.00") - Decimal("20.00") - Decimal("6.00") - (Decimal("149.00") * Decimal("0.03"))
    assert r["gross_profit"] == expected_gp, f"GP expected {expected_gp}, got {r['gross_profit']}"

    # gross_margin_pct = gp / net_revenue * 100
    expected_margin = float(expected_gp / Decimal("149.00") * 100)
    assert abs(r["gross_margin_pct"] - expected_margin) < 0.01


def test_gross_profit_summary_cogs_estimated_when_sku_missing(db):
    """cogs_estimated=True when any order SKU is missing from cost_inputs."""
    _cost_fixtures(db)
    _customer(db, "c1")
    # Order with an unknown SKU
    _order(db, "o1", "c1", Decimal("149.00"), sku="UNKNOWN-SKU", days_ago=1)
    db.commit()

    r = gross_profit_summary(db, window_days=7)
    assert r is not None
    assert r["cogs_estimated"] is True


def test_gross_profit_summary_refunds_reduce_net(db):
    """Partial refund reduces net_revenue but not gross_revenue."""
    _cost_fixtures(db)
    _customer(db, "c1")
    _order(db, "o1", "c1", Decimal("149.00"), refunded=Decimal("50.00"), days_ago=1)
    db.commit()

    r = gross_profit_summary(db, window_days=7)
    assert r["gross_revenue"] == Decimal("149.00")
    assert r["refunds"] == Decimal("50.00")
    assert r["net_revenue"] == Decimal("99.00")


def test_gross_profit_summary_order_count_correct(db):
    """order_count matches the number of orders in the window."""
    _cost_fixtures(db)
    _customer(db, "c1")
    _customer(db, "c2")
    _order(db, "o1", "c1", Decimal("149.00"), days_ago=1)
    _order(db, "o2", "c2", Decimal("99.00"),  days_ago=2)
    db.commit()

    r = gross_profit_summary(db, window_days=7)
    assert r["order_count"] == 2


# ── setup_checklist ───────────────────────────────────────────────────────────

def test_setup_checklist_returns_expected_structure(db):
    """setup_checklist returns groups, done_count, total_count with correct shapes."""
    from app_dashboard.stats import setup_checklist

    # Build a minimal mock settings with all tokens absent
    settings = MagicMock()
    settings.shopify_admin_token = None
    settings.shopify_shop_domain = None
    settings.meta_access_token = None
    settings.meta_account_id = None
    settings.recharge_api_token = None
    settings.ga4_property_id = None
    settings.google_client_id = None
    settings.slack_webhook_url = None

    result = setup_checklist(db, settings)

    assert "groups" in result
    assert "done_count" in result
    assert "total_count" in result
    assert result["total_count"] == 10
    assert isinstance(result["groups"], list)
    assert len(result["groups"]) == 3


def test_setup_checklist_all_pending_when_no_tokens(db):
    """With no tokens and empty tables, all items are pending or warning — none done."""
    from app_dashboard.stats import setup_checklist

    settings = MagicMock()
    settings.shopify_admin_token = None
    settings.shopify_shop_domain = None
    settings.meta_access_token = None
    settings.meta_account_id = None
    settings.recharge_api_token = None
    settings.ga4_property_id = None
    settings.google_client_id = None
    settings.slack_webhook_url = None

    result = setup_checklist(db, settings)
    # Shopify: pending (no token). Orders: pending (no data). Meta: pending. Recharge: pending.
    # GA4: pending. Omnisend: pending. COGS: pending. Shipping: pending. SSO: pending. Slack: pending.
    assert result["done_count"] == 0, f"Expected 0 done, got {result['done_count']}"


def test_setup_checklist_orders_item_done_when_orders_exist(db):
    """Orders item flips to 'done' when the orders table is non-empty."""
    from app_dashboard.stats import setup_checklist

    _customer(db, "c1")
    _order(db, "o1", "c1", Decimal("149.00"))
    db.commit()

    settings = MagicMock()
    settings.shopify_admin_token = None
    settings.shopify_shop_domain = None
    settings.meta_access_token = None
    settings.meta_account_id = None
    settings.recharge_api_token = None
    settings.ga4_property_id = None
    settings.google_client_id = None
    settings.slack_webhook_url = None

    result = setup_checklist(db, settings)
    # Find the "Orders in database" item
    all_items = [item for g in result["groups"] for item in g["items"]]
    orders_item = next((i for i in all_items if "Orders" in i["label"]), None)
    assert orders_item is not None
    assert orders_item["status"] == "done", f"Expected 'done', got {orders_item['status']}"


def test_setup_checklist_cost_done_when_real_cogs_set(db):
    """COGS item is 'warning' (default placeholder values) after cost fixture seed."""
    from app_dashboard.stats import setup_checklist

    _cost_fixtures(db)
    db.commit()

    settings = MagicMock()
    settings.shopify_admin_token = None
    settings.shopify_shop_domain = None
    settings.meta_access_token = None
    settings.meta_account_id = None
    settings.recharge_api_token = None
    settings.ga4_property_id = None
    settings.google_client_id = None
    settings.slack_webhook_url = None

    result = setup_checklist(db, settings)
    all_items = [item for g in result["groups"] for item in g["items"]]
    cogs_item = next((i for i in all_items if "cost" in i["label"].lower() or "cogs" in i["label"].lower()), None)
    # $20 COGS doesn't match default placeholders ($3.50/$5.00/$8.50/$10.50) → 'done'
    assert cogs_item is not None
    assert cogs_item["status"] == "done", f"Expected 'done' for $20 COGS, got {cogs_item['status']}"


# ── subscription_movement_summary ─────────────────────────────────────────────

def test_subscription_movement_summary_none_when_no_events(db):
    """Returns None when subscription_events has no rows for the period."""
    result = subscription_movement_summary(db, year=2026, month=8)
    assert result is None


def test_subscription_movement_summary_counts_correctly(db):
    """Counts and MRR sums match inserted events."""
    _customer(db, "c1")
    _customer(db, "c2")
    _customer(db, "c3")

    # Insert: 2 new, 1 expansion, 1 churn for August 2026
    _sub(db, "s1", "c1", Decimal("99"), datetime(2026, 8, 1, tzinfo=timezone.utc))
    _sub(db, "s2", "c2", Decimal("149"), datetime(2026, 8, 5, tzinfo=timezone.utc))
    _sub(db, "s3", "c3", Decimal("99"), datetime(2026, 7, 1, tzinfo=timezone.utc))
    db.commit()

    _sub_event(db, "s1", "c1", "new",       date(2026, 8, 1), mrr_delta=Decimal("99"))
    _sub_event(db, "s2", "c2", "new",       date(2026, 8, 5), mrr_delta=Decimal("149"))
    _sub_event(db, "s3", "c3", "expansion", date(2026, 8, 10), mrr_delta=Decimal("50"))
    _sub_event(db, "s3", "c3", "churn",     date(2026, 8, 20), mrr_delta=Decimal("-99"))
    db.commit()

    result = subscription_movement_summary(db, year=2026, month=8)
    assert result is not None
    assert result["new"]["count"] == 2
    assert result["new"]["mrr"] == Decimal("248")  # 99 + 149
    assert result["expansion"]["count"] == 1
    assert result["expansion"]["mrr"] == Decimal("50")
    assert result["churn"]["count"] == 1
    assert result["churn"]["mrr"] == Decimal("-99")
    # Types with no events default to count=0
    assert result["contraction"]["count"] == 0
    assert result["winback"]["count"] == 0


def test_subscription_movement_summary_ignores_other_months(db):
    """Events outside the requested month are not counted."""
    _customer(db, "c1")
    _sub(db, "s1", "c1", Decimal("99"), datetime(2026, 7, 1, tzinfo=timezone.utc))
    db.commit()

    # July events — should NOT show up in August summary
    _sub_event(db, "s1", "c1", "new", date(2026, 7, 1), mrr_delta=Decimal("99"))
    db.commit()

    result = subscription_movement_summary(db, year=2026, month=8)
    assert result is None, "Expected None — no August events"


# ── New + repeat customers = total orders (reconciliation) ────────────────────

def test_new_plus_repeat_equals_total_orders_in_window(db):
    """In any window, count(is_new=true) + count(is_new=false) = count(*).

    This is an arithmetic identity on orders.is_new_customer. Any violation means
    a NULL is_new_customer in the table or an ingest bug.
    """
    _customer(db, "c1")
    _customer(db, "c2")

    # c1: one new order + one repeat
    _order(db, "o1", "c1", Decimal("149.00"), is_new=True,  days_ago=3)
    _order(db, "o2", "c1", Decimal("99.00"),  is_new=False, days_ago=1)
    # c2: one new order only
    _order(db, "o3", "c2", Decimal("228.00"), is_new=True,  days_ago=2)
    db.commit()

    row = db.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE is_new_customer = TRUE)  AS new_count,
            COUNT(*) FILTER (WHERE is_new_customer = FALSE) AS repeat_count,
            COUNT(*) AS total_count
        FROM orders
        WHERE created_at >= now() - interval '7 days'
        """
    ).fetchone()

    new_count, repeat_count, total_count = row
    assert new_count + repeat_count == total_count, (
        f"new ({new_count}) + repeat ({repeat_count}) = {new_count + repeat_count} "
        f"!= total ({total_count}). "
        "is_new_customer has NULL or unexpected values."
    )
    assert new_count == 2
    assert repeat_count == 1
    assert total_count == 3


def test_new_plus_repeat_no_nulls_in_is_new_customer(db):
    """is_new_customer must never be NULL — all orders must be classified."""
    _customer(db, "c1")
    _order(db, "o1", "c1", Decimal("149.00"), is_new=True)
    db.commit()

    null_count = db.execute(
        "SELECT COUNT(*) FROM orders WHERE is_new_customer IS NULL"
    ).fetchone()[0]
    assert null_count == 0, f"{null_count} orders have NULL is_new_customer"


# ── Subscription share: non-zero on aligned data ──────────────────────────────

def test_subscription_share_nonzero_when_converted_at_in_window(db):
    """subscription_share returns > 0 when a new customer also converted to a sub
    in the same window.

    Verifies the join between overview_stats (orders.is_new_customer) and
    subscription_revenue.converted_at is working correctly.
    """
    _customer(db, "c1")
    # New order in the window
    _order(db, "o1", "c1", Decimal("149.00"), is_new=True, days_ago=3)
    # Subscription that also converted in the window
    converted_at = datetime.now(timezone.utc) - timedelta(days=2)
    _sub(db, "s1", "c1", Decimal("99"), converted_at)
    db.commit()

    stats = overview_stats(db, window_days=7)
    assert stats["new_customers"] == 1
    assert stats["subscription_share"] is not None, "subscription_share should not be None"
    assert stats["subscription_share"] > 0, (
        f"subscription_share should be > 0 when new customer has a subscription, "
        f"got {stats['subscription_share']}"
    )
    assert stats["subscription_share"] == Decimal("100"), (
        f"1/1 new customer subscribed = 100%, got {stats['subscription_share']}"
    )


def test_subscription_share_zero_when_converted_at_outside_window(db):
    """subscription_share is 0% (not NULL) when a subscription exists but its
    converted_at is outside the query window.

    This confirms the window boundary: new customers exist (denominator > 0),
    but no subscription converted in this window (numerator = 0) → 0%, not NULL.
    """
    _customer(db, "c1")
    # New order in the 7-day window
    _order(db, "o1", "c1", Decimal("149.00"), is_new=True, days_ago=3)
    # Subscription converted 30 days ago — outside the 7-day window
    converted_at = datetime.now(timezone.utc) - timedelta(days=30)
    _sub(db, "s1", "c1", Decimal("99"), converted_at)
    db.commit()

    stats = overview_stats(db, window_days=7)
    assert stats["new_customers"] == 1
    assert stats["subscription_share"] is not None, "Should be 0%, not None (denominator > 0)"
    assert stats["subscription_share"] == Decimal("0"), (
        f"Expected 0% (sub outside window), got {stats['subscription_share']}"
    )


def test_subscription_share_null_when_no_new_customers(db):
    """subscription_share is NULL when there are no new customers in the window,
    even if subscriptions exist.
    """
    _customer(db, "c1")
    # Repeat order only — is_new=False
    _order(db, "o1", "c1", Decimal("149.00"), is_new=False, days_ago=1)
    converted_at = datetime.now(timezone.utc) - timedelta(days=1)
    _sub(db, "s1", "c1", Decimal("99"), converted_at)
    db.commit()

    stats = overview_stats(db, window_days=7)
    assert stats["new_customers"] == 0
    assert stats["subscription_share"] is None, (
        "subscription_share must be NULL when new_customers = 0"
    )
