"""stats.py computes the numbers on the dashboard, so it gets direct coverage
rather than being exercised only through page renders against an empty DB.

Tests referencing old Partner-API schema (shops, subscriptions, transactions,
raw_app_events, app_events) are kept as documentation but marked skip.
New Densologie-specific tests are at the bottom of this file.
"""

import pytest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app_dashboard.stats import (
    COMPARED,
    collected_revenue,
    country_breakdown,
    days_of_cover,
    installed_at_time,
    overview_comparison,
    install_retention_cohorts,
    revenue_by_month,
    review_candidates,
    trial_watch,
    mrr_movements,
    mrr_trend,
    overview_stats,
    unit_economics,
)


# ── Old-schema helpers (shops / subscriptions / transactions) ────────────────
# These reference tables that no longer exist in the Densologie schema.

def _shop(db, gid, **kw):
    cols = {"shop_gid": gid, "install_state": "installed"}
    cols.update(kw)
    names = ", ".join(cols)
    holes = ", ".join(["%s"] * len(cols))
    db.execute(f"insert into shops ({names}) values ({holes})", list(cols.values()))


def _sub(db, sub_id, gid, monthly, converted_at, churned_at=None):
    db.execute(
        "insert into subscriptions (id, shop_gid, monthly_amount, converted_at, churned_at) "
        "values (%s, %s, %s, %s, %s)",
        (sub_id, gid, monthly, converted_at, churned_at),
    )


def _uninstall_event(db, gid, at, reason=None, description=None,
                     raw_type="RELATIONSHIP_UNINSTALLED"):
    event_id = f"e-{gid}-{at}"
    db.execute(
        "insert into raw_app_events (id, type, occurred_at, shop_gid, payload) "
        "values (%s, %s, %s, %s, '{}')",
        (event_id, raw_type, at, gid),
    )
    db.execute(
        "insert into app_events (platform_event_id, type, occurred_at, shop_gid, "
        "uninstall_reason, uninstall_description) values (%s, 'uninstalled', %s, %s, %s, %s)",
        (event_id, at, gid, reason, description),
    )


