"""Tests for ingest-side offer tagging (customers.acquisition_offer).

Rules under test:
  1. reactivation   — customer had a prior churn event
  2. steep-intro-discount — first-order discount > 30% of order total
  3. coupon-only    — first-order discount > 0 and ≤ 30%
  4. full-price     — no discount, or no first order in DB

All tests use the `db` fixture (truncated, migrated schema).
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app_dashboard.ingest_recharge import (
    _derive_offer_tag,
    _tag_customer_offer,
    apply_offer_tags_retroactively,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _customer(db, cid, first_order_at=None):
    if first_order_at is None:
        first_order_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db.execute(
        "insert into customers (id, email_hash, first_order_at, country) "
        "values (%s, %s, %s, 'US') on conflict (id) do nothing",
        (cid, f"hash_{cid}", first_order_at),
    )


def _order(db, oid, cid, total, discount=Decimal("0"), days_ago=1, is_new=True):
    created_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.execute(
        "insert into orders "
        "(id, customer_id, created_at, total, refunded, currency, "
        " is_new_customer, is_subscription_order, discount_amount, line_items) "
        "values (%s, %s, %s, %s, %s, 'USD', %s, %s, %s, '[]'::jsonb) "
        "on conflict (id) do nothing",
        (oid, cid, created_at, total, Decimal("0"), is_new, False, discount),
    )


def _sub(db, sid, cid, amount, converted_at):
    db.execute(
        "insert into subscription_revenue "
        "(id, customer_id, monthly_amount, converted_at, sub_type, cash_collected, status) "
        "values (%s, %s, %s, %s, 'monthly', %s, 'active') on conflict (id) do nothing",
        (sid, cid, amount, converted_at, amount),
    )


def _churn_event(db, sid, cid, event_date):
    db.execute(
        "insert into subscription_events "
        "(subscription_id, customer_id, event_type, event_date, mrr_delta) "
        "values (%s, %s, 'churn', %s, %s)",
        (sid, cid, event_date, Decimal("-99")),
    )


# ── _derive_offer_tag ─────────────────────────────────────────────────────────

def test_derive_tag_full_price_no_discount(db):
    """No discount → full-price."""
    _customer(db, "c1")
    _order(db, "o1", "c1", Decimal("149.00"), discount=Decimal("0"))
    db.commit()
    assert _derive_offer_tag(db, "c1") == "full-price"


def test_derive_tag_full_price_no_orders(db):
    """Customer exists but has no orders → full-price (no evidence of discount)."""
    _customer(db, "c1")
    db.commit()
    assert _derive_offer_tag(db, "c1") == "full-price"


def test_derive_tag_coupon_only_small_discount(db):
    """Discount > 0 and ≤ 30% → coupon-only."""
    _customer(db, "c1")
    # 20% discount on $149 = $29.80 (29.8% ≤ 30%)
    _order(db, "o1", "c1", Decimal("149.00"), discount=Decimal("29.80"))
    db.commit()
    assert _derive_offer_tag(db, "c1") == "coupon-only"


def test_derive_tag_steep_intro_discount(db):
    """Discount > 30% of order total → steep-intro-discount."""
    _customer(db, "c1")
    # 50% off $149 = $74.50 (49.9% > 30%)
    _order(db, "o1", "c1", Decimal("149.00"), discount=Decimal("74.50"))
    db.commit()
    assert _derive_offer_tag(db, "c1") == "steep-intro-discount"


def test_derive_tag_reactivation_trumps_discount(db):
    """Prior churn event → reactivation, even if the order had a steep discount."""
    _customer(db, "c1")
    _order(db, "o1", "c1", Decimal("149.00"), discount=Decimal("100.00"))  # steep
    # First subscription + churn event
    _sub(db, "s1", "c1", Decimal("99"), datetime(2026, 1, 1, tzinfo=timezone.utc))
    _churn_event(db, "s1", "c1", date(2026, 3, 1))
    db.commit()
    # Tag should be reactivation, not steep-intro-discount
    assert _derive_offer_tag(db, "c1") == "reactivation"


def test_derive_tag_reactivation_no_discount(db):
    """Prior churn event with full-price order → still reactivation."""
    _customer(db, "c1")
    _order(db, "o1", "c1", Decimal("149.00"), discount=Decimal("0"))
    _sub(db, "s1", "c1", Decimal("99"), datetime(2026, 1, 1, tzinfo=timezone.utc))
    _churn_event(db, "s1", "c1", date(2026, 3, 1))
    db.commit()
    assert _derive_offer_tag(db, "c1") == "reactivation"


def test_derive_tag_uses_first_order_only(db):
    """Only the customer's first order (earliest created_at) is checked for discounts."""
    _customer(db, "c1")
    # First order: no discount (full-price)
    _order(db, "o1", "c1", Decimal("149.00"), discount=Decimal("0"), days_ago=10)
    # Later order: heavy discount — must NOT affect the tag
    _order(db, "o2", "c1", Decimal("149.00"), discount=Decimal("100.00"), days_ago=1)
    db.commit()
    assert _derive_offer_tag(db, "c1") == "full-price"


