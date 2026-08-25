"""Gate 2 metric tests — every formula with deterministic seed data and exact expected values.

Each test sets up its own minimal DB state; no test relies on the full seed.
Uses the `db` fixture from conftest.py which runs all migrations and truncates
all tables before yielding the connection.

Null-not-zero: every function must return None when data is absent, not 0.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app_dashboard.stats import (
    active_subscribers,
    blended_cac_excl_reactivations,
    cash_collected_in_month,
    cohort_ltv_12m,
    get_subscriber_state_counts,
    gross_profit_per_order,
    landing_page_funnel,
    logo_churn_involuntary,
    logo_churn_voluntary,
    mrr_recognized,
    pause_outcome_split,
    pause_rate,
    paused_subscribers,
    payback_timing,
    reactivation_stats,
    rev_churn_involuntary,
    rev_churn_voluntary,
    skip_rate,
    subs_in_dunning,
    subscription_waterfall,
    subscription_waterfall_v2,
    three_revenue_streams,
    upsell_stats,
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


def _order(db, oid, cust_id, total, refunded=Decimal("0"),
           is_new=True, is_sub=False, sku="HAIR-SERUM-50ML",
           days_ago=1, discount=Decimal("0")):
    created_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.execute(
        "insert into orders "
        "(id, customer_id, created_at, total, refunded, currency, "
        " is_new_customer, is_subscription_order, discount_amount, line_items) "
        "values (%s, %s, %s, %s, %s, 'USD', %s, %s, %s, %s::jsonb) "
        "on conflict (id) do nothing",
        (
            oid, cust_id, created_at, total, refunded,
            is_new, is_sub, discount,
            f'[{{"sku":"{sku}","quantity":1,"unit_price":{float(total)}}}]',
        ),
    )


def _sub(db, sid, cust_id, amount, converted_at, churned_at=None,
         sub_type="monthly", cash_collected=None,
         churn_type=None, churn_reason=None, dunning_started_at=None,
         status=None, paused_at=None, paused_outcome=None):
    if cash_collected is None:
        cash_collected = amount
    # Infer status if not provided
    if status is None:
        if churned_at is not None:
            status = "churned"
        elif paused_at is not None:
            status = "paused"
        else:
            status = "active"
    db.execute(
        "insert into subscription_revenue "
        "(id, customer_id, monthly_amount, converted_at, churned_at, "
        " sub_type, cash_collected, churn_type, churn_reason, dunning_started_at,"
        " status, paused_at, paused_outcome) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "on conflict (id) do nothing",
        (sid, cust_id, amount, converted_at, churned_at,
         sub_type, cash_collected, churn_type, churn_reason, dunning_started_at,
         status, paused_at, paused_outcome),
    )


def _sub_event(db, sid, cust_id, event_type, event_date, mrr_delta=None, reason=None):
    db.execute(
        "insert into subscription_events "
        "(subscription_id, customer_id, event_type, event_date, mrr_delta, reason) "
        "values (%s, %s, %s, %s, %s, %s)",
        (sid, cust_id, event_type, event_date, mrr_delta, reason),
    )


def _cost_inputs(db):
    """Seed the cost_inputs and cost_settings tables (migration 021 seeds them,
    but conftest truncates all tables, so we re-seed in each test that needs them)."""
    db.execute(
        "insert into cost_inputs (sku, label, cogs_per_unit) values "
        "('HAIR-SERUM-50ML', 'Hair Serum 50ml', 3.50) on conflict (sku) do nothing"
    )
    db.execute(
        "insert into cost_settings (key, value, label) values "
        "('shipping_cost_per_order', 6.50, 'Shipping'), "
        "('payment_fee_pct', 0.029, 'Payment fee'), "
        "('return_processing_cost', 5.00, 'Return cost') "
        "on conflict (key) do nothing"
    )


# ── Group 1: MRR recognition ──────────────────────────────────────────────────

def test_mrr_recognized_monthly(db):
    """1 monthly sub at $129, active entire Jan 2026."""
    _customer(db, "c1", datetime(2025, 12, 1, tzinfo=timezone.utc))
    _sub(db, "s1", "c1", Decimal("129"),
         converted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
         sub_type="monthly", cash_collected=Decimal("129"))
    result = mrr_recognized(db, 2026, 1)
    assert result == Decimal("129"), f"Expected 129, got {result}"


def test_mrr_recognized_3mo_prepaid(db):
    """1 3-mo prepaid sub: cash=$327, monthly_amount=$109, active Jan-Mar 2026."""
    _customer(db, "c1", datetime(2025, 12, 1, tzinfo=timezone.utc))
    _sub(db, "s1", "c1", Decimal("109"),
         converted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
         churned_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
         sub_type="3mo", cash_collected=Decimal("327"))

    # MRR recognized in Jan = monthly_amount
    assert mrr_recognized(db, 2026, 1) == Decimal("109"), \
        f"Expected 109, got {mrr_recognized(db, 2026, 1)}"

    # Cash collected in Jan = $327 (initial payment)
    jan_cash = cash_collected_in_month(db, 2026, 1)
    assert jan_cash == Decimal("327"), f"Expected 327, got {jan_cash}"

    # Cash collected in Feb = $0 (prepaid, no renewal charge)
    feb_cash = cash_collected_in_month(db, 2026, 2)
    assert feb_cash == Decimal("0"), f"Expected 0, got {feb_cash}"


def test_mrr_recognized_6mo_prepaid(db):
    """1 6-mo prepaid sub: cash=$594, monthly_amount=$99, active Jan-Jun 2026."""
    _customer(db, "c1", datetime(2025, 12, 1, tzinfo=timezone.utc))
    _sub(db, "s1", "c1", Decimal("99"),
         converted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
         churned_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
         sub_type="6mo", cash_collected=Decimal("594"))

    assert mrr_recognized(db, 2026, 1) == Decimal("99"), \
        f"Expected 99, got {mrr_recognized(db, 2026, 1)}"

    jan_cash = cash_collected_in_month(db, 2026, 1)
    assert jan_cash == Decimal("594"), f"Expected 594, got {jan_cash}"

    feb_cash = cash_collected_in_month(db, 2026, 2)
    assert feb_cash == Decimal("0"), f"Expected 0, got {feb_cash}"


# ── Group 2: Churn ────────────────────────────────────────────────────────────

def _setup_10_active_subs_at_jan1(db):
    """Create 10 active subs at Jan 1, 2026. Returns list of (sub_id, cust_id)."""
    pairs = []
    for i in range(10):
        cid = f"c{i}"
        sid = f"s{i}"
        _customer(db, cid, datetime(2025, 11, 1, tzinfo=timezone.utc))
        _sub(db, sid, cid, Decimal("129"),
             converted_at=datetime(2025, 12, 1, tzinfo=timezone.utc))
        pairs.append((sid, cid))
    return pairs


def test_logo_churn_voluntary(db):
    """10 active subs at Jan 1; 2 voluntary churns in Jan."""
    pairs = _setup_10_active_subs_at_jan1(db)

    # Churn 2 subs voluntarily in Jan
    for sid, cid in pairs[:2]:
        db.execute(
            "update subscription_revenue set churned_at = %s, churn_type = 'voluntary', "
            "churn_reason = 'cancelled' where id = %s",
            (datetime(2026, 1, 15, tzinfo=timezone.utc), sid),
        )

    result = logo_churn_voluntary(db, 2026, 1)
    assert result == Decimal("0.20"), f"Expected 0.20, got {result}"


def test_logo_churn_involuntary_confirmed(db):
    """10 active subs at Jan 1; 1 involuntary churn, dunning started 20 days before churn."""
    pairs = _setup_10_active_subs_at_jan1(db)

    churned_at = datetime(2026, 1, 25, tzinfo=timezone.utc)
    dunning_started = churned_at - timedelta(days=20)
    sid, cid = pairs[0]
    db.execute(
        "update subscription_revenue set churned_at = %s, churn_type = 'involuntary', "
        "churn_reason = 'payment_failed', dunning_started_at = %s where id = %s",
        (churned_at, dunning_started, sid),
    )

    result = logo_churn_involuntary(db, 2026, 1)
    assert result == Decimal("0.10"), f"Expected 0.10, got {result}"


def test_logo_churn_involuntary_in_dunning_excluded(db):
    """10 active subs; 1 has dunning started 5 days ago (inside 14-day window), no churned_at."""
    pairs = _setup_10_active_subs_at_jan1(db)

    dunning_started = datetime.now(timezone.utc) - timedelta(days=5)
    sid, cid = pairs[0]
    db.execute(
        "update subscription_revenue set dunning_started_at = %s, "
        "churn_type = 'involuntary', churn_reason = 'payment_failed' where id = %s",
        (dunning_started, sid),
    )

    # Involuntary churn in Jan 2026 = 0 (dunning not confirmed yet)
    result = logo_churn_involuntary(db, 2026, 1)
    assert result == Decimal("0"), f"Expected 0, got {result}"

    # But subs_in_dunning should show 1
    dunning = subs_in_dunning(db)
    assert dunning["count"] == 1, f"Expected 1 in dunning, got {dunning['count']}"


def test_rev_churn_voluntary(db):
    """10 subs all at $129/mo at Jan 1; 2 voluntary churns in Jan."""
    pairs = _setup_10_active_subs_at_jan1(db)

    for sid, cid in pairs[:2]:
        db.execute(
            "update subscription_revenue set churned_at = %s, churn_type = 'voluntary', "
            "churn_reason = 'cancelled' where id = %s",
            (datetime(2026, 1, 15, tzinfo=timezone.utc), sid),
        )

    result = rev_churn_voluntary(db, 2026, 1)
    assert result == Decimal("0.20"), f"Expected 0.20, got {result}"


def test_skip_rate(db):
    """10 active subs; 2 skip events in Jan 2026."""
    pairs = _setup_10_active_subs_at_jan1(db)

    for sid, cid in pairs[:2]:
        _sub_event(db, sid, cid, "skip", date(2026, 1, 10))

    result = skip_rate(db, 2026, 1)
    assert result == Decimal("0.20"), f"Expected 0.20, got {result}"


# ── Group 3: Revenue streams ──────────────────────────────────────────────────

def test_three_revenue_streams_sum_to_total(db):
    """3 orders: new customer ($149), subscription ($129), non-sub repeat ($99)."""
    _customer(db, "c1")
    _customer(db, "c2")
    _customer(db, "c3")

    # New customer order
    _order(db, "o1", "c1", Decimal("149"), is_new=True, is_sub=False, days_ago=1)
    # Subscription order
    _order(db, "o2", "c2", Decimal("129"), is_new=False, is_sub=True, days_ago=1)
    # Non-sub repeat
    _order(db, "o3", "c3", Decimal("99"), is_new=False, is_sub=False, days_ago=1)

    streams = three_revenue_streams(db, window_days=30)

    assert streams["new_customer_revenue"] == Decimal("149"), \
        f"new_customer_revenue: expected 149, got {streams['new_customer_revenue']}"
    assert streams["subscription_recurring_revenue"] == Decimal("129"), \
        f"sub_recurring: expected 129, got {streams['subscription_recurring_revenue']}"
    assert streams["non_sub_repeat_revenue"] == Decimal("99"), \
        f"non_sub_repeat: expected 99, got {streams['non_sub_repeat_revenue']}"
    assert streams["total"] == Decimal("377"), \
        f"total: expected 377, got {streams['total']}"

    # Reconciliation T16
    computed_total = (
        (streams["new_customer_revenue"] or Decimal("0"))
        + (streams["subscription_recurring_revenue"] or Decimal("0"))
        + (streams["non_sub_repeat_revenue"] or Decimal("0"))
    )
    assert computed_total == streams["total"], \
        f"Reconciliation T16 FAIL: {computed_total} != {streams['total']}"


def test_three_streams_null_when_no_data(db):
    """Empty period: all three streams must be None."""
    # Use window_days=1 but insert orders days_ago=365 (outside window)
    _customer(db, "c1")
    _order(db, "o1", "c1", Decimal("149"), is_new=True, days_ago=365)

    streams = three_revenue_streams(db, window_days=1)
    assert streams["new_customer_revenue"] is None, \
        f"Expected None, got {streams['new_customer_revenue']}"
    assert streams["subscription_recurring_revenue"] is None, \
        f"Expected None, got {streams['subscription_recurring_revenue']}"
    assert streams["non_sub_repeat_revenue"] is None, \
        f"Expected None, got {streams['non_sub_repeat_revenue']}"


# ── Group 4: Gross profit + cost inputs ──────────────────────────────────────

def test_gross_profit_single_order(db):
    """Serum order $149, COGS=$3.50, shipping=$6.50, fee=2.9%."""
    _cost_inputs(db)
    _customer(db, "c1")
    _order(db, "o1", "c1", Decimal("149"), is_new=True, sku="HAIR-SERUM-50ML")

    # Expected: 149 - 3.50 - 6.50 - (149 * 0.029) = 149 - 3.50 - 6.50 - 4.321 = 134.679
    # Using exact Decimal: 149 * 0.029 = 4.321
    expected = Decimal("149") - Decimal("3.50") - Decimal("6.50") - (Decimal("149") * Decimal("0.0290"))
    # = 149 - 3.50 - 6.50 - 4.3210 = 134.6790
    result = gross_profit_per_order(db, "o1")
    assert result is not None, "gross_profit_per_order returned None"
    # Allow 1 cent tolerance for decimal arithmetic
    assert abs(result - expected) <= Decimal("0.01"), \
        f"Expected ~{expected}, got {result}"


def test_gross_profit_none_when_sku_missing(db):
    """Order with unknown SKU must return None."""
    _cost_inputs(db)
    _customer(db, "c1")
    _order(db, "o1", "c1", Decimal("149"), is_new=True, sku="UNKNOWN-SKU")

    result = gross_profit_per_order(db, "o1")
    assert result is None, f"Expected None, got {result}"


# ── Group 5: LTV ──────────────────────────────────────────────────────────────

def test_theoretical_ltv_none_when_no_churn_data(db):
    """No subs at all → theoretical_ltv = None."""
    from app_dashboard.stats import theoretical_ltv
    result = theoretical_ltv(db)
    assert result is None, f"Expected None, got {result}"


def test_theoretical_ltv_formula(db):
    """10 subs at $129/mo each; seed GP via sub orders; check LTV is positive."""
    from app_dashboard.stats import theoretical_ltv
    _cost_inputs(db)

    # Create 10 customers with subs, including 2 voluntary churns last month
    now = datetime.now(timezone.utc)
    last_month_start = now.replace(day=1) - timedelta(days=1)
    last_month_start = last_month_start.replace(day=1)

    for i in range(10):
        cid = f"c{i}"
        sid = f"s{i}"
        _customer(db, cid, datetime(2026, 1, 1, tzinfo=timezone.utc))
        _sub(db, sid, cid, Decimal("129"),
             converted_at=last_month_start - timedelta(days=30),
             sub_type="monthly", cash_collected=Decimal("129"))
        # Each customer has a recent sub order
        _order(db, f"o{i}", cid, Decimal("129"),
               is_new=False, is_sub=True, sku="HAIR-SERUM-50ML", days_ago=45)

    # Churn 2 voluntarily in last month
    for i in range(2):
        churn_date = last_month_start + timedelta(days=10)
        db.execute(
            "update subscription_revenue set churned_at = %s, "
            "churn_type = 'voluntary', churn_reason = 'cancelled' where id = %s",
            (churn_date, f"s{i}"),
        )

    result = theoretical_ltv(db)
    # We just verify it's not None and is a positive number (exact value depends on timing)
    assert result is not None, "theoretical_ltv returned None with data present"
    assert result > Decimal("0"), f"Expected positive LTV, got {result}"


# ── Group 6: Upsell take rates ────────────────────────────────────────────────

def test_upsell_take_rate_priority_shipping(db):
    """10 orders; 3 priority_shipping accepted=true → 30% take rate."""
    for i in range(10):
        cid = f"c{i}"
        oid = f"o{i}"
        _customer(db, cid)
        _order(db, oid, cid, Decimal("149"), is_new=True, days_ago=1)

        accepted = i < 3
        db.execute(
            "insert into upsell_events (order_id, upsell_type, accepted, amount) "
            "values (%s, 'priority_shipping', %s, %s)",
            (oid, accepted, Decimal("12.00") if accepted else Decimal("0.00")),
        )

    result = upsell_stats(db, window_days=30)
    take_rate = result["priority_shipping"]["take_rate"]
    assert take_rate is not None, "take_rate should not be None"
    assert abs(take_rate - Decimal("30")) <= Decimal("0.01"), \
        f"Expected 30%, got {take_rate}"


def test_upsell_null_when_no_events(db):
    """No upsell_events → take_rate is None for all types."""
    result = upsell_stats(db, window_days=30)
    for utype in ["priority_shipping", "upsell_t1", "upsell_t2", "upsell_t3", "aftersell"]:
        assert result[utype]["take_rate"] is None, \
            f"Expected None for {utype}, got {result[utype]['take_rate']}"


# ── Group 7: Landing page funnel ──────────────────────────────────────────────

def test_landing_page_funnel_bucketing(db):
    """ga4_funnel rows for pdp, listicle, lander; verify rates."""
    today = date.today()
    # pdp: sessions=100, atcs=8, checkouts=4, purchases=2
    db.execute(
        "insert into ga4_funnel (date, utm_source, utm_medium, sessions, add_to_carts, "
        "begin_checkouts, purchases, landing_page_type) "
        "values (%s, '', '', 100, 8, 4, 2, 'pdp')",
        (today,),
    )
    # listicle: sessions=200, atcs=8, checkouts=4, purchases=2
    db.execute(
        "insert into ga4_funnel (date, utm_source, utm_medium, sessions, add_to_carts, "
        "begin_checkouts, purchases, landing_page_type) "
        "values (%s, '', '', 200, 12, 6, 4, 'listicle')",
        (today,),
    )
    # lander
    db.execute(
        "insert into ga4_funnel (date, utm_source, utm_medium, sessions, add_to_carts, "
        "begin_checkouts, purchases, landing_page_type) "
        "values (%s, '', '', 50, 5, 3, 1, 'lander')",
        (today,),
    )

    results = landing_page_funnel(db, window_days=30)
    by_type = {r["page_type"]: r for r in results}

    assert "pdp" in by_type, "pdp should be in results"
    pdp = by_type["pdp"]
    assert pdp["sessions"] == 100
    assert pdp["atc_rate"] == 8.0, f"pdp atc_rate: expected 8.0, got {pdp['atc_rate']}"
    assert pdp["checkout_rate"] == 4.0, f"pdp checkout_rate: expected 4.0, got {pdp['checkout_rate']}"
    assert pdp["purchase_rate"] == 2.0, f"pdp purchase_rate: expected 2.0, got {pdp['purchase_rate']}"


def test_direct_checkout_excluded_from_funnel(db):
    """direct_checkout rows must not appear in landing_page_funnel results."""
    today = date.today()
    db.execute(
        "insert into ga4_funnel (date, utm_source, utm_medium, sessions, add_to_carts, "
        "begin_checkouts, purchases, landing_page_type) "
        "values (%s, '', '', 50, 5, 3, 2, 'direct_checkout')",
        (today,),
    )
    db.execute(
        "insert into ga4_funnel (date, utm_source, utm_medium, sessions, add_to_carts, "
        "begin_checkouts, purchases, landing_page_type) "
        "values (%s, 'meta', '', 100, 10, 5, 3, 'pdp')",
        (today,),
    )

    results = landing_page_funnel(db, window_days=30)
    page_types = [r["page_type"] for r in results]
    assert "direct_checkout" not in page_types, \
        f"direct_checkout should be excluded, got: {page_types}"
    assert "pdp" in page_types


# ── Group 8: Subscription waterfall ──────────────────────────────────────────

def test_subscription_waterfall(db):
    """Seed subscription_events for Jan 2026 and verify waterfall arithmetic."""
    # Create customers and subs needed for events
    for i in range(15):
        cid = f"c{i}"
        sid = f"s{i}"
        _customer(db, cid, datetime(2025, 12, 1, tzinfo=timezone.utc))
        _sub(db, sid, cid, Decimal("129"),
             converted_at=datetime(2025, 12, 1, tzinfo=timezone.utc))

    # Voluntary churn sub
    vol_cid, vol_sid = "cv1", "sv1"
    _customer(db, vol_cid, datetime(2025, 12, 1, tzinfo=timezone.utc))
    _sub(db, vol_sid, vol_cid, Decimal("129"),
         converted_at=datetime(2025, 12, 1, tzinfo=timezone.utc),
         churned_at=datetime(2026, 1, 20, tzinfo=timezone.utc),
         churn_type="voluntary")

    # Involuntary churn sub (confirmed >=14 days)
    invol_cid, invol_sid = "ci1", "si1"
    _customer(db, invol_cid, datetime(2025, 12, 1, tzinfo=timezone.utc))
    _sub(db, invol_sid, invol_cid, Decimal("129"),
         converted_at=datetime(2025, 12, 1, tzinfo=timezone.utc),
         churned_at=datetime(2026, 1, 25, tzinfo=timezone.utc),
         churn_type="involuntary",
         dunning_started_at=datetime(2026, 1, 5, tzinfo=timezone.utc))

    # Beginning MRR: 10 subs new before Jan 1 (each $129)
    for i in range(10):
        _sub_event(db, f"s{i}", f"c{i}", "new", date(2025, 12, 1), mrr_delta=Decimal("129"))

    # New in Jan: 2 new subs × $129
    for i in range(10, 12):
        _sub_event(db, f"s{i}", f"c{i}", "new", date(2026, 1, 5), mrr_delta=Decimal("129"))

    # Expansion in Jan: +$60
    _sub_event(db, "s12", "c12", "expansion", date(2026, 1, 10), mrr_delta=Decimal("60"))

    # Contraction in Jan: -$30 (stored as negative)
    _sub_event(db, "s13", "c13", "contraction", date(2026, 1, 10), mrr_delta=Decimal("-30"))

    # Churn voluntary in Jan
    _sub_event(db, vol_sid, vol_cid, "churn", date(2026, 1, 20), mrr_delta=Decimal("-129"),
               reason="cancelled")

    # Churn involuntary in Jan
    _sub_event(db, invol_sid, invol_cid, "churn", date(2026, 1, 25), mrr_delta=Decimal("-129"),
               reason="payment_failed")

    result = subscription_waterfall(db, 2026, 1)

    assert result["beginning_mrr"] == Decimal("1290"), \
        f"beginning_mrr: expected 1290, got {result['beginning_mrr']}"
    assert result["new_mrr"] == Decimal("258"), \
        f"new_mrr: expected 258, got {result['new_mrr']}"
    assert result["expansion_mrr"] == Decimal("60"), \
        f"expansion_mrr: expected 60, got {result['expansion_mrr']}"
    assert result["contraction_mrr"] == Decimal("30"), \
        f"contraction_mrr: expected 30 (positive), got {result['contraction_mrr']}"
    assert result["churned_mrr_voluntary"] == Decimal("129"), \
        f"churned_mrr_voluntary: expected 129, got {result['churned_mrr_voluntary']}"
    assert result["churned_mrr_involuntary"] == Decimal("129"), \
        f"churned_mrr_involuntary: expected 129, got {result['churned_mrr_involuntary']}"
    assert result["ending_mrr"] == Decimal("1320"), \
        f"ending_mrr: expected 1320, got {result['ending_mrr']}"


# ── Group 9: Reconciliation ───────────────────────────────────────────────────

def test_reconciliation_three_streams_equals_total_revenue(db):
    """new + sub + non_sub_repeat = total net revenue to the cent."""
    _customer(db, "c1")
    _customer(db, "c2")
    _customer(db, "c3")

    _order(db, "o1", "c1", Decimal("149"), is_new=True, is_sub=False, days_ago=5)
    _order(db, "o2", "c2", Decimal("129"), is_new=False, is_sub=True, days_ago=5)
    _order(db, "o3", "c3", Decimal("99"), is_new=False, is_sub=False, days_ago=5)
    # Add a refunded order to test net
    _order(db, "o4", "c1", Decimal("149"), refunded=Decimal("149"),
           is_new=False, is_sub=False, days_ago=5)

    streams = three_revenue_streams(db, window_days=30)
    computed = (
        (streams["new_customer_revenue"] or Decimal("0"))
        + (streams["subscription_recurring_revenue"] or Decimal("0"))
        + (streams["non_sub_repeat_revenue"] or Decimal("0"))
    )
    assert abs(computed - (streams["total"] or Decimal("0"))) <= Decimal("0.01"), (
        f"Reconciliation FAIL: new={streams['new_customer_revenue']} + "
        f"sub={streams['subscription_recurring_revenue']} + "
        f"non_sub={streams['non_sub_repeat_revenue']} = {computed} "
        f"!= total={streams['total']}"
    )


def test_reconciliation_waterfall_ending_mrr(db):
    """Waterfall arithmetic: beginning + new + expansion - contraction - churn = ending."""
    for i in range(5):
        cid = f"c{i}"
        sid = f"s{i}"
        _customer(db, cid, datetime(2025, 11, 1, tzinfo=timezone.utc))
        _sub(db, sid, cid, Decimal("129"),
             converted_at=datetime(2025, 12, 1, tzinfo=timezone.utc))
        _sub_event(db, sid, cid, "new", date(2025, 12, 1), mrr_delta=Decimal("129"))

    # New in month
    for i in range(5, 7):
        cid = f"c{i}"
        sid = f"s{i}"
        _customer(db, cid, datetime(2026, 1, 1, tzinfo=timezone.utc))
        _sub(db, sid, cid, Decimal("129"),
             converted_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        _sub_event(db, sid, cid, "new", date(2026, 1, 5), mrr_delta=Decimal("129"))

    result = subscription_waterfall(db, 2026, 1)
    expected_ending = (
        result["beginning_mrr"]
        + result["new_mrr"]
        + result["expansion_mrr"]
        - result["contraction_mrr"]
        - result["churned_mrr_voluntary"]
        - result["churned_mrr_involuntary"]
    )
    breakdown = (
        f"beginning={result['beginning_mrr']} + new={result['new_mrr']} + "
        f"expansion={result['expansion_mrr']} - contraction={result['contraction_mrr']} - "
        f"churn_vol={result['churned_mrr_voluntary']} - churn_invol={result['churned_mrr_involuntary']}"
    )
    assert result["ending_mrr"] == expected_ending, (
        f"Waterfall reconciliation FAIL: {breakdown} = {expected_ending} "
        f"!= ending_mrr={result['ending_mrr']}"
    )


def test_null_not_zero_all_new_metrics(db):
    """Empty DB: all new metrics return None, not 0 or Decimal('0')."""
    # MRR recognized in a future month with no data
    assert mrr_recognized(db, 2099, 1) is None, \
        f"mrr_recognized: expected None, got {mrr_recognized(db, 2099, 1)}"

    assert logo_churn_voluntary(db, 2099, 1) is None, \
        f"logo_churn_voluntary: expected None, got {logo_churn_voluntary(db, 2099, 1)}"

    assert logo_churn_involuntary(db, 2099, 1) is None, \
        f"logo_churn_involuntary: expected None, got {logo_churn_involuntary(db, 2099, 1)}"

    assert rev_churn_voluntary(db, 2099, 1) is None, \
        f"rev_churn_voluntary: expected None, got {rev_churn_voluntary(db, 2099, 1)}"

    assert rev_churn_involuntary(db, 2099, 1) is None, \
        f"rev_churn_involuntary: expected None, got {rev_churn_involuntary(db, 2099, 1)}"

    assert skip_rate(db, 2099, 1) is None, \
        f"skip_rate: expected None, got {skip_rate(db, 2099, 1)}"

    # three_revenue_streams: all three streams None in an empty window
    streams = three_revenue_streams(db, window_days=1)
    assert streams["new_customer_revenue"] is None, \
        f"new_customer_revenue: expected None, got {streams['new_customer_revenue']}"
    assert streams["subscription_recurring_revenue"] is None, \
        f"subscription_recurring_revenue: expected None, got {streams['subscription_recurring_revenue']}"
    assert streams["non_sub_repeat_revenue"] is None, \
        f"non_sub_repeat_revenue: expected None, got {streams['non_sub_repeat_revenue']}"


# ── Group 10: Pause lifecycle ─────────────────────────────────────────────────

def test_pause_excludes_from_active_count(db):
    """Seed: 5 active subs, 2 paused subs.
    Expected: active_subscribers(conn, as_of=now) == 5 (not 7).
    Expected: paused_subscribers(conn)['count'] == 2."""
    now = datetime.now(timezone.utc)
    for i in range(5):
        cid = f"c{i}"
        _customer(db, cid, datetime(2026, 1, 1, tzinfo=timezone.utc))
        _sub(db, f"s{i}", cid, Decimal("129"),
             converted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
             status="active")

    # 2 paused subs
    for i in range(5, 7):
        cid = f"cp{i}"
        _customer(db, cid, datetime(2026, 1, 1, tzinfo=timezone.utc))
        _sub(db, f"sp{i}", cid, Decimal("129"),
             converted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
             status="paused",
             paused_at=datetime(2026, 6, 1, tzinfo=timezone.utc))

    db.commit()
    active = active_subscribers(db, as_of=now)
    assert active == 5, f"Expected 5 active, got {active}"

    paused = paused_subscribers(db)
    assert paused["count"] == 2, f"Expected 2 paused, got {paused['count']}"


def test_pause_deferred_mrr(db):
    """Seed: 2 paused subs at $129/mo each.
    Expected: paused_subscribers(conn)['deferred_mrr'] == Decimal('258')."""
    for i in range(2):
        cid = f"c{i}"
        _customer(db, cid, datetime(2026, 1, 1, tzinfo=timezone.utc))
        _sub(db, f"s{i}", cid, Decimal("129"),
             converted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
             status="paused",
             paused_at=datetime(2026, 6, 1, tzinfo=timezone.utc))

    db.commit()
    result = paused_subscribers(db)
    assert result["deferred_mrr"] == Decimal("258"), \
        f"Expected deferred_mrr=258, got {result['deferred_mrr']}"


def test_pause_not_counted_as_churn(db):
    """Seed: 10 active subs at Jan 1; 2 pause events in Jan; 0 churn events.
    Expected: logo_churn_voluntary(conn, 2026, 1) == Decimal('0') or None."""
    pairs = _setup_10_active_subs_at_jan1(db)

    # Add pause events for 2 of them — these are NOT churn events
    for sid, cid in pairs[:2]:
        _sub_event(db, sid, cid, "pause", date(2026, 1, 10))

    result = logo_churn_voluntary(db, 2026, 1)
    # No voluntary churns seeded, so should be 0 (not None — there are subs at start)
    assert result is not None and result == Decimal("0"), \
        f"Expected 0, got {result} (pauses must not count as churn)"


def test_pause_outcome_split(db):
    """Seed: 10 paused subs total:
      4 reactivated, 3 cancelled, 3 still paused.
    Verify outcome split percentages."""
    now = datetime.now(timezone.utc)
    configs = [
        # (paused_outcome, status)
        ("reactivated", "active"),  # 0
        ("reactivated", "active"),  # 1
        ("reactivated", "active"),  # 2
        ("reactivated", "active"),  # 3
        ("cancelled", "churned"),   # 4
        ("cancelled", "churned"),   # 5
        ("cancelled", "churned"),   # 6
        (None, "paused"),           # 7
        (None, "paused"),           # 8
        (None, "paused"),           # 9
    ]
    for i, (outcome, sts) in enumerate(configs):
        cid = f"c{i}"
        _customer(db, cid, datetime(2026, 1, 1, tzinfo=timezone.utc))
        _sub(db, f"s{i}", cid, Decimal("129"),
             converted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
             status=sts,
             paused_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
             paused_outcome=outcome,
             churned_at=datetime(2026, 5, 1, tzinfo=timezone.utc) if sts == "churned" else None)

    db.commit()
    result = pause_outcome_split(db)
    assert result is not None, "pause_outcome_split returned None"
    assert result["total_paused"] == 10, f"Expected 10, got {result['total_paused']}"
    assert result["reactivated_pct"] == Decimal("40"), \
        f"Expected reactivated_pct=40, got {result['reactivated_pct']}"
    assert result["cancelled_pct"] == Decimal("30"), \
        f"Expected cancelled_pct=30, got {result['cancelled_pct']}"
    assert result["still_paused_pct"] == Decimal("30"), \
        f"Expected still_paused_pct=30, got {result['still_paused_pct']}"


# ── Group 11: Win-back / reactivation ────────────────────────────────────────

def test_winback_stays_in_original_cohort(db):
    """Customer A's original cohort is 2026-01. After win-back with new orders in 2026-06,
    cohort LTV revenue (per customer_cohorts) still comes from first_order_at in 2026-01."""
    # Customer A with first_order in Jan 2026
    cust_a_first = datetime(2026, 1, 10, tzinfo=timezone.utc)
    _customer(db, "custA", first_order_at=cust_a_first)

    # Original orders in Jan-Mar 2026
    _order(db, "oA1", "custA", Decimal("129"), is_new=True, is_sub=True, days_ago=200)

    # Win-back order in June 2026 — is_new_customer=False, is_subscription_order=True
    _order(db, "oA2", "custA", Decimal("129"), is_new=False, is_sub=True, days_ago=80)

    # The customer's cohort is still Jan 2026
    cohort_row = db.execute(
        "select to_char(date_trunc('month', first_order_at), 'YYYY-MM') from customers where id='custA'"
    ).fetchone()
    assert cohort_row[0] == "2026-01", f"Expected cohort 2026-01, got {cohort_row[0]}"

    # Win-back order must NOT be counted as new_customer_revenue
    streams = three_revenue_streams(db, window_days=365)
    assert streams["new_customer_revenue"] == Decimal("129"), \
        f"Only original order should be new; got {streams['new_customer_revenue']}"
    assert streams["subscription_recurring_revenue"] == Decimal("129"), \
        f"Win-back order should be in sub_recurring; got {streams['subscription_recurring_revenue']}"


def test_winback_not_new_customer(db):
    """Win-back order (is_new_customer=False) must appear in subscription_recurring_revenue,
    not in new_customer_revenue."""
    _customer(db, "c1")
    # First order (new customer)
    _order(db, "o1", "c1", Decimal("149"), is_new=True, is_sub=False, days_ago=10)
    # Win-back order
    _order(db, "o2", "c1", Decimal("129"), is_new=False, is_sub=True, days_ago=5)

    streams = three_revenue_streams(db, window_days=30)
    assert streams["new_customer_revenue"] == Decimal("149"), \
        f"Only new order should be in new_customer_revenue; got {streams['new_customer_revenue']}"
    assert streams["subscription_recurring_revenue"] == Decimal("129"), \
        f"Win-back sub order should be in sub_recurring; got {streams['subscription_recurring_revenue']}"


def test_reactivation_waterfall_bucket(db):
    """Seed subscription_events for Jan 2026:
      2 'new' events: mrr_delta=+129 each (total new_mrr=258)
      1 'winback' event: mrr_delta=+129
    Expected: reactivation_mrr == 129, new_mrr == 258 (winback NOT in new_mrr)."""
    for i in range(5):
        cid = f"c{i}"
        _customer(db, cid, datetime(2025, 12, 1, tzinfo=timezone.utc))
        _sub(db, f"s{i}", cid, Decimal("129"),
             converted_at=datetime(2025, 12, 1, tzinfo=timezone.utc))
        _sub_event(db, f"s{i}", cid, "new", date(2025, 12, 1), mrr_delta=Decimal("129"))

    # 2 new subs in Jan
    for i in range(5, 7):
        cid = f"cnew{i}"
        _customer(db, cid, datetime(2026, 1, 5, tzinfo=timezone.utc))
        _sub(db, f"snew{i}", cid, Decimal("129"),
             converted_at=datetime(2026, 1, 5, tzinfo=timezone.utc))
        _sub_event(db, f"snew{i}", cid, "new", date(2026, 1, 5), mrr_delta=Decimal("129"))

    # 1 win-back sub in Jan (new sub row for an existing customer)
    wb_cid = "cwb"
    _customer(db, wb_cid, datetime(2025, 6, 1, tzinfo=timezone.utc))
    # Original sub (churned)
    _sub(db, "swb_orig", wb_cid, Decimal("129"),
         converted_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
         churned_at=datetime(2025, 10, 1, tzinfo=timezone.utc),
         churn_type="voluntary")
    # Win-back sub
    _sub(db, "swb_new", wb_cid, Decimal("129"),
         converted_at=datetime(2026, 1, 15, tzinfo=timezone.utc))
    # Winback event
    _sub_event(db, "swb_new", wb_cid, "winback", date(2026, 1, 15), mrr_delta=Decimal("129"))

    result = subscription_waterfall_v2(db, 2026, 1)
    assert result["new_mrr"] == Decimal("258"), \
        f"new_mrr: expected 258, got {result['new_mrr']}"
    assert result["reactivation_mrr"] == Decimal("129"), \
        f"reactivation_mrr: expected 129, got {result['reactivation_mrr']}"


def test_cac_excludes_reactivations(db):
    """Seed: $1000 ad spend in last 30 days, 5 new customers (winback_count=0),
    2 win-back customers (winback_count=1).
    Expected: blended_cac_excl_reactivations == 200 (1000/5, NOT 1000/7)."""
    from datetime import date as _date

    today = _date.today()
    db.execute(
        "insert into ad_spend (date, campaign_id, campaign_name, platform, spend) "
        "values (%s, 'test_camp', 'Test', 'meta', %s) on conflict do nothing",
        (today, Decimal("1000")),
    )

    # 5 genuine new customers
    for i in range(5):
        cid = f"c{i}"
        _customer(db, cid)
        _order(db, f"o{i}", cid, Decimal("149"), is_new=True, days_ago=1)
        # winback_count defaults to 0

    # 2 win-back customers
    for i in range(5, 7):
        cid = f"cwb{i}"
        _customer(db, cid)
        _order(db, f"owb{i}", cid, Decimal("129"), is_new=False, is_sub=True, days_ago=1)
        db.execute(
            "update customers set winback_count=1 where id=%s", (cid,)
        )

    db.commit()

    result = blended_cac_excl_reactivations(db, window_days=30)
    assert result is not None, "blended_cac_excl_reactivations returned None"
    assert result == Decimal("200"), \
        f"Expected 200 (1000/5), got {result}"


# ── Group 12: Reconciliation — all states account for every subscriber ────────

def test_reconciliation_all_states(db):
    """Seed: 20 total subscribers:
      8 active, 4 paused, 8 churned.
    Expected: active + paused + churned == 20."""
    now = datetime.now(timezone.utc)
    idx = 0

    # 8 active
    for _ in range(8):
        cid = f"ca{idx}"
        _customer(db, cid, datetime(2026, 1, 1, tzinfo=timezone.utc))
        _sub(db, f"sa{idx}", cid, Decimal("129"),
             converted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
             status="active")
        idx += 1

    # 4 paused
    for _ in range(4):
        cid = f"ca{idx}"
        _customer(db, cid, datetime(2026, 1, 1, tzinfo=timezone.utc))
        _sub(db, f"sa{idx}", cid, Decimal("129"),
             converted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
             status="paused",
             paused_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
        idx += 1

    # 8 churned
    for _ in range(8):
        cid = f"ca{idx}"
        _customer(db, cid, datetime(2026, 1, 1, tzinfo=timezone.utc))
        _sub(db, f"sa{idx}", cid, Decimal("129"),
             converted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
             churned_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
             churn_type="voluntary",
             status="churned")
        idx += 1

    db.commit()
    result = get_subscriber_state_counts(db)
    assert result["active"] == 8, f"Expected 8 active, got {result['active']}"
    assert result["paused"] == 4, f"Expected 4 paused, got {result['paused']}"
    assert result["churned"] == 8, f"Expected 8 churned, got {result['churned']}"
    assert result["active"] + result["paused"] + result["churned"] == result["total"], \
        f"State reconciliation FAIL: {result['active']}+{result['paused']}+{result['churned']} != {result['total']}"
    assert result["total"] == 20, f"Expected total=20, got {result['total']}"


# ── T30: Involuntary revenue churn — exact dollar assert (AR2d CLOSED) ────────

def test_rev_churn_involuntary_exact(db):
    """10 subs all at $129/mo active at Jan 1 2026.
    2 involuntary churns in Jan 2026, dunning_started_at >= 14 days before churned_at.
    Expected: rev_churn_involuntary(conn, 2026, 1) = (129+129)/(10×129) = 258/1290 = 0.20
    Function must return Decimal rounded to 2 decimal places."""
    # Create 10 subs active at Jan 1 2026
    for i in range(10):
        cid = f"cinvol{i}"
        _customer(db, cid, datetime(2025, 11, 1, tzinfo=timezone.utc))
        _sub(db, f"sinvol{i}", cid, Decimal("129"),
             converted_at=datetime(2025, 12, 1, tzinfo=timezone.utc))

    # 2 involuntary churns in Jan 2026; dunning_started_at = 2026-01-05, churned_at = 2026-01-20
    # Gap = 15 days >= 14 days → confirmed churn
    for i in range(2):
        db.execute(
            "update subscription_revenue "
            "set churned_at = %s, churn_type = 'involuntary', "
            "    churn_reason = 'payment_failed', dunning_started_at = %s "
            "where id = %s",
            (
                datetime(2026, 1, 20, tzinfo=timezone.utc),
                datetime(2026, 1, 5, tzinfo=timezone.utc),
                f"sinvol{i}",
            ),
        )

    result = rev_churn_involuntary(db, 2026, 1)
    # 258 / 1290 = 0.20 exactly
    assert result == Decimal("0.20"), f"Expected 0.20, got {result}"
    # Also verify it's rounded to exactly 2 decimal places
    assert result == result.quantize(Decimal("0.01")), \
        f"Result should be quantized to 2dp, got {result}"


# ── T31 + T32: Long-history cohort (Jan 2025) ─────────────────────────────────

def _seed_long_history_cohort_for_test(db):
    """Deterministic seed of the Jan 2025 40-customer cohort for T31/T32.
    Mirrors the logic in scripts/seed_demo.py seed_long_history_cohort()."""
    import hashlib
    rng = __import__("random").Random(20260101)

    from datetime import date as _date

    COHORT_START_DT = datetime(2025, 1, 1, tzinfo=timezone.utc)
    COHORT_SIZE = 40

    # Seed cost_inputs (needed for GP calc)
    db.execute(
        "insert into cost_inputs (sku, label, cogs_per_unit) values "
        "('HAIR-SERUM-50ML', 'Hair Serum 50ml', 3.50) on conflict (sku) do nothing"
    )
    db.execute(
        "insert into cost_settings (key, value, label) values "
        "('shipping_cost_per_order', 6.50, 'Shipping'), "
        "('payment_fee_pct', 0.029, 'Payment fee'), "
        "('return_processing_cost', 5.00, 'Return cost') "
        "on conflict (key) do nothing"
    )

    # M0 prices: 30 full-price at $149, 10 steep-discount at $89
    m0_prices = [Decimal("149")] * 30 + [Decimal("89")] * 10
    rng.shuffle(m0_prices)

    customers = []
    for i in range(COHORT_SIZE):
        cid = f"lh_c{i:03d}"
        day_of_jan = rng.randint(1, 28)
        first_order_dt = datetime(2025, 1, day_of_jan, 10, tzinfo=timezone.utc)
        email_hash = hashlib.sha256(f"lh_customer_{i}".encode()).hexdigest()
        db.execute(
            "insert into customers (id, email_hash, first_order_at, country) "
            "values (%s, %s, %s, 'US') on conflict (id) do nothing",
            (cid, email_hash, first_order_dt),
        )
        customers.append({"id": cid, "first_order_at": first_order_dt, "m0_price": m0_prices[i]})

    # Ad spend for Jan 2025: 31 days × $230/day
    for day in range(1, 32):
        spend_date = _date(2025, 1, day)
        db.execute(
            "insert into ad_spend (date, campaign_id, campaign_name, platform, spend) "
            "values (%s, %s, %s, %s, %s) on conflict do nothing",
            (spend_date, "cohort-meta-jan2025", "Meta Jan 2025 Cohort", "meta", Decimal("230.00")),
        )

    # Churn schedule (month offset → list of customer indices that churn)
    churn_at_m3 = sorted(rng.sample(range(COHORT_SIZE), 8))
    voluntary_m3 = churn_at_m3[:6]
    involuntary_m3 = churn_at_m3[6:]  # 2 involuntary
    churn_at_m6 = sorted(rng.sample([i for i in range(COHORT_SIZE) if i not in churn_at_m3], 5))
    churn_at_m9 = sorted(rng.sample([i for i in range(COHORT_SIZE) if i not in churn_at_m3 and i not in churn_at_m6], 3))
    churn_at_m12 = sorted(rng.sample([i for i in range(COHORT_SIZE) if i not in churn_at_m3 and i not in churn_at_m6 and i not in churn_at_m9], 2))
    winback_at_m10 = churn_at_m3[:3]  # 3 customers win back

    churned_set = set(churn_at_m3 + churn_at_m6 + churn_at_m9 + churn_at_m12)

    # Retention rates by month
    retention_schedule = {1: 0.70, 2: 0.65, 3: 0.58, 4: 0.52, 5: 0.48,
                          6: 0.42, 7: 0.40, 8: 0.38, 9: 0.36, 10: 0.34,
                          11: 0.33, 12: 0.32, 13: 0.31, 14: 0.31, 15: 0.30,
                          16: 0.30, 17: 0.30, 18: 0.30}

    order_idx = 0
    sub_idx = 0

    for i, cust in enumerate(customers):
        cid = cust["id"]
        first_order_dt = cust["first_order_at"]
        m0_price = cust["m0_price"]

        # Determine churn month for this customer
        if i in churn_at_m3:
            churn_offset = 3
        elif i in churn_at_m6:
            churn_offset = 6
        elif i in churn_at_m9:
            churn_offset = 9
        elif i in churn_at_m12:
            churn_offset = 12
        else:
            churn_offset = None  # still active through M18

        # M0 order (first order at serum price)
        m0_date = first_order_dt
        oid = f"lh_ord_{order_idx:05d}"
        order_idx += 1
        db.execute(
            "insert into orders "
            "(id, customer_id, created_at, total, refunded, currency, "
            " is_new_customer, is_subscription_order, discount_amount, line_items) "
            "values (%s, %s, %s, %s, %s, 'USD', true, false, %s, %s::jsonb) "
            "on conflict (id) do nothing",
            (
                oid, cid, m0_date, m0_price, Decimal("0"),
                Decimal("149") - m0_price if m0_price < Decimal("149") else Decimal("0"),
                f'[{{"sku":"HAIR-SERUM-50ML","quantity":1,"unit_price":{float(m0_price)}}}]',
            ),
        )

        # Subscription (converted_at = first_order_at for monthly sub)
        sid = f"lh_sub_{sub_idx:05d}"
        sub_idx += 1
        churned_at_dt = (first_order_dt + timedelta(days=30 * churn_offset)) if churn_offset else None
        sub_status = "churned" if churn_offset else "active"

        # Involuntary churn for the 2 involuntary customers
        is_involuntary = (i in involuntary_m3)
        dunning_started = (churned_at_dt - timedelta(days=15)) if is_involuntary and churned_at_dt else None

        db.execute(
            "insert into subscription_revenue "
            "(id, customer_id, monthly_amount, converted_at, churned_at, "
            " sub_type, cash_collected, churn_type, churn_reason, dunning_started_at, status) "
            "values (%s, %s, %s, %s, %s, 'monthly', %s, %s, %s, %s, %s) "
            "on conflict (id) do nothing",
            (
                sid, cid, Decimal("129"), first_order_dt, churned_at_dt,
                Decimal("129"),
                "involuntary" if is_involuntary else ("voluntary" if churn_offset else None),
                "payment_failed" if is_involuntary else ("cancelled" if churn_offset else None),
                dunning_started,
                sub_status,
            ),
        )

        # M1–M18 subscription rebill orders (if customer is retained that month)
        active_through = churn_offset - 1 if churn_offset else 18
        for month_offset in range(1, active_through + 1):
            # Determine if retained based on retention schedule
            retain_prob = retention_schedule.get(month_offset, 0.30)
            # Use deterministic: retain if rng.random() < retain_prob
            if rng.random() < retain_prob:
                rebill_date = first_order_dt + timedelta(days=30 * month_offset)
                if rebill_date > datetime(2026, 8, 24, tzinfo=timezone.utc):
                    break
                oid = f"lh_ord_{order_idx:05d}"
                order_idx += 1
                db.execute(
                    "insert into orders "
                    "(id, customer_id, created_at, total, refunded, currency, "
                    " is_new_customer, is_subscription_order, discount_amount, line_items) "
                    "values (%s, %s, %s, %s, %s, 'USD', false, true, %s, %s::jsonb) "
                    "on conflict (id) do nothing",
                    (
                        oid, cid, rebill_date, Decimal("129"), Decimal("0"), Decimal("0"),
                        f'[{{"sku":"HAIR-SERUM-50ML","quantity":1,"unit_price":129.0}}]',
                    ),
                )

        # Win-back customers at M10
        if i in winback_at_m10 and churn_offset:
            wb_date = first_order_dt + timedelta(days=30 * 10)
            if wb_date <= datetime(2026, 8, 24, tzinfo=timezone.utc):
                wb_sid = f"lh_sub_wb_{i:03d}"
                db.execute(
                    "insert into subscription_revenue "
                    "(id, customer_id, monthly_amount, converted_at, churned_at, "
                    " sub_type, cash_collected, status) "
                    "values (%s, %s, %s, %s, null, 'monthly', %s, 'active') "
                    "on conflict (id) do nothing",
                    (wb_sid, cid, Decimal("129"), wb_date, Decimal("129")),
                )
                # subscription_event for win-back
                db.execute(
                    "insert into subscription_events "
                    "(subscription_id, customer_id, event_type, event_date, mrr_delta) "
                    "values (%s, %s, 'winback', %s, %s)",
                    (wb_sid, cid, wb_date.date(), Decimal("129")),
                )
                # Win-back order
                oid = f"lh_ord_{order_idx:05d}"
                order_idx += 1
                db.execute(
                    "insert into orders "
                    "(id, customer_id, created_at, total, refunded, currency, "
                    " is_new_customer, is_subscription_order, discount_amount, line_items) "
                    "values (%s, %s, %s, %s, %s, 'USD', false, true, %s, %s::jsonb) "
                    "on conflict (id) do nothing",
                    (
                        oid, cid, wb_date, Decimal("129"), Decimal("0"), Decimal("0"),
                        f'[{{"sku":"HAIR-SERUM-50ML","quantity":1,"unit_price":129.0}}]',
                    ),
                )
                # Update winback_count
                db.execute(
                    "update customers set winback_count = 1 where id = %s", (cid,)
                )

    db.commit()


def test_cohort_ltv_12m_returns_real_value(db):
    """cohort_ltv_12m() must return an actual dollar figure for the Jan 2025 cohort.
    Known: 40 customers, M0 revenue = 30×$149 + 10×$89 = $5360.
    M1-M11: subscription rebills at $129 (decaying retention).
    LTV per customer should be roughly $500–$2000."""
    _seed_long_history_cohort_for_test(db)

    results = cohort_ltv_12m(db)
    jan_2025 = next(
        (r for r in results if r.get("cohort_label", "").startswith("2025-01")), None
    )
    assert jan_2025 is not None, \
        f"Jan 2025 cohort not found in cohort_ltv_12m results. Got: {[r['cohort_label'] for r in results]}"
    assert jan_2025["cohort_size"] == 40, \
        f"Expected cohort_size=40, got {jan_2025['cohort_size']}"
    ltv = jan_2025["ltv"]
    assert ltv is not None, "LTV should not be None"
    assert Decimal("200") < ltv < Decimal("2000"), \
        f"LTV {ltv} outside expected range $200–$2000"
    print(f"\nJan 2025 cohort LTV (12m): ${ltv:.2f}")


def test_payback_timing_returns_real_value(db):
    """payback_timing() must identify a payback month for Jan 2025 cohort.
    Known: CAC = $7130 / 40 = $178.25 (31 days × $230).
    M0 GP per customer ≈ (avg price - COGS - shipping - fees) / 40, should cross CAC quickly.
    Payback_month should be 0, 1, or 2."""
    _seed_long_history_cohort_for_test(db)

    results = payback_timing(db)
    jan_2025 = next(
        (r for r in results if r.get("cohort_label", "").startswith("2025-01")), None
    )
    assert jan_2025 is not None, \
        f"Jan 2025 cohort not found in payback_timing results. Got: {[r['cohort_label'] for r in results]}"
    assert jan_2025["payback_month"] is not None, \
        "Payback month should not be None for Jan 2025 cohort (CAC ~$178, M0 GP ~$100+)"
    assert jan_2025["payback_month"] <= 3, \
        f"Payback month {jan_2025['payback_month']} > 3 (unexpected given CAC ~$178)"
    cac = jan_2025["cac"]
    assert cac is not None, "CAC should be computed for Jan 2025"
    print(f"\nJan 2025 cohort payback month: M{jan_2025['payback_month']}, CAC: ${cac:.2f}")