def _txn(db, id, at, gross, net, type="AppSubscriptionSale", shop_gid="s1"):
    db.execute(
        "insert into transactions (id, type, created_at, shop_gid, gross_amount, "
        "shopify_fee, net_amount, currency_code) "
        "values (%s, %s, %s, %s, %s, 0, %s, 'USD')",
        (id, type, at, shop_gid, gross, net),
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


# ── Old-schema tests (all skipped — Phase A deleted shops/subscriptions/etc.) ─

@pytest.mark.skip(reason="Phase A: shops/subscriptions tables removed")
def test_overview_arpu_and_churn(db):
    _shop(db, "s1")
    _shop(db, "s2")
    _shop(db, "s3", install_state="uninstalled")
    _sub(db, "c1", "s1", Decimal("19.00"), "2026-01-01Z")
    _sub(db, "c2", "s2", Decimal("15.83"), "2026-01-01Z")
    _uninstall_event(db, "s3", "2026-08-01T00:00:00Z")
    db.commit()

    stats = overview_stats(db)
    assert stats["installed"] == 2
    assert stats["active_mrr"] == Decimal("34.83")
    assert stats["paying"] == 2
    assert stats["arpu"] == Decimal("17.415")
    assert stats["churn_30d"] == 33.3


@pytest.mark.skip(reason="Phase A: shops/subscriptions tables removed")
def test_mrr_trend_counts_a_sub_only_while_it_lived(db):
    _shop(db, "s1")
    _sub(db, "c1", "s1", Decimal("19.00"), "2026-01-15Z", churned_at="2026-03-10Z")
    db.commit()
    by_label = {m["label"]: m["mrr"] for m in mrr_trend(db, months=12)}
    assert by_label["Feb 2026"] == Decimal("19.00")
    assert by_label["Mar 2026"] == Decimal("0")


@pytest.mark.skip(reason="Phase A: shops/subscriptions tables removed")
def test_mrr_movements_splits_new_churn_and_upgrades(db):
    _shop(db, "s1")
    _shop(db, "s2")
    _shop(db, "s3")
    _sub(db, "c1", "s1", Decimal("19.00"), "2026-02-10Z")
    _sub(db, "c2", "s2", Decimal("19.00"), "2026-01-10Z", churned_at="2026-03-05Z")
    _sub(db, "c3a", "s3", Decimal("19.00"), "2026-01-10Z", churned_at="2026-04-02Z")
    _sub(db, "c3b", "s3", Decimal("25.00"), "2026-04-02Z")
    db.commit()

    by_label = {m["label"]: m for m in mrr_movements(db, months=12)}
    assert by_label["Jan 2026"]["new"] == Decimal("38.00")
    assert by_label["Feb 2026"]["new"] == Decimal("19.00")
    assert by_label["Mar 2026"]["churned"] == Decimal("-19.00")
    assert by_label["Apr 2026"]["expansion"] == Decimal("6.00")
    assert by_label["Apr 2026"]["new"] == Decimal("0")
    assert by_label["Apr 2026"]["net"] == Decimal("6.00")


@pytest.mark.skip(reason="Phase A: shops/subscriptions tables removed")
def test_mrr_movements_calls_a_returning_payer_a_reactivation(db):
    _shop(db, "s1")
    _sub(db, "c1", "s1", Decimal("19.00"), "2026-01-10Z", churned_at="2026-02-05Z")
    _sub(db, "c2", "s1", Decimal("19.00"), "2026-05-10Z")
    db.commit()
    by_label = {m["label"]: m for m in mrr_movements(db, months=12)}
    assert by_label["May 2026"]["reactivation"] == Decimal("19.00")
    assert by_label["May 2026"]["new"] == Decimal("0")


@pytest.mark.skip(reason="Phase A: shops/subscriptions tables removed")
def test_mrr_movements_reconcile_with_the_trend_line(db):
    _shop(db, "s1")
    _shop(db, "s2")
    _shop(db, "s3")
    _sub(db, "c1", "s1", Decimal("19.00"), "2026-01-10Z")
    _sub(db, "c2", "s2", Decimal("19.00"), "2026-02-10Z", churned_at="2026-06-05Z")
    _sub(db, "c3a", "s3", Decimal("19.00"), "2026-03-10Z", churned_at="2026-04-02Z")
    _sub(db, "c3b", "s3", Decimal("15.83"), "2026-04-02Z")
    db.commit()

    trend = mrr_trend(db, months=12)
    moves = {m["label"]: m for m in mrr_movements(db, months=12)}
    for prev, curr in zip(trend, trend[1:]):
        assert moves[curr["label"]]["net"] == curr["mrr"] - prev["mrr"], curr["label"]


@pytest.mark.skip(reason="Phase A: shops table removed")
def test_country_breakdown_splits_live_from_all_time(db):
    _shop(db, "s1", country="US")
    _shop(db, "s2", country="US", install_state="uninstalled")
    _shop(db, "s3", country="GB")
    db.commit()
    rows = {r["country"]: r for r in country_breakdown(db)}
    assert rows["US"]["installed"] == 1 and rows["US"]["ever"] == 2
    assert rows["GB"]["installed"] == 1 and rows["GB"]["ever"] == 1


@pytest.mark.skip(reason="Phase A: shops table removed")
def test_country_breakdown_folds_the_tail_into_other(db):
    for i in range(12):
        _shop(db, f"s{i}", country=f"C{i:02d}")
    db.commit()
    rows = country_breakdown(db, top=10)
    assert len(rows) == 11
    assert rows[-1]["country"] == "Other (2)"
    assert rows[-1]["installed"] == 2


@pytest.mark.skip(reason="Phase A: shops/subscriptions tables removed")
def test_plan_mix_labels_intervals(db):
    _shop(db, "s1")
    _shop(db, "s2")
    for gid, amount, interval in (("c1", "19.00", "EVERY_30_DAYS"), ("c2", "190.00", "ANNUAL")):
        db.execute(
            "insert into charges (gid, amount, currency_code, subscription_id, plan_interval, "
            "plan_amount) values (%s, %s, 'USD', %s, %s, %s)",
            (gid, amount, gid, interval, amount),
        )
    _sub(db, "c1", "s1", Decimal("19.00"), "2026-01-01Z")
    _sub(db, "c2", "s2", Decimal("15.83"), "2026-01-01Z")
    db.commit()
    labels = {p["label"]: p for p in plan_mix(db)}
    assert labels["Monthly"]["count"] == 1
    assert labels["Annual"]["mrr"] == Decimal("15.83")


@pytest.mark.skip(reason="Phase A: uninstall_reasons removed (Partner API)")
def test_uninstall_reasons_group_across_languages(db):
    pass


@pytest.mark.skip(reason="Phase A: uninstall_reasons removed (Partner API)")
def test_reason_buckets_count_the_mandatory_era_only(db):
    pass


@pytest.mark.skip(reason="Phase A: uninstall_reasons removed (Partner API)")
def test_deactivations_are_left_out_of_the_reason_denominator(db):
    pass


@pytest.mark.skip(reason="Phase A: uninstall_reasons removed (Partner API)")
def test_multi_reason_uninstall_counts_in_every_bucket(db):
    pass


@pytest.mark.skip(reason="Phase A: shops/subscriptions tables removed")
def test_review_candidates_start_at_thirty_days(db):
    _shop(db, "s1", shop_name="Veteran", shop_domain="veteran.myshopify.com")
    _shop(db, "s2", shop_name="Fresh")
    _shop(db, "s3", shop_name="Gone", install_state="uninstalled")
    _sub(db, "c1", "s1", Decimal("19.00"), "2026-01-01Z")
    db.execute("update subscriptions set converted_at = now() - interval '30 days' where id='c1'")
    _sub(db, "c2", "s2", Decimal("19.00"), "2026-01-01Z")
    db.execute("update subscriptions set converted_at = now() - interval '29 days' where id='c2'")
    _sub(db, "c3", "s3", Decimal("19.00"), "2024-01-01Z")
    db.commit()
    rows = review_candidates(db)
    assert [r["shop"] for r in rows] == ["Veteran"]
    assert rows[0]["domain"] == "veteran.myshopify.com"


@pytest.mark.skip(reason="Phase A: shops/subscriptions tables removed")
def test_review_candidates_skip_merchants_who_already_reviewed(db):
    _shop(db, "s1", shop_name="Reviewed", owner_name="Ada", email="ada@ex.example",
          reviewed_at="2026-04-13")
    _shop(db, "s2", shop_name="Not yet", owner_name="Bo", email="bo@ex.example")
    for sub_id, gid in (("c1", "s1"), ("c2", "s2")):
        _sub(db, sub_id, gid, Decimal("19.00"), "2026-01-01Z")
    db.execute("update subscriptions set converted_at = now() - interval '90 days'")
    db.commit()
    rows = review_candidates(db)
    assert [r["shop"] for r in rows] == ["Not yet"]
    assert "email" not in rows[0] and "owner_name" not in rows[0]


@pytest.mark.skip(reason="Phase A: shops/subscriptions tables removed")
def test_review_candidates_exclude_churned_subscriptions(db):
    _shop(db, "s1", shop_name="Lapsed")
    _sub(db, "c1", "s1", Decimal("19.00"), "2026-01-01Z", churned_at="2026-05-01Z")
    db.commit()
    assert review_candidates(db) == []


@pytest.mark.skip(reason="Phase A: annual_upgrade_candidates removed (Partner API)")
def test_annual_candidates_are_monthly_plans_past_three_months(db):
    pass


@pytest.mark.skip(reason="Phase A: shops/subscriptions tables removed")
def test_trial_watch_is_recent_installs_with_no_subscription(db):
    _shop(db, "s1", shop_name="Silent New")
    _shop(db, "s2", shop_name="Paid New")
    _shop(db, "s3", shop_name="Silent Old")
    db.execute("update shops set installed_at = now() - interval '3 days' where shop_gid='s1'")
    db.execute("update shops set installed_at = now() - interval '2 days' where shop_gid='s2'")
    db.execute("update shops set installed_at = now() - interval '20 days' where shop_gid='s3'")
    _sub(db, "c2", "s2", Decimal("19.00"), "2026-08-01Z")
    db.commit()
    assert [r["shop"] for r in trial_watch(db)] == ["Silent New"]


def _install_event(db, gid, at, kind="installed"):
    event_id = f"i-{gid}-{at}-{kind}"
    db.execute(
        "insert into raw_app_events (id, type, occurred_at, shop_gid, payload) "
        "values (%s, 'RELATIONSHIP_INSTALLED', %s, %s, '{}')",
        (event_id, at, gid),
    )
    db.execute(
        "insert into app_events (platform_event_id, type, occurred_at, shop_gid) "
        "values (%s, %s, %s, %s)",
        (event_id, kind, at, gid),
    )


@pytest.mark.skip(reason="Phase A: shops/app_events/raw_app_events tables removed")
def test_install_retention_covers_everyone_who_ever_installed(db):
    _shop(db, "s1", installed_at="2026-01-05Z")
    _shop(db, "s2", install_state="uninstalled",
          installed_at="2026-01-06Z", uninstalled_at="2026-02-10Z")
    _shop(db, "s3", installed_at="2026-01-07Z", uninstalled_at="2026-02-11Z")
    for gid, at in (("s1", "2026-01-05Z"), ("s2", "2026-01-06Z"), ("s3", "2026-01-07Z")):
        _install_event(db, gid, at)
    db.commit()

    out = install_retention_cohorts(db)
    jan = [c for c in out["cohorts"] if c["label"] == "01/2026"][0]
    assert jan["size"] == 3
    assert sum(c["size"] for c in out["cohorts"]) == 3
    assert jan["cells"][0] == 100
    assert jan["cells"][1] == 67


@pytest.mark.skip(reason="Phase A: shops/app_events tables removed")
def test_install_retention_includes_a_shop_whose_first_event_was_a_reactivation(db):
    _shop(db, "s1", installed_at="2026-03-15Z")
    db.execute(
        "insert into app_events (platform_event_id, type, occurred_at, shop_gid) "
        "values ('re1', 'reinstalled', '2026-03-15Z', 's1')")
    db.commit()
    out = install_retention_cohorts(db)
    assert [c["label"] for c in out["cohorts"]] == ["03/2026"]
    assert out["cohorts"][0]["size"] == 1


@pytest.mark.skip(reason="Phase A: transactions table removed; collected_revenue now queries orders")
def test_collected_revenue_measures_the_fee_rather_than_assuming_a_rate(db):
    _txn(db, "t1", "2026-07-01Z", Decimal("19.00"), Decimal("18.45"))
    _txn(db, "t2", "2026-07-02Z", Decimal("19.00"), Decimal("18.07"))
    _txn(db, "t3", "2026-07-03Z", Decimal("19.00"), Decimal("17.88"))
    db.commit()

    money = collected_revenue(db)
    assert money["gross"] == Decimal("57.00")
    assert money["net"] == Decimal("54.40")
    assert money["taken"] == Decimal("2.60")
    assert money["count"] == 3
    assert money["refund_count"] == 0


@pytest.mark.skip(reason="Phase A: transactions table removed")
def test_collected_revenue_counts_refunds_and_nets_them_out(db):
    _txn(db, "t1", "2026-07-01Z", Decimal("19.00"), Decimal("18.45"))
    _txn(db, "t2", "2026-07-05Z", Decimal("-19.00"), Decimal("-19.00"),
         type="AppSaleAdjustment")
    _txn(db, "t3", "2026-07-06Z", Decimal("-19.00"), Decimal("-19.00"),
         type="AppSaleCredit")
    db.commit()

    money = collected_revenue(db)
    assert money["refund_count"] == 2
    assert money["refunded"] == Decimal("38.00")
    assert money["gross"] == Decimal("-19.00")


@pytest.mark.skip(reason="Phase A: transactions table removed; revenue_by_month now queries orders")
def test_revenue_by_month_keeps_empty_months(db):
    _txn(db, "t1", "2026-07-15Z", Decimal("19.00"), Decimal("18.45"))
    db.commit()
    months = revenue_by_month(db, months=12)
    assert len(months) == 12
    assert all(m["gross"] is not None for m in months)


@pytest.mark.skip(reason="Phase A: shops/subscriptions tables removed; unit_economics is a stub")
def test_ltv_is_arpu_over_churn(db):
    now = datetime.now(timezone.utc)
    _shop(db, "s1")
    _shop(db, "s2")
    _shop(db, "s3", install_state="uninstalled")
    _sub(db, "c1", "s1", Decimal("20.00"), now - timedelta(days=300))
    _sub(db, "c2", "s2", Decimal("20.00"), now - timedelta(days=300))
    _sub(db, "c3", "s3", Decimal("20.00"), now - timedelta(days=300),
         churned_at=now - timedelta(days=10))
    db.commit()

    out = unit_economics(db)
    assert out["subs_at_start"] == 3
    assert out["churned_in_window"] == 1
    assert out["monthly_churn_pct"] == 11.1
    assert round(out["ltv"]) == 180


@pytest.mark.skip(reason="Phase A: shops/subscriptions tables removed")
def test_ltv_is_none_rather_than_infinite_when_nobody_churned(db):
    _shop(db, "s1")
    _sub(db, "c1", "s1", Decimal("19.00"), datetime.now(timezone.utc) - timedelta(days=300))
    db.commit()
    out = unit_economics(db)
    assert out["ltv"] is None
    assert out["monthly_churn_pct"] == 0.0


@pytest.mark.skip(reason="Phase A: install_reconciliation removed (GA4/Partner API)")
def test_install_reconciliation_names_the_measurement_gap(db):
    pass


@pytest.mark.skip(reason="Phase A: install_reconciliation removed (GA4/Partner API)")
def test_install_reconciliation_survives_an_empty_partner_side(db):
    pass


@pytest.mark.skip(reason="Phase A: uninstall_verbatims removed (Partner API)")
def test_verbatims_group_under_the_first_reason_selected(db):
    pass


@pytest.mark.skip(reason="Phase A: uninstall_verbatims removed (Partner API)")
def test_verbatims_skip_empty_notes_and_deactivations(db):
    pass


@pytest.mark.skip(reason="Phase A: installed_at_time stub; app_events/raw_app_events removed")
def test_installed_at_time_replays_the_lifecycle(db):
    now = datetime.now(timezone.utc)
    _shop(db, "s1")
    _shop(db, "s2", install_state="uninstalled")
    _install_event(db, "s1", now - timedelta(days=90))
    _install_event(db, "s2", now - timedelta(days=90))
    _uninstall_event(db, "s2", now - timedelta(days=10))
    db.commit()

    assert installed_at_time(db, now - timedelta(days=30)) == 2
    assert installed_at_time(db, now) == 1
    assert installed_at_time(db, now - timedelta(days=200)) == 0


@pytest.mark.skip(reason="Phase A: shops/app_events tables removed")
def test_a_shop_that_came_back_counts_as_installed(db):
    now = datetime.now(timezone.utc)
    _shop(db, "s1")
    _install_event(db, "s1", now - timedelta(days=100))
    _uninstall_event(db, "s1", now - timedelta(days=60))
    _install_event(db, "s1", now - timedelta(days=40), kind="reinstalled")
    db.commit()
    assert installed_at_time(db, now - timedelta(days=50)) == 0
    assert installed_at_time(db, now) == 1


@pytest.mark.skip(reason="Phase A: shops/app_events removed; overview_comparison signature changed")
def test_windowed_counts_compare_to_the_window_before(db):
    now = datetime.now(timezone.utc)
    _shop(db, "s1")
    _shop(db, "s2")
    _shop(db, "s3")
    _install_event(db, "s1", now - timedelta(days=5))
    _install_event(db, "s2", now - timedelta(days=40))
    _install_event(db, "s3", now - timedelta(days=200))
    db.commit()

    current = overview_stats(db)
    assert current["installs_30d"] == 1
    comparison = overview_comparison(db, {**current, "net_30d": Decimal("0")})
    assert comparison["installs_30d"]["prior"] == 1
    assert comparison["installs_30d"]["change"] == 0


@pytest.mark.skip(reason="Phase A: shops/subscriptions removed; overview_comparison signature changed")
def test_point_in_time_figures_compare_to_their_own_past(db):
    now = datetime.now(timezone.utc)
    _shop(db, "s1")
    _shop(db, "s2")
    _sub(db, "c1", "s1", Decimal("19.00"), now - timedelta(days=90))
    _sub(db, "c2", "s2", Decimal("19.00"), now - timedelta(days=10))
    db.commit()

    current = overview_stats(db)
    comparison = overview_comparison(db, {**current, "net_30d": Decimal("0")})
    assert comparison["active_mrr"]["prior"] == Decimal("19.00")
    assert comparison["active_mrr"]["change"] == Decimal("19.00")
    assert comparison["paying"]["prior"] == 1
    assert comparison["paying"]["change"] == 1
    assert comparison["active_mrr"]["pct"] == 100.0


@pytest.mark.skip(reason="Phase A: shops/subscriptions removed; overview_comparison signature changed")
def test_no_percentage_from_a_zero_base(db):
    now = datetime.now(timezone.utc)
    _shop(db, "s1")
    _sub(db, "c1", "s1", Decimal("19.00"), now - timedelta(days=2))
    db.commit()
    current = overview_stats(db)
    comparison = overview_comparison(db, {**current, "net_30d": Decimal("0")})
    assert comparison["active_mrr"]["prior"] == 0
    assert comparison["active_mrr"]["pct"] is None


@pytest.mark.skip(reason="Phase A: overview_comparison signature changed; old COMPARED keys differ")
def test_every_compared_key_is_reported(db):
    current = overview_stats(db)
    comparison = overview_comparison(db, {**current, "net_30d": Decimal("0")})
    assert set(comparison) == set(COMPARED)


@pytest.mark.skip(reason="Phase A: shops table removed")
def test_the_other_country_row_is_flagged_not_sniffed(db):
    for i in range(12):
        _shop(db, f"s{i}", country=f"C{i:02d}")
    db.commit()
    rows = country_breakdown(db, top=10)
    assert rows[-1]["other"] is True
    assert all("other" not in r for r in rows[:-1])


@pytest.mark.skip(reason="Phase A: churn_rows removed (Partner API)")
def test_churn_rows_counts_every_real_uninstall_and_no_deactivations(db):
    pass


@pytest.mark.skip(reason="Phase A: churn_rows removed (Partner API)")
def test_churn_rows_flag_shops_that_paid(db):
    pass


@pytest.mark.skip(reason="Phase A: churn_rows removed (Partner API)")
def test_churn_rows_filter_on_whether_a_reason_was_given(db):
    pass


@pytest.mark.skip(reason="Phase A: churn_rows removed (Partner API)")
def test_churn_rows_measure_the_stay_that_ended_not_the_first_install(db):
    pass


@pytest.mark.skip(reason="Phase A: plan_mix removed (Partner API)")
def test_plan_mix_carries_the_raw_interval_for_its_link(db):
    pass


@pytest.mark.skip(reason="Phase A: churn_composition removed (Partner API)")
def test_churn_composition_separates_payers_from_tourists(db):
    pass


@pytest.mark.skip(reason="Phase A: time_to_uninstall removed (Partner API)")
def test_time_to_uninstall_median_and_buckets(db):
    pass


@pytest.mark.skip(reason="Phase A: churn_rows removed (Partner API)")
def test_churn_rows_filter_on_a_reason_bucket(db):
    pass


@pytest.mark.skip(reason="Phase A: churn_rows removed (Partner API)")
def test_a_multi_reason_uninstall_matches_either_bucket(db):
    pass


@pytest.mark.skip(reason="Phase A: churn_rows removed (Partner API)")
def test_churn_rows_take_a_window(db):
    pass


@pytest.mark.skip(reason="Phase A: churn_rows removed (Partner API)")
def test_a_window_and_a_bucket_apply_together(db):
    pass


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
