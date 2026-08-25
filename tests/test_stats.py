"""stats.py computes the numbers on the dashboard, so it gets direct coverage
rather than being exercised only through page renders against an empty DB.
"""

import pytest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app_dashboard.stats import (
    COMPARED,
    days_of_cover,
    overview_comparison,
    overview_stats,
)


# ── New Densologie schema helpers ─────────────────────────────────────────────

def _customer(db, cid, email_hash=None, country="US"):
    if email_hash is None:
        email_hash = f"fake_hash_{cid}"
    db.execute(
        "insert into customers (id, email_hash, first_order_at, country) "
        "values (%s, %s, now(), %s)",
        (cid, email_hash, country),
    )


def _order(db, oid, cust_id, total, refunded=Decimal("0"), is_new=True,
           sku="HAIR-SERUM-50ML", days_ago=0):
    created_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.execute(
        "insert into orders (id, customer_id, created_at, total, refunded, currency, "
        "is_new_customer, line_items) values (%s, %s, %s, %s, %s, 'USD', %s, %s::jsonb)",
        (
            oid, cust_id, created_at, total, refunded, is_new,
            f'[{{"sku":"{sku}","quantity":1,"unit_price":{float(total)}}}]',
        ),
    )


def _spend(db, d, campaign_id, spend, platform="meta"):
    db.execute(
        "insert into ad_spend (date, campaign_id, campaign_name, platform, spend) "
        "values (%s, %s, %s, %s, %s)",
        (d, campaign_id, campaign_id, platform, spend),
    )


def _subscription(db, sub_id, cust_id, amount, converted_at, churned_at=None):
    db.execute(
        "insert into subscription_revenue (id, customer_id, monthly_amount, converted_at, churned_at) "
        "values (%s, %s, %s, %s, %s)",
        (sub_id, cust_id, amount, converted_at, churned_at),
    )



# ── Densologie null-not-zero invariants ───────────────────────────────────────
#
# Spec: "MER and CAC must be None (not 0 or ∞) when spend or new-customer
# counts are zero — assert it against an empty-window query."
#
# These tests use the new schema (customers, orders, ad_spend, inventory_levels,
# subscription_revenue) and assert the nullability rules stated in stats.py.

def test_null_not_zero_empty_db(db):
    """All window metrics return None on a completely empty database.

    A None means 'no data'. A 0 would mean 'data exists and sums to zero',
    which is a different — and false — claim when the tables are empty.
    """
    stats = overview_stats(db, window_days=7)
    assert stats["revenue"] is None, "revenue must be None on empty table, not 0"
    assert stats["blended_cac"] is None, "CAC must be None when there are no new customers"
    assert stats["mer"] is None, "MER must be None when there is no spend"
    assert stats["subscription_share"] is None, "sub share must be None when new_customers=0"
    assert stats["aov"] is None, "AOV must be None when there are no orders"
    # new_customers COUNT returns 0 on empty table — that is correct
    assert stats["new_customers"] == 0


def test_cac_none_when_zero_new_customers(db):
    """CAC = total_spend / new_customers. Undefined when new_customers = 0.

    This is the correct arithmetic result: you cannot divide by zero.
    Returning 0 would misrepresent 'we spent money but acquired nobody'.
    """
    _customer(db, "c1")
    _order(db, "o1", "c1", Decimal("149.00"), is_new=False)  # repeat customer
    _spend(db, date.today(), "camp1", Decimal("200.00"))
    db.commit()

    stats = overview_stats(db, window_days=7)
    assert stats["new_customers"] == 0
    assert stats["blended_cac"] is None, "CAC must be None, not 0, when no new customers"


def test_cac_none_when_zero_spend(db):
    """CAC is None when there is no ad spend in the window.

    The formula is spend/new_customers. A zero-spend CAC ('free customers')
    would be reported as $0, which is misleading; None is the correct signal
    that the spend side of the equation is absent.
    """
    _customer(db, "c1")
    _order(db, "o1", "c1", Decimal("149.00"), is_new=True)
    # No ad_spend rows inserted
    db.commit()

    stats = overview_stats(db, window_days=7)
    assert stats["new_customers"] == 1
    assert stats["blended_cac"] is None, "CAC must be None when spend is absent"


def test_mer_none_when_no_spend(db):
    """MER = revenue / spend. None when spend is zero or absent.

    An ∞ MER ('infinite efficiency') would be arithmetically wrong and
    invisible to Jinja's {{ value }} rendering anyway — None is the contract.
    """
    _customer(db, "c1")
    _order(db, "o1", "c1", Decimal("149.00"), is_new=True)
    # No ad_spend rows inserted
    db.commit()

    stats = overview_stats(db, window_days=7)
    assert stats["revenue"] is not None, "revenue should be non-None here"
    assert stats["mer"] is None, "MER must be None (not ∞) when spend is absent"