def test_derive_tag_discount_at_30pct_boundary(db):
    """Exactly 30% discount is coupon-only (must be STRICTLY greater than 30%)."""
    _customer(db, "c1")
    _order(db, "o1", "c1", Decimal("100.00"), discount=Decimal("30.00"))  # exactly 30%
    db.commit()
    assert _derive_offer_tag(db, "c1") == "coupon-only"


# ── _tag_customer_offer ───────────────────────────────────────────────────────

def test_tag_customer_offer_writes_tag(db):
    """_tag_customer_offer writes the derived tag to customers.acquisition_offer."""
    _customer(db, "c1")
    _order(db, "o1", "c1", Decimal("149.00"), discount=Decimal("0"))
    db.commit()

    written = _tag_customer_offer(db, "c1")
    db.commit()

    assert written is True
    row = db.execute(
        "select acquisition_offer from customers where id = 'c1'"
    ).fetchone()
    assert row[0] == "full-price"


def test_tag_customer_offer_idempotent(db):
    """Calling _tag_customer_offer twice does not overwrite an existing tag."""
    _customer(db, "c1")
    _order(db, "o1", "c1", Decimal("149.00"), discount=Decimal("0"))
    db.commit()

    _tag_customer_offer(db, "c1")
    db.commit()

    # Manually change the tag to simulate a different value, then re-run
    db.execute("update customers set acquisition_offer = 'coupon-only' where id = 'c1'")
    db.commit()

    # Second call must not overwrite the existing tag
    written = _tag_customer_offer(db, "c1")
    db.commit()

    assert written is False
    row = db.execute(
        "select acquisition_offer from customers where id = 'c1'"
    ).fetchone()
    assert row[0] == "coupon-only"  # unchanged


# ── apply_offer_tags_retroactively ────────────────────────────────────────────

def test_apply_retroactive_tags_all_customers(db):
    """apply_offer_tags_retroactively tags every subscriber without an existing tag."""
    # Three customers: full-price, coupon, steep
    _customer(db, "c1")
    _customer(db, "c2")
    _customer(db, "c3")

    _order(db, "o1", "c1", Decimal("149.00"), discount=Decimal("0"))
    _order(db, "o2", "c2", Decimal("149.00"), discount=Decimal("29.00"))  # coupon
    _order(db, "o3", "c3", Decimal("149.00"), discount=Decimal("75.00"))  # steep

    converted = datetime.now(timezone.utc) - timedelta(days=1)
    _sub(db, "s1", "c1", Decimal("99"), converted)
    _sub(db, "s2", "c2", Decimal("99"), converted)
    _sub(db, "s3", "c3", Decimal("99"), converted)
    db.commit()

    tagged = apply_offer_tags_retroactively(db)
    db.commit()

    assert tagged == 3

    tags = {
        row[0]: row[1]
        for row in db.execute(
            "select id, acquisition_offer from customers where id in ('c1','c2','c3')"
        ).fetchall()
    }
    assert tags["c1"] == "full-price"
    assert tags["c2"] == "coupon-only"
    assert tags["c3"] == "steep-intro-discount"


def test_apply_retroactive_tags_skips_already_tagged(db):
    """Customers with an existing tag are skipped — returns only newly tagged count."""
    _customer(db, "c1")
    _order(db, "o1", "c1", Decimal("149.00"), discount=Decimal("0"))
    _sub(db, "s1", "c1", Decimal("99"), datetime.now(timezone.utc) - timedelta(days=1))
    db.commit()

    # Tag once
    apply_offer_tags_retroactively(db)
    db.commit()

    # Second run should tag 0 new customers
    tagged_second = apply_offer_tags_retroactively(db)
    db.commit()

    assert tagged_second == 0


def test_apply_retroactive_tags_skips_non_subscribers(db):
    """Customers with orders but no subscription_revenue row are not tagged."""
    _customer(db, "c1")
    _order(db, "o1", "c1", Decimal("149.00"))
    # No subscription row
    db.commit()

    tagged = apply_offer_tags_retroactively(db)
    db.commit()

    assert tagged == 0
    row = db.execute(
        "select acquisition_offer from customers where id = 'c1'"
    ).fetchone()
    assert row[0] is None
