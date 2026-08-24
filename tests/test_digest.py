"""Phase C: digest tests using Densologie schema."""

import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from app_dashboard.digest import (
    DIGEST_SOURCE,
    collect_digest,
    render_digest,
    send_weekly_digest,
    should_send,
)

NOW = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)


def _settings(webhook="http://hook", serum_sku="HAIR-SERUM-50ML"):
    return SimpleNamespace(
        slack_webhook_url=webhook,
        public_base_url="https://dash.example.com",
        app_name="Densologie",
        dashboard_name="Densologie Scoreboard",
        serum_sku=serum_sku,
    )


def _capture():
    sent = []

    def post(url, json):
        sent.append(json)
        return SimpleNamespace(status_code=200)

    return sent, post


def _seed(db):
    """Seed minimal Densologie data for the last 7 days."""
    # Customer
    db.execute(
        "insert into customers (id, email_hash, first_order_at) values "
        "('c1', 'aaa', %s), ('c2', 'bbb', %s)",
        (NOW - timedelta(days=5), NOW - timedelta(days=3)),
    )
    # Orders (both new customers, in the last 7 days)
    db.execute(
        "insert into orders (id, customer_id, total, refunded, is_new_customer, "
        "created_at, line_items) values "
        "('o1','c1',149.00,0,true,%s,'[{\"sku\":\"HAIR-SERUM-50ML\",\"quantity\":1}]'::jsonb),"
        "('o2','c2',228.00,0,true,%s,'[{\"sku\":\"BUNDLE\",\"quantity\":1}]'::jsonb)",
        (NOW - timedelta(days=5), NOW - timedelta(days=3)),
    )
    # Ad spend in the window
    db.execute(
        "insert into ad_spend (date, campaign_id, campaign_name, platform, spend) values "
        "(%s,'camp1','Test Camp','meta',150.00), (%s,'camp1','Test Camp','meta',100.00)",
        ((NOW - timedelta(days=5)).date(), (NOW - timedelta(days=3)).date()),
    )
    # Subscription
    db.execute(
        "insert into subscription_revenue (id, customer_id, monthly_amount, converted_at) "
        "values ('s1','c1',149.00,%s)",
        (NOW - timedelta(days=4),),
    )
    db.commit()


def test_collect_counts_revenue_and_customers(db):
    _seed(db)
    data = collect_digest(db, _settings(), now=NOW)
    assert data["revenue_7d"] == Decimal("377.00")  # 149 + 228
    assert data["new_customers"] == 2


def test_collect_blended_cac(db):
    _seed(db)
    data = collect_digest(db, _settings(), now=NOW)
    # spend = 250, new customers = 2 → CAC = 125
    assert data["blended_cac"] == Decimal("125.00")


def test_collect_mer(db):
    _seed(db)
    data = collect_digest(db, _settings(), now=NOW)
    # revenue 377 / spend 250 = 1.508
    assert data["mer"] is not None
    assert float(data["mer"]) == pytest.approx(1.508, rel=0.01)


def test_collect_subscription_share(db):
    _seed(db)
    data = collect_digest(db, _settings(), now=NOW)
    # 1 of 2 new customers converted → 50%
    assert data["subscription_share"] == Decimal("50.0")


def test_render_contains_key_numbers(db):
    _seed(db)
    text = render_digest(collect_digest(db, _settings(), now=NOW))
    assert "Revenue" in text
    assert "377" in text
    assert "New customers" in text
    assert "CAC" in text
    assert text.count("\n") < 5


def test_render_no_data_shows_dashes(db):
    data = {
        "revenue_7d": None, "new_customers": 0, "blended_cac": None,
        "mer": None, "subscription_share": None, "days_of_cover": None,
    }
    text = render_digest(data)
    assert "—" in text
    assert "n/a" in text  # days_of_cover


def test_render_cover_flag_below_60(db):
    data = {
        "revenue_7d": Decimal("1000"), "new_customers": 5,
        "blended_cac": None, "mer": None, "subscription_share": None,
        "days_of_cover": 30,
    }
    text = render_digest(data)
    assert ":rotating_light:" in text
    assert "30d" in text


def test_digest_will_not_fire_twice_in_a_week(db):
    _seed(db)
    sent, post = _capture()
    assert send_weekly_digest(db, _settings(), http_post=post, now=NOW) is True
    assert len(sent) == 1
    assert send_weekly_digest(db, _settings(), http_post=post, now=NOW) is False
    assert len(sent) == 1


def test_should_send_guards_replays_and_restarts():
    monday = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
    assert should_send(None, monday) is True
    assert should_send(monday - timedelta(minutes=5), monday) is False
    assert should_send(monday - timedelta(days=7), monday) is True


def test_no_webhook_is_a_noop(db):
    _seed(db)
    sent, post = _capture()
    assert send_weekly_digest(db, _settings(webhook=None), http_post=post, now=NOW) is False
    assert sent == []


# ── Days-of-cover red-flag: DB-backed (closes the Phase C inventory amendment) ─
#
# The red-flag path requires live DB data because days_of_cover() calls
# _utcnow() internally rather than accepting a `now` override. Orders must
# therefore be seeded at real wall-clock timestamps so they fall inside the
# 14-day trailing window.

def _seed_low_cover(db):
    """Seed 20 serum orders at daily cadence (real time) + 40 units on hand.

    Formula: units_on_hand=40, units_sold_14d≈15, daily_rate≈1.07 → ~37 days.
    37 < 60  →  red-flag threshold breached.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    db.execute(
        "insert into customers (id, email_hash, first_order_at) values ('lc', 'lch', %s)",
        (now - timedelta(days=20),),
    )
    for i in range(20):
        db.execute(
            "insert into orders (id, customer_id, total, refunded, is_new_customer, "
            "created_at, line_items) values (%s, 'lc', 149.00, 0, false, %s, "
            """'[{"sku":"HAIR-SERUM-50ML","quantity":1}]'::jsonb)""",
            (f"lco{i}", now - timedelta(days=i)),
        )
    db.execute(
        "insert into inventory_levels (sku, units_on_hand, updated_at) "
        "values ('HAIR-SERUM-50ML', 40, now())"
    )
    db.commit()


def test_collect_digest_red_flag_fires_when_cover_below_60(db):
    """collect_digest returns days_of_cover < 60 and render_digest includes :rotating_light:."""
    _seed_low_cover(db)
    data = collect_digest(db, _settings(), now=NOW)

    assert data["days_of_cover"] is not None, "days_of_cover must not be None with seeded data"
    assert data["days_of_cover"] < 60, (
        f"expected days_of_cover < 60, got {data['days_of_cover']}"
    )
    text = render_digest(data)
    assert ":rotating_light:" in text, "digest must include :rotating_light: when cover < 60"
    cover_str = f"{data['days_of_cover']}d"
    assert cover_str in text, f"digest must display cover value '{cover_str}'"