def test_mer_none_when_no_revenue(db):
    """MER is also None when revenue is zero (empty orders table)."""
    _spend(db, date.today(), "camp1", Decimal("100.00"))
    db.commit()

    stats = overview_stats(db, window_days=7)
    assert stats["revenue"] is None
    assert stats["mer"] is None, "MER must be None when revenue data is absent"


def test_subscription_share_none_when_no_new_customers(db):
    """Subscription share = sub conversions / new customers. None when denominator = 0."""
    db.commit()  # empty DB
    stats = overview_stats(db, window_days=7)
    assert stats["subscription_share"] is None


def test_aov_none_when_no_orders(db):
    """AOV = revenue / order count. None when there are no orders."""
    db.commit()
    stats = overview_stats(db, window_days=7)
    assert stats["aov"] is None


def test_overview_comparison_none_when_either_side_is_none(db):
    """overview_comparison propagates None: if current or prior is None,
    change and pct are also None — never computed with a stand-in zero.
    """
    current = {"revenue": Decimal("100.00"), "new_customers": 1,
               "blended_cac": None, "mer": None, "subscription_share": None,
               "aov": Decimal("100.00"), "days_of_cover": None}
    prior   = {"revenue": Decimal("80.00"),  "new_customers": 2,
               "blended_cac": Decimal("50.00"), "mer": Decimal("2.5"),
               "subscription_share": Decimal("50.0"),
               "aov": Decimal("90.00"), "days_of_cover": None}

    cmp = overview_comparison(current, prior)

    # revenue: both sides present — change should be computable
    assert cmp["revenue"]["change"] == Decimal("20.00")
    assert cmp["revenue"]["pct"] == 25.0

    # blended_cac: current is None — change must be None
    assert cmp["blended_cac"]["change"] is None
    assert cmp["blended_cac"]["pct"] is None

    # mer: current is None
    assert cmp["mer"]["change"] is None

    # days_of_cover: both None
    assert cmp["days_of_cover"]["change"] is None


def test_days_of_cover_none_with_fewer_than_14_days_history(db):
    """days_of_cover must return None when fewer than 14 days of orders exist.

    The formula divides by a trailing rate. If the trailing window is too short
    the rate is unreliable; returning None defers the tile until data matures.
    """
    now = datetime.now(timezone.utc)
    _customer(db, "c1")
    db.execute(
        "insert into orders (id, customer_id, created_at, total, refunded, currency, "
        "is_new_customer, line_items) values ('o1', 'c1', %s, 149, 0, 'USD', true, "
        """'[{"sku":"HAIR-SERUM-50ML","quantity":1,"unit_price":149}]'::jsonb)""",
        (now - timedelta(days=5),),
    )
    db.execute(
        "insert into inventory_levels (sku, units_on_hand, updated_at) "
        "values ('HAIR-SERUM-50ML', 800, now())"
    )
    db.commit()
    result = days_of_cover(db, "HAIR-SERUM-50ML")
    assert result is None, "days_of_cover must be None when history < 14 days"


def test_days_of_cover_none_when_no_inventory_row(db):
    """days_of_cover is None when inventory_levels has no row for the SKU."""
    result = days_of_cover(db, "HAIR-SERUM-50ML")
    assert result is None


def test_days_of_cover_computed_correctly(db):
    """Sanity-check the formula: units_on_hand / (units_sold_14d / 14)."""
    now = datetime.now(timezone.utc)
    _customer(db, "c1")
    # One anchor order at 15 days ago: passes the >=14-day data-age guard
    # without landing inside the 14-day unit-sales window.
    db.execute(
        "insert into orders (id, customer_id, created_at, total, refunded, currency, "
        "is_new_customer, line_items) values ('o_anchor', 'c1', %s, 149, 0, 'USD', false, "
        """'[{"sku":"HAIR-SERUM-50ML","quantity":1,"unit_price":149}]'::jsonb)""",
        (now - timedelta(days=15),),
    )
    # 14 orders at days 0–13: all inside the 14-day window, no boundary ambiguity.
    for i in range(14):
        db.execute(
            "insert into orders (id, customer_id, created_at, total, refunded, currency, "
            "is_new_customer, line_items) values (%s, 'c1', %s, 149, 0, 'USD', false, "
            """'[{"sku":"HAIR-SERUM-50ML","quantity":1,"unit_price":149}]'::jsonb)""",
            (f"o{i}", now - timedelta(days=i)),
        )
    db.execute(
        "insert into inventory_levels (sku, units_on_hand, updated_at) "
        "values ('HAIR-SERUM-50ML', 140, now())"
    )
    db.commit()
    # 14 units sold in 14 days = 1/day; 140 units on hand → 140 days of cover
    result = days_of_cover(db, "HAIR-SERUM-50ML")
    assert result == 140, f"expected 140 days of cover, got {result}"
