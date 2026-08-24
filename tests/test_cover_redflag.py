"""Red-flag path for the Days-of-Cover inventory tile — closes Phase C amendment.

Verifies two surfaces independently:
  1. digest.render_digest → :rotating_light: when days_of_cover < 60
  2. Overview HTML → 'card stat down' class + threshold text when days_of_cover < 60

Both tests use real DB data (the db fixture from conftest). days_of_cover()
calls _utcnow() internally, so orders are seeded at wall-clock timestamps that
fall within its 14-day trailing window.
"""

from datetime import datetime, timedelta, timezone
from html import unescape

import pytest
from fastapi.testclient import TestClient

from app_dashboard.auth import SESSION_COOKIE, issue_session
from app_dashboard.digest import collect_digest, render_digest
from app_dashboard.web import create_app

# Must match conftest.py's SESSION_SECRET env-var value.
SESSION_SECRET = "test-session-secret-not-the-default"


def _seed_low_cover(db):
    """Seed 20 serum orders (1/day, real wall-clock) plus 40 units on hand.

    Inventory arithmetic:
      units_on_hand = 40
      units_sold_last_14d ≈ 15  (i=0..14 fall within the 14-day window)
      daily_rate = 15 / 14 ≈ 1.07 units/day
      days_of_cover = int(40 / 1.07) = 37

    37 < 60  →  red-flag threshold breached.
    """
    from types import SimpleNamespace
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


def _settings():
    from types import SimpleNamespace
    return SimpleNamespace(
        slack_webhook_url="http://hook",
        public_base_url="https://dash.example.com",
        app_name="Densologie",
        dashboard_name="Densologie Scoreboard",
        serum_sku="HAIR-SERUM-50ML",
    )


def keep_open(conn):
    """Prevent the route from closing the shared test connection."""
    class NoClose:
        def __getattr__(self, name):
            return getattr(conn, name)
        def close(self):
            pass
    return NoClose()


# ── Test 1: digest red-flag ───────────────────────────────────────────────────

def test_digest_rotating_light_when_cover_below_60(db):
    """collect_digest returns days_of_cover < 60 and render_digest includes :rotating_light:.

    This is the DB-backed companion to the pure-Python test_render_cover_flag_below_60
    that already existed in test_digest.py. That test passed a literal dict; this test
    goes through the full collect_digest path with real seeded data so that the
    days_of_cover() query is exercised against live rows.
    """
    _seed_low_cover(db)
    data = collect_digest(db, _settings())

    cover = data["days_of_cover"]
    assert cover is not None, (
        "days_of_cover must not be None — 20 days of order history + inventory row are seeded"
    )
    assert cover < 60, (
        f"expected days_of_cover < 60 with 40 units on hand and ~1 unit/day rate, got {cover}"
    )

    text = render_digest(data)
    assert ":rotating_light:" in text, (
        f"digest must include :rotating_light: when days_of_cover={cover} < 60\n"
        f"Actual digest text:\n{text}"
    )
    assert f"{cover}d" in text, f"cover value '{cover}d' must appear in the digest"


# ── Test 2: Overview HTML red-flag ────────────────────────────────────────────

def test_overview_html_red_flag_when_cover_below_60(db):
    """Overview page renders 'card stat down' CSS class and threshold warning text.

    Checks the Jinja template path:
      {% if stats.days_of_cover < 60 %} down {% endif %}
      → class="card stat down"
      → <div class="delta bad">…Below 60-day threshold…</div>
    """
    _seed_low_cover(db)

    app = create_app(conn_factory=lambda: keep_open(db))
    c = TestClient(app)
    c.cookies.set(SESSION_COOKIE, issue_session(SESSION_SECRET, "ada@example.com"))
    r = c.get("/")

    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    body = unescape(r.text)
    assert "card stat down" in body, (
        "Overview tile must carry 'down' CSS class when days_of_cover < 60"
    )
    assert "Below 60-day threshold" in body, (
        "Threshold warning text must be visible in the tile"
    )
