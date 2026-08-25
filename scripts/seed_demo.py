"""Seed a database with 90 days of synthetic Densologie DTC data.

Inserts directly into the four Phase A tables (customers, orders, ad_spend,
subscription_revenue) so the dashboard can be exercised without a live
Shopify webhook or ad platform credentials.

Every customer name and email is invented. Emails are stored only as
SHA-256 hashes — the raw address never touches the database.

Usage:

    createdb densologie_demo
    DATABASE_URL=postgresql://localhost:5432/densologie_demo \\
    DASHBOARD_USERS=demo:demo-only-not-a-password \\
    PUBLIC_BASE_URL=http://localhost:8000 \\
    NO_SCHEDULER=1 \\
      uv run python scripts/seed_demo.py --yes

Appends to an already-seeded database unless --truncate is also passed.
"""

import argparse
import hashlib
import math
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlparse

import psycopg

# ── Constants ────────────────────────────────────────────────────────────────

# Fixed seed: two runs produce the same dashboard so screenshots remain valid.
RNG = random.Random(20260809)

DAYS = 90          # window of synthetic history
BASE_DATE = date(2026, 5, 25)   # first day of the seeded window

# Densologie SKU universe ────────────────────────────────────────────────────
SKUS = [
    {"sku": "HAIR-SERUM-50ML",  "title": "TRICHOGENESIS Hair Serum 50ml",    "price": Decimal("149.00")},
    {"sku": "DSL-CAPS-60",      "title": "TRICHOGENESIS Capsules 60ct",       "price": Decimal("99.00")},
    {"sku": "DSL-BUNDLE-SYS",   "title": "TRICHOGENESIS System Bundle",       "price": Decimal("228.00")},
    {"sku": "DSL-SERUM-60ML",   "title": "TRICHOGENESIS Hair Serum 60ml",     "price": Decimal("228.00")},
    {"sku": "DSL-BUNDLE-3MO",   "title": "TRICHOGENESIS 3-Month Supply",      "price": Decimal("594.00")},
]

# Subscription amounts (monthly billing; bundle subscribers get auto-enrolled)
SUB_AMOUNTS = [Decimal("99.00"), Decimal("149.00"), Decimal("228.00")]

# Ad campaigns ───────────────────────────────────────────────────────────────
CAMPAIGNS = [
    {"id": "meta_prospe_01",  "name": "Meta – Prospecting",   "platform": "meta",   "daily_base": Decimal("100.00")},
    {"id": "meta_retarg_01",  "name": "Meta – Retargeting",   "platform": "meta",   "daily_base": Decimal("80.00")},
    {"id": "google_brand_01", "name": "Google – Branded",     "platform": "google", "daily_base": Decimal("70.00")},
]

# UTM sources for organic / paid traffic
UTM_SOURCES = [
    None,                                          # organic (no UTM)
    {"utm_source": "meta",   "utm_medium": "paid_social", "utm_campaign": "prospe_01"},
    {"utm_source": "meta",   "utm_medium": "paid_social", "utm_campaign": "retarg_01"},
    {"utm_source": "google", "utm_medium": "cpc",         "utm_campaign": "brand_01"},
    {"utm_source": "email",  "utm_medium": "email",       "utm_campaign": "weekly_digest"},
]

COUNTRIES = ["US", "US", "US", "US", "CA", "GB", "AU"]  # weighted toward US

# Upsell type config
UPSELL_TYPES = [
    {"type": "priority_shipping", "accept_rate": 0.25, "amount": Decimal("12.00")},
    {"type": "upsell_t1",         "accept_rate": 0.15, "amount": Decimal("89.00")},
    {"type": "upsell_t2",         "accept_rate": 0.08, "amount": Decimal("189.00")},
    {"type": "upsell_t3",         "accept_rate": 0.03, "amount": Decimal("399.00")},
    {"type": "aftersell",         "accept_rate": 0.12, "amount": Decimal("49.00")},
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def fake_email(idx: int) -> str:
    domains = ["gmail.com", "yahoo.com", "icloud.com", "hotmail.com", "outlook.com"]
    return f"demo.customer.{idx:04d}@{RNG.choice(domains)}"


def poisson_count(lam: float) -> int:
    """Simple Poisson draw without scipy dependency."""
    L = math.exp(-lam)
    k, p = 0, 1.0
    while p > L:
        k += 1
        p *= RNG.random()
    return k - 1


def ts(d: date, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=timezone.utc)


def utm_json(utm: dict | None) -> str | None:
    """Convert a UTM dict to a valid JSON string, or return None for organic."""
    if utm is None:
        return None
    pairs = ", ".join(f'"{k}": "{v}"' for k, v in utm.items())
    return "{" + pairs + "}"


# ── Seed functions ────────────────────────────────────────────────────────────

def truncate_tables(conn):
    # Truncate in FK-safe order (children first)
    for table in (
        "upsell_events", "subscription_events",
        "subscription_revenue", "omnisend_sends", "ga4_funnel",
        "checkouts", "orders", "ad_spend", "customers",
        "inventory_levels", "meta_ad_stats",
        "cost_inputs", "cost_settings", "landing_page_type_map",
    ):
        conn.execute(f"truncate table {table} cascade")

    # Re-seed static lookup tables cleared by truncate
    _reseed_cost_inputs(conn)
    _reseed_landing_page_type_map(conn)
    conn.commit()
    print("Tables truncated.")


def _reseed_cost_inputs(conn):
    conn.execute("""
        insert into cost_inputs (sku, label, cogs_per_unit) values
          ('HAIR-SERUM-50ML', 'Hair Serum 50ml', 3.50),
          ('DSL-CAPS-90',     'Capsules 90-day', 5.00),
          ('DSL-BUNDLE',      'Serum + Capsules Bundle', 8.50),
          ('DSL-3MO-SUPPLY',  '3-Month Supply', 10.50)
        on conflict (sku) do nothing
    """)
    conn.execute("""
        insert into cost_settings (key, value, label) values
          ('shipping_cost_per_order', 6.50,  'Flat shipping cost per order'),
          ('payment_fee_pct',         0.029, 'Payment processor fee (decimal, e.g. 0.029 = 2.9%)'),
          ('return_processing_cost',  5.00,  'Flat cost per returned order')
        on conflict (key) do nothing
    """)


def _reseed_landing_page_type_map(conn):
    conn.execute("""
        insert into landing_page_type_map (url_prefix, page_type) values
          ('/products/', 'pdp'),
          ('/blogs/',    'listicle'),
          ('/pages/',    'lander'),
          ('/',          'lander'),
          ('/checkout',  'direct_checkout')
        on conflict (url_prefix) do nothing
    """)


def seed_customers(conn, count: int) -> list[dict]:
    """Create `count` synthetic customers and return their records."""
    customers = []
    for i in range(count):
        email = fake_email(i)
        cid = f"cust_{i:05d}"
        # Skew first orders toward earlier cohorts (natural accumulation)
        first_order_day = BASE_DATE + timedelta(
            days=int(RNG.betavariate(0.8, 3) * DAYS)
        )
        country = RNG.choice(COUNTRIES)
        rec = {
            "id": cid,
            "email_hash": sha256(email),
            "first_order_at": ts(first_order_day, RNG.randint(6, 22), RNG.randint(0, 59)),
            "country": country,
        }
        customers.append(rec)
        conn.execute(
            "insert into customers (id, email_hash, first_order_at, country) "
            "values (%(id)s, %(email_hash)s, %(first_order_at)s, %(country)s) "
            "on conflict (id) do nothing",
            rec,
        )
    conn.commit()
    return customers


def seed_orders(conn, customers: list[dict]) -> None:
    """Seed ~3 orders/day with realistic Densologie SKU distribution."""
    order_idx = 0
    # Build a set of customer IDs who have had at least one order (for new-flag tracking)
    seen_customers: set[str] = set()

    for day_offset in range(DAYS):
        d = BASE_DATE + timedelta(days=day_offset)
        n_orders = poisson_count(3.0)   # λ=3 → ~3 orders/day average
        for _ in range(n_orders):
            customer = RNG.choice(customers)
            sku_rec = RNG.choices(SKUS, weights=[35, 30, 20, 10, 5])[0]
            order_hour = RNG.randint(6, 23)
            order_minute = RNG.randint(0, 59)
            created_at = ts(d, order_hour, order_minute)

            is_new = customer["id"] not in seen_customers
            seen_customers.add(customer["id"])

            # ~2% refund rate
            refunded = sku_rec["price"] if RNG.random() < 0.02 else Decimal("0.00")

            utm = RNG.choices(UTM_SOURCES, weights=[40, 25, 20, 10, 5])[0]

            order_id = f"ord_{order_idx:06d}"
            order_idx += 1

            conn.execute(
                "insert into orders "
                "(id, customer_id, created_at, total, refunded, currency, "
                " is_new_customer, line_items, source_utm) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
                "on conflict (id) do nothing",
                (
                    order_id,
                    customer["id"],
                    created_at,
                    sku_rec["price"],
                    refunded,
                    "USD",
                    is_new,
                    f'[{{"sku":"{sku_rec["sku"]}","title":"{sku_rec["title"]}",'
                    f'"quantity":1,"unit_price":{float(sku_rec["price"])}}}]',
                    utm_json(utm),
                ),
            )
    conn.commit()


def seed_ad_spend(conn) -> None:
    """Insert daily ad spend for each campaign with realistic variance."""
    for day_offset in range(DAYS):
        d = BASE_DATE + timedelta(days=day_offset)
        is_weekend = d.weekday() >= 5
        for camp in CAMPAIGNS:
            # Weekend spend is 20% lower; add ±15% jitter
            base = camp["daily_base"] * (Decimal("0.80") if is_weekend else Decimal("1.00"))
            jitter = Decimal(str(round(RNG.uniform(0.85, 1.15), 4)))
            spend = round(base * jitter, 2)

            impressions = int(float(spend) * RNG.uniform(80, 130))
            clicks = int(impressions * RNG.uniform(0.01, 0.03))

            conn.execute(
                "insert into ad_spend "
                "(date, campaign_id, campaign_name, platform, spend, impressions, clicks) "
                "values (%s, %s, %s, %s, %s, %s, %s) "
                "on conflict (date, campaign_id) do nothing",
                (d, camp["id"], camp["name"], camp["platform"], spend, impressions, clicks),
            )
    conn.commit()


def seed_subscriptions(conn, customers: list[dict]) -> list[dict]:
    """Seed subscriptions: ~60% of customers, realistic churn over 90 days.

    Returns list of sub dicts for downstream seeding (events, is_subscription_order).
    """
    subscribers = [c for c in customers if RNG.random() < 0.60]

    # Sub type distribution: 50% monthly, 30% 3mo, 20% 6mo
    sub_type_choices = ["monthly", "3mo", "6mo"]
    sub_type_weights = [50, 30, 20]

    sub_records = []

    for i, customer in enumerate(subscribers):
        sub_type = RNG.choices(sub_type_choices, weights=sub_type_weights)[0]

        if sub_type == "monthly":
            monthly_amount = Decimal("129.00")
            cash_collected_initial = Decimal("129.00")
        elif sub_type == "3mo":
            monthly_amount = Decimal("109.00")
            cash_collected_initial = Decimal("327.00")
        else:  # 6mo
            monthly_amount = Decimal("99.00")
            cash_collected_initial = Decimal("594.00")

        # Spread subscriptions evenly so every window (7d/30d/90d) has meaningful coverage
        converted_at_offset = timedelta(
            days=RNG.randint(0, DAYS - 1),
            hours=RNG.randint(0, 23),
            minutes=RNG.randint(0, 59),
        )
        converted_at = ts(BASE_DATE, 0, 0) + converted_at_offset

        # ~25% of subscribers churn within the 90-day window
        churned_at = None
        churn_type = None
        churn_reason = None
        dunning_started_at = None

        if RNG.random() < 0.25:
            # Churn happens 14–75 days after subscription start
            churn_offset = timedelta(days=RNG.randint(14, 75))
            churned_at = converted_at + churn_offset
            # Don't let churned_at exceed window end
            window_end = ts(BASE_DATE + timedelta(days=DAYS - 1), 23, 59)
            if churned_at > window_end:
                churned_at = None   # still active at window end

        # Assign churn type only if actually churned
        if churned_at is not None:
            r = RNG.random()
            if r < 0.35:
                churn_type = "voluntary"
                churn_reason = "cancelled"
            elif r < 0.85:  # 50% of total = 0.35..0.85
                churn_type = "involuntary"
                churn_reason = "payment_failed"
                dunning_started_at = churned_at - timedelta(days=RNG.randint(15, 30))
            else:  # 15% of churned: was in dunning but dunning recent (keep as not-yet-churned edge case)
                churn_type = "involuntary"
                churn_reason = "payment_failed"
                dunning_started_at = churned_at - timedelta(days=RNG.randint(15, 30))

        sub_id = f"sub_{i:05d}"
        conn.execute(
            "insert into subscription_revenue "
            "(id, customer_id, monthly_amount, converted_at, churned_at, "
            " sub_type, cash_collected, churn_type, churn_reason, dunning_started_at) "
            "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "on conflict (id) do nothing",
            (sub_id, customer["id"], monthly_amount, converted_at, churned_at,
             sub_type, cash_collected_initial, churn_type, churn_reason, dunning_started_at),
        )
        sub_records.append({
            "id": sub_id,
            "customer_id": customer["id"],
            "monthly_amount": monthly_amount,
            "cash_collected": cash_collected_initial,
            "sub_type": sub_type,
            "converted_at": converted_at,
            "churned_at": churned_at,
            "churn_type": churn_type,
            "churn_reason": churn_reason,
            "dunning_started_at": dunning_started_at,
        })

    # Add 15-20 subs currently in dunning (no churned_at, dunning started within last 14 days)
    n_dunning = RNG.randint(15, 20)
    dunning_pool = [c for c in customers if not any(s["customer_id"] == c["id"] for s in sub_records)]
    RNG.shuffle(dunning_pool)
    for j, customer in enumerate(dunning_pool[:n_dunning]):
        converted_at = ts(BASE_DATE, 0, 0) + timedelta(days=RNG.randint(0, DAYS - 20))
        dunning_started = datetime.now(timezone.utc) - timedelta(days=RNG.randint(1, 13))
        sub_id = f"sub_dun_{j:04d}"
        conn.execute(
            "insert into subscription_revenue "
            "(id, customer_id, monthly_amount, converted_at, churned_at, "
            " sub_type, cash_collected, churn_type, churn_reason, dunning_started_at) "
            "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "on conflict (id) do nothing",
            (sub_id, customer["id"], Decimal("129.00"), converted_at, None,
             "monthly", Decimal("129.00"), "involuntary", "payment_failed", dunning_started),
        )
        sub_records.append({
            "id": sub_id,
            "customer_id": customer["id"],
            "monthly_amount": Decimal("129.00"),
            "cash_collected": Decimal("129.00"),
            "sub_type": "monthly",
            "converted_at": converted_at,
            "churned_at": None,
            "churn_type": "involuntary",
            "churn_reason": "payment_failed",
            "dunning_started_at": dunning_started,
        })

    conn.commit()
    return sub_records


def seed_acquisition_offers(conn, customers: list[dict]) -> None:
    """Set acquisition_offer on customers based on first-order discount pattern."""
    # Get first-order discount info per customer
    rows = conn.execute("""
        select o.customer_id,
               o.discount_amount,
               o.total,
               o.discount_code,
               o.source_utm->>'utm_source' as utm_source
        from orders o
        where o.created_at = (
            select min(o2.created_at) from orders o2 where o2.customer_id = o.customer_id
        )
    """).fetchall()

    for customer_id, discount_amount, total, discount_code, utm_source in rows:
        offer = "full-price"
        discount_amount = discount_amount or Decimal("0")
        total = total or Decimal("1")

        if utm_source == "reactivation":
            offer = "reactivation"
        elif discount_amount > 0 and total > 0 and (discount_amount / total) >= Decimal("0.30"):
            offer = "steep-intro-discount"
        elif discount_amount > 0:
            offer = "coupon-only"
        else:
            offer = "full-price"

        conn.execute(
            "update customers set acquisition_offer = %s where id = %s",
            (offer, customer_id),
        )
    conn.commit()


def seed_subscription_events(conn, sub_records: list[dict]) -> None:
    """Seed subscription lifecycle events."""
    now = datetime.now(timezone.utc)

    for sub in sub_records:
        converted_date = sub["converted_at"].date()

        # 1. 'new' event at conversion
        conn.execute(
            "insert into subscription_events "
            "(subscription_id, customer_id, event_type, event_date, mrr_delta, "
            " old_monthly_amount, new_monthly_amount) "
            "values (%s, %s, 'new', %s, %s, %s, %s)",
            (sub["id"], sub["customer_id"], converted_date,
             sub["monthly_amount"], Decimal("0"), sub["monthly_amount"]),
        )

        # 2. mrr_recognized events for prepaid terms
        if sub["sub_type"] in ("3mo", "6mo"):
            n_months = 3 if sub["sub_type"] == "3mo" else 6
            end_date = sub["churned_at"].date() if sub["churned_at"] else (now.date())
            for mo in range(n_months):
                rec_date = (sub["converted_at"] + timedelta(days=30 * mo)).date()
                if rec_date > end_date:
                    break
                conn.execute(
                    "insert into subscription_events "
                    "(subscription_id, customer_id, event_type, event_date, mrr_delta) "
                    "values (%s, %s, 'mrr_recognized', %s, %s)",
                    (sub["id"], sub["customer_id"], rec_date, sub["monthly_amount"]),
                )

        # 3. churn event for churned subs
        if sub["churned_at"] is not None:
            conn.execute(
                "insert into subscription_events "
                "(subscription_id, customer_id, event_type, event_date, mrr_delta, reason) "
                "values (%s, %s, 'churn', %s, %s, %s)",
                (sub["id"], sub["customer_id"], sub["churned_at"].date(),
                 -sub["monthly_amount"], sub["churn_reason"]),
            )

        # 4. dunning_start events for involuntary churn or active-dunning subs
        if sub["dunning_started_at"] is not None:
            conn.execute(
                "insert into subscription_events "
                "(subscription_id, customer_id, event_type, event_date) "
                "values (%s, %s, 'dunning_start', %s)",
                (sub["id"], sub["customer_id"], sub["dunning_started_at"].date()),
            )

    # 5. ~10 expansion events across active subs
    active_subs = [s for s in sub_records if s["churned_at"] is None and s["dunning_started_at"] is None]
    expansion_pool = RNG.sample(active_subs, min(10, len(active_subs)))
    for sub in expansion_pool:
        old_amount = sub["monthly_amount"]
        new_amount = old_amount + Decimal(str(RNG.choice([20, 30, 79])))
        event_date = (sub["converted_at"] + timedelta(days=RNG.randint(20, 60))).date()
        conn.execute(
            "insert into subscription_events "
            "(subscription_id, customer_id, event_type, event_date, mrr_delta, "
            " old_monthly_amount, new_monthly_amount) "
            "values (%s, %s, 'expansion', %s, %s, %s, %s)",
            (sub["id"], sub["customer_id"], event_date,
             new_amount - old_amount, old_amount, new_amount),
        )

    # 6. ~5 contraction events
    contraction_pool = RNG.sample(
        [s for s in active_subs if s not in expansion_pool],
        min(5, len(active_subs) - len(expansion_pool)),
    )
    for sub in contraction_pool:
        old_amount = sub["monthly_amount"]
        new_amount = max(Decimal("99"), old_amount - Decimal(str(RNG.choice([20, 30]))))
        event_date = (sub["converted_at"] + timedelta(days=RNG.randint(20, 60))).date()
        conn.execute(
            "insert into subscription_events "
            "(subscription_id, customer_id, event_type, event_date, mrr_delta, "
            " old_monthly_amount, new_monthly_amount) "
            "values (%s, %s, 'contraction', %s, %s, %s, %s)",
            (sub["id"], sub["customer_id"], event_date,
             new_amount - old_amount, old_amount, new_amount),
        )

    # 7. ~15% skip events for active subs
    skip_eligible = [s for s in active_subs]
    skip_count = max(1, int(len(skip_eligible) * 0.15))
    skip_pool = RNG.sample(skip_eligible, min(skip_count, len(skip_eligible)))
    for sub in skip_pool:
        event_date = (sub["converted_at"] + timedelta(days=RNG.randint(10, 50))).date()
        conn.execute(
            "insert into subscription_events "
            "(subscription_id, customer_id, event_type, event_date) "
            "values (%s, %s, 'skip', %s)",
            (sub["id"], sub["customer_id"], event_date),
        )

    conn.commit()


def seed_is_subscription_orders(conn, sub_records: list[dict]) -> None:
    """Mark 1-3 recent non-new-customer orders per subscriber as is_subscription_order=true.

    Never marks is_new_customer=true orders as subscription orders — the three revenue
    streams (new / sub-recurring / non-sub-repeat) are mutually exclusive per spec T16.
    """
    for sub in sub_records:
        # Only mark orders that are NOT the customer's first (is_new_customer=false)
        order_rows = conn.execute(
            "select id from orders where customer_id = %s "
            "and is_new_customer = false "
            "order by created_at desc limit 3",
            (sub["customer_id"],),
        ).fetchall()
        n = RNG.randint(1, min(3, len(order_rows))) if order_rows else 0
        for row in order_rows[:n]:
            conn.execute(
                "update orders set is_subscription_order = true where id = %s",
                (row[0],),
            )
    conn.commit()


def seed_upsell_events(conn) -> None:
    """Seed upsell events for each order with per-type acceptance rates."""
    order_ids = [r[0] for r in conn.execute("select id from orders").fetchall()]
    for oid in order_ids:
        for utype in UPSELL_TYPES:
            accepted = RNG.random() < utype["accept_rate"]
            amount = utype["amount"] if accepted else Decimal("0.00")
            conn.execute(
                "insert into upsell_events (order_id, upsell_type, accepted, amount) "
                "values (%s, %s, %s, %s)",
                (oid, utype["type"], accepted, amount),
            )
    conn.commit()


def seed_inventory(conn) -> None:
    """Seed inventory_levels with the serum SKU."""
    conn.execute(
        """
        insert into inventory_levels (sku, units_on_hand, updated_at)
        values (%s, %s, now())
        on conflict (sku) do update set
            units_on_hand = excluded.units_on_hand,
            updated_at    = excluded.updated_at
        """,
        ("HAIR-SERUM-50ML", 800),
    )
    conn.commit()


def seed_paused_subscribers(conn, rng) -> None:
    """Pick 12 existing active subscribers and simulate pause lifecycle.

    7 still paused, 3 reactivated, 2 paused-then-cancelled.
    Adds pause event for all 12; reactivate/churn events for the resolved ones.
    """
    now = datetime.now(timezone.utc)

    # Find 12 active subs (status='active', churned_at is null)
    active_rows = conn.execute(
        """
        select id, customer_id, monthly_amount
        from subscription_revenue
        where churned_at is null
          and status = 'active'
        order by id
        limit 12
        """
    ).fetchall()

    if len(active_rows) < 12:
        print(f"  Warning: only {len(active_rows)} active subs available for pause seeding.")

    for idx, (sub_id, cust_id, monthly_amount) in enumerate(active_rows[:12]):
        # Random paused_at in last 60 days
        paused_at = now - timedelta(days=rng.randint(1, 60), hours=rng.randint(0, 23))

        if idx < 7:
            # Still paused
            conn.execute(
                "update subscription_revenue set status='paused', paused_at=%s where id=%s",
                (paused_at, sub_id),
            )
        elif idx < 10:
            # Reactivated: paused then came back 30 days later
            reactivated_at = paused_at + timedelta(days=30)
            conn.execute(
                "update subscription_revenue set status='active', paused_at=%s, paused_outcome='reactivated' where id=%s",
                (paused_at, sub_id),
            )
            conn.execute(
                "insert into subscription_events "
                "(subscription_id, customer_id, event_type, event_date) "
                "values (%s, %s, 'reactivate', %s)",
                (sub_id, cust_id, reactivated_at.date()),
            )
        else:
            # Cancelled from pause: set churned 45 days after pause
            churned_at = paused_at + timedelta(days=45)
            if churned_at > now:
                churned_at = now - timedelta(hours=1)
            conn.execute(
                """
                update subscription_revenue
                set status='churned', churned_at=%s, churn_type='voluntary',
                    churn_reason='cancelled_from_pause', paused_at=%s, paused_outcome='cancelled'
                where id=%s
                """,
                (churned_at, paused_at, sub_id),
            )
            conn.execute(
                "insert into subscription_events "
                "(subscription_id, customer_id, event_type, event_date, mrr_delta) "
                "values (%s, %s, 'churn', %s, %s)",
                (sub_id, cust_id, churned_at.date(), -monthly_amount),
            )

        # Pause event for all 12
        conn.execute(
            "insert into subscription_events "
            "(subscription_id, customer_id, event_type, event_date) "
            "values (%s, %s, 'pause', %s)",
            (sub_id, cust_id, paused_at.date()),
        )

    conn.commit()
    print(f"  {len(active_rows[:12])} subscribers paused (7 still paused, 3 reactivated, 2 cancelled-from-pause).")


def seed_winback_customers(conn, rng) -> None:
    """Pick 10 churned customers and simulate win-back subscriptions.

    Creates a new subscription_revenue row per customer, marks winback_count,
    adds winback subscription_event, and creates 1-2 repeat orders.
    CRITICAL: is_new_customer is NEVER set to true — customer stays in original cohort.
    """
    now = datetime.now(timezone.utc)

    churned_rows = conn.execute(
        """
        select sr.id, sr.customer_id, sr.monthly_amount, sr.churned_at
        from subscription_revenue sr
        where sr.churned_at is not null
          and sr.churn_type in ('voluntary', 'involuntary')
          and sr.paused_outcome is null   -- not from a pause
        order by sr.id
        limit 10
        """
    ).fetchall()

    winback_count = 0
    for sub_id, cust_id, monthly_amount, churned_at in churned_rows:
        if churned_at is None:
            continue

        gap_days = rng.randint(60, 120)
        new_converted_at = churned_at + timedelta(days=gap_days, hours=rng.randint(0, 23))
        if new_converted_at > now:
            new_converted_at = now - timedelta(days=1)

        new_sub_id = f"sub_wb_{winback_count:04d}"

        # Create new subscription row for this customer
        conn.execute(
            """
            insert into subscription_revenue
            (id, customer_id, monthly_amount, converted_at, sub_type, cash_collected, status)
            values (%s, %s, %s, %s, 'monthly', %s, 'active')
            on conflict (id) do nothing
            """,
            (new_sub_id, cust_id, monthly_amount, new_converted_at, monthly_amount),
        )

        # Mark customer winback
        conn.execute(
            "update customers set winback_count=1, last_winback_at=%s where id=%s",
            (new_converted_at, cust_id),
        )

        # Winback subscription event
        conn.execute(
            "insert into subscription_events "
            "(subscription_id, customer_id, event_type, event_date, mrr_delta) "
            "values (%s, %s, 'winback', %s, %s)",
            (new_sub_id, cust_id, new_converted_at.date(), monthly_amount),
        )

        # Create 1-2 repeat orders for this customer — NEVER is_new_customer=true
        n_orders = rng.randint(1, 2)
        for k in range(n_orders):
            oid = f"ord_wb_{winback_count:04d}_{k}"
            order_offset = timedelta(days=rng.randint(0, max(1, (now - new_converted_at).days)))
            order_at = new_converted_at + order_offset
            if order_at > now:
                order_at = now - timedelta(hours=1)
            conn.execute(
                "insert into orders "
                "(id, customer_id, created_at, total, refunded, currency, "
                " is_new_customer, is_subscription_order, line_items) "
                "values (%s, %s, %s, %s, 0, 'USD', false, true, %s::jsonb) "
                "on conflict (id) do nothing",
                (
                    oid, cust_id, order_at, monthly_amount,
                    f'[{{"sku":"HAIR-SERUM-50ML","quantity":1,"unit_price":{float(monthly_amount)}}}]',
                ),
            )

        winback_count += 1

    conn.commit()
    print(f"  {winback_count} win-back customers seeded; orders attributed to original cohorts.")


def seed_ga4_funnel(conn) -> None:
    """Seed GA4 funnel data with landing_page_type breakdown.

    Truncates existing ga4_funnel rows and inserts 1350 new rows:
    5 sources × 90 days × 3 page types (pdp, listicle, lander).
    """
    conn.execute("truncate table ga4_funnel")

    funnel_sources = [
        ("meta", "paid"),
        ("google", "paid"),
        ("", ""),           # organic / direct
        ("email", "email"),
        ("reactivation", "email"),
    ]

    page_type_configs = {
        "pdp":      {"session_pct": 0.50, "atc_rate": 0.08,  "checkout_rate": 0.04,  "purchase_rate": 0.015},
        "listicle": {"session_pct": 0.30, "atc_rate": 0.04,  "checkout_rate": 0.02,  "purchase_rate": 0.008},
        "lander":   {"session_pct": 0.20, "atc_rate": 0.10,  "checkout_rate": 0.05,  "purchase_rate": 0.022},
    }

    for day_offset in range(DAYS):
        day = BASE_DATE + timedelta(days=day_offset)
        for utm_source, utm_medium in funnel_sources:
            base_sessions_total = int(RNG.uniform(30, 80))
            if utm_source == "reactivation":
                base_sessions_total = int(RNG.uniform(5, 20))

            for page_type, cfg in page_type_configs.items():
                sessions = max(1, int(base_sessions_total * cfg["session_pct"] * RNG.uniform(0.85, 1.15)))
                atc = max(0, int(sessions * cfg["atc_rate"] * RNG.uniform(0.8, 1.2)))
                bc = max(0, int(sessions * cfg["checkout_rate"] * RNG.uniform(0.8, 1.2)))
                purchases = max(0, int(sessions * cfg["purchase_rate"] * RNG.uniform(0.8, 1.2)))

                conn.execute(
                    "insert into ga4_funnel "
                    "(date, utm_source, utm_medium, sessions, add_to_carts, "
                    " begin_checkouts, purchases, landing_page_type) "
                    "values (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "on conflict do nothing",
                    (day, utm_source, utm_medium, sessions, atc, bc, purchases, page_type),
                )

    conn.commit()


# ── Long-history cohort seed ─────────────────────────────────────────────────

def seed_long_history_cohort(conn, rng) -> None:
    """Seed 40 customers in Jan 2025 with 18 months of orders, subscriptions, churn, and win-backs.

    Provides the long-dated history needed for cohort_ltv_12m() and payback_timing()
    to return real values. Uses rng for all random choices (deterministic with fixed seed).

    Prints: "Long-history cohort seeded: 40 customers, Jan 2025, 18 months of orders."
    """
    import hashlib
    from datetime import date as _date

    COHORT_SIZE = 40
    COHORT_BASE = datetime(2025, 1, 1, tzinfo=timezone.utc)

    # M0 prices: 30 full-price at $149, 10 steep-discount at $89
    m0_prices = [Decimal("149")] * 30 + [Decimal("89")] * 10
    rng.shuffle(m0_prices)

    customers = []
    for i in range(COHORT_SIZE):
        cid = f"lh_c{i:03d}"
        day_of_jan = rng.randint(1, 28)
        first_order_dt = datetime(2025, 1, day_of_jan, 10, tzinfo=timezone.utc)
        email_hash = hashlib.sha256(f"lh_customer_{i}".encode()).hexdigest()
        conn.execute(
            "insert into customers (id, email_hash, first_order_at, country) "
            "values (%s, %s, %s, 'US') on conflict (id) do nothing",
            (cid, email_hash, first_order_dt),
        )
        conn.execute(
            "update customers set acquisition_offer = %s where id = %s",
            ("full-price" if m0_prices[i] == Decimal("149") else "steep-intro-discount", cid),
        )
        customers.append({"id": cid, "first_order_at": first_order_dt, "m0_price": m0_prices[i]})

    # Ad spend for Jan 2025: 31 days × $230/day (~$7,130 total)
    for day in range(1, 32):
        spend_date = _date(2025, 1, day)
        conn.execute(
            "insert into ad_spend (date, campaign_id, campaign_name, platform, spend) "
            "values (%s, %s, %s, %s, %s) on conflict (date, campaign_id) do nothing",
            (spend_date, "cohort-meta-jan2025", "Meta Jan 2025 Cohort", "meta", Decimal("230.00")),
        )

    # Churn schedule (using deterministic rng)
    churn_at_m3 = sorted(rng.sample(range(COHORT_SIZE), 8))
    voluntary_m3 = churn_at_m3[:6]
    involuntary_m3 = churn_at_m3[6:]
    remaining = [i for i in range(COHORT_SIZE) if i not in churn_at_m3]
    churn_at_m6 = sorted(rng.sample(remaining, 5))
    remaining = [i for i in remaining if i not in churn_at_m6]
    churn_at_m9 = sorted(rng.sample(remaining, 3))
    remaining = [i for i in remaining if i not in churn_at_m9]
    churn_at_m12 = sorted(rng.sample(remaining, 2))
    winback_at_m10 = churn_at_m3[:3]

    retention_schedule = {1: 0.70, 2: 0.65, 3: 0.58, 4: 0.52, 5: 0.48,
                          6: 0.42, 7: 0.40, 8: 0.38, 9: 0.36, 10: 0.34,
                          11: 0.33, 12: 0.32, 13: 0.31, 14: 0.31, 15: 0.30,
                          16: 0.30, 17: 0.30, 18: 0.30}

    order_idx = 5000  # offset to avoid collisions with main seed
    sub_idx = 5000

    now = datetime.now(timezone.utc)

    for i, cust in enumerate(customers):
        cid = cust["id"]
        first_order_dt = cust["first_order_at"]
        m0_price = cust["m0_price"]

        if i in churn_at_m3:
            churn_offset = 3
        elif i in churn_at_m6:
            churn_offset = 6
        elif i in churn_at_m9:
            churn_offset = 9
        elif i in churn_at_m12:
            churn_offset = 12
        else:
            churn_offset = None

        # M0 order
        oid = f"lh_ord_{order_idx:05d}"
        order_idx += 1
        conn.execute(
            "insert into orders "
            "(id, customer_id, created_at, total, refunded, currency, "
            " is_new_customer, is_subscription_order, discount_amount, line_items) "
            "values (%s, %s, %s, %s, %s, 'USD', true, false, %s, %s::jsonb) "
            "on conflict (id) do nothing",
            (
                oid, cid, first_order_dt, m0_price, Decimal("0"),
                Decimal("149") - m0_price if m0_price < Decimal("149") else Decimal("0"),
                f'[{{"sku":"HAIR-SERUM-50ML","quantity":1,"unit_price":{float(m0_price)}}}]',
            ),
        )

        # Subscription
        sid = f"lh_sub_{sub_idx:05d}"
        sub_idx += 1
        churned_at_dt = (first_order_dt + timedelta(days=30 * churn_offset)) if churn_offset else None
        sub_status = "churned" if churn_offset else "active"
        is_involuntary = (i in involuntary_m3)
        dunning_started = (churned_at_dt - timedelta(days=15)) if is_involuntary and churned_at_dt else None

        conn.execute(
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

        # Subscription events
        conn.execute(
            "insert into subscription_events "
            "(subscription_id, customer_id, event_type, event_date, mrr_delta) "
            "values (%s, %s, 'new', %s, %s)",
            (sid, cid, first_order_dt.date(), Decimal("129")),
        )
        if churned_at_dt:
            conn.execute(
                "insert into subscription_events "
                "(subscription_id, customer_id, event_type, event_date, mrr_delta, reason) "
                "values (%s, %s, 'churn', %s, %s, %s)",
                (sid, cid, churned_at_dt.date(), Decimal("-129"),
                 "payment_failed" if is_involuntary else "cancelled"),
            )

        # M1-M18 subscription rebill orders
        active_through = churn_offset - 1 if churn_offset else 18
        for month_offset in range(1, active_through + 1):
            retain_prob = retention_schedule.get(month_offset, 0.30)
            if rng.random() < retain_prob:
                rebill_date = first_order_dt + timedelta(days=30 * month_offset)
                if rebill_date > now:
                    break
                oid = f"lh_ord_{order_idx:05d}"
                order_idx += 1
                conn.execute(
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
            if wb_date <= now:
                wb_sid = f"lh_sub_wb_{i:03d}"
                conn.execute(
                    "insert into subscription_revenue "
                    "(id, customer_id, monthly_amount, converted_at, churned_at, "
                    " sub_type, cash_collected, status) "
                    "values (%s, %s, %s, %s, null, 'monthly', %s, 'active') "
                    "on conflict (id) do nothing",
                    (wb_sid, cid, Decimal("129"), wb_date, Decimal("129")),
                )
                conn.execute(
                    "insert into subscription_events "
                    "(subscription_id, customer_id, event_type, event_date, mrr_delta) "
                    "values (%s, %s, 'winback', %s, %s)",
                    (wb_sid, cid, wb_date.date(), Decimal("129")),
                )
                oid = f"lh_ord_{order_idx:05d}"
                order_idx += 1
                conn.execute(
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
                conn.execute(
                    "update customers set winback_count = 1 where id = %s", (cid,)
                )

    conn.commit()
    print("Long-history cohort seeded: 40 customers, Jan 2025, 18 months of orders.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true",
                        help="Confirm you want to write to the database.")
    parser.add_argument("--truncate", action="store_true",
                        help="Truncate all seeded tables before inserting.")
    parser.add_argument("--customers", type=int, default=250,
                        help="Number of synthetic customers to create (default: 250).")
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 2

    db_name = urlparse(url).path.lstrip("/")
    print(f"Target database: {db_name}")
    if not args.yes:
        print("Pass --yes to confirm you want to write to this database.")
        return 1

    from app_dashboard.db import run_migrations
    conn = psycopg.connect(url)
    run_migrations(conn)

    if args.truncate:
        truncate_tables(conn)

    print(f"Seeding {args.customers} customers over {DAYS} days...")

    customers = seed_customers(conn, args.customers)
    print(f"  {len(customers)} customers inserted.")

    seed_orders(conn, customers)
    order_count = conn.execute("select count(*) from orders").fetchone()[0]
    print(f"  {order_count} orders inserted (~{order_count/DAYS:.1f}/day).")

    seed_ad_spend(conn)
    spend_rows = conn.execute("select count(*) from ad_spend").fetchone()[0]
    total_spend = conn.execute("select sum(spend) from ad_spend").fetchone()[0]
    print(f"  {spend_rows} ad_spend rows inserted (total spend ${total_spend:,.2f}).")

    sub_records = seed_subscriptions(conn, customers)
    sub_count = conn.execute("select count(*) from subscription_revenue").fetchone()[0]
    active_count = conn.execute(
        "select count(*) from subscription_revenue where churned_at is null"
    ).fetchone()[0]
    print(f"  {sub_count} subscriptions inserted ({active_count} active).")

    seed_inventory(conn)
    inv_row = conn.execute(
        "select sku, units_on_hand from inventory_levels where sku = 'HAIR-SERUM-50ML'"
    ).fetchone()
    print(f"  inventory_levels: {inv_row[0]} = {inv_row[1]} units on hand.")

    # ── Discount codes ─────────────────────────────────────────────────────────
    print("  Seeding discount codes on orders...")
    DISCOUNT_CODES = ["WELCOME10", "REACTIVATE15", "VIP20", None, None, None, None]
    order_ids = [r[0] for r in conn.execute("select id from orders").fetchall()]
    for oid in order_ids:
        code = RNG.choice(DISCOUNT_CODES)
        if code:
            discount = float(RNG.choice([10.0, 15.0, 22.90, 14.90]))
            conn.execute(
                "update orders set discount_code = %s, discount_amount = %s where id = %s",
                (code, discount, oid),
            )
    conn.commit()
    disc_count = conn.execute(
        "select count(*) from orders where discount_code is not null"
    ).fetchone()[0]
    print(f"  {disc_count} orders updated with discount codes.")

    # ── Acquisition offer tags ─────────────────────────────────────────────────
    print("  Setting acquisition_offer on customers...")
    seed_acquisition_offers(conn, customers)
    offer_counts = conn.execute("""
        select acquisition_offer, count(*)
        from customers
        where acquisition_offer is not null
        group by acquisition_offer
        order by acquisition_offer
    """).fetchall()
    for offer, cnt in offer_counts:
        print(f"    {offer}: {cnt}")

    # ── Subscription events ────────────────────────────────────────────────────
    print("  Seeding subscription events...")
    seed_subscription_events(conn, sub_records)
    ev_count = conn.execute("select count(*) from subscription_events").fetchone()[0]
    print(f"  {ev_count} subscription_events inserted.")

    # ── Mark subscription orders ───────────────────────────────────────────────
    print("  Marking subscription orders...")
    seed_is_subscription_orders(conn, sub_records)
    sub_order_count = conn.execute(
        "select count(*) from orders where is_subscription_order = true"
    ).fetchone()[0]
    print(f"  {sub_order_count} orders marked as subscription orders.")

    # ── Upsell events ──────────────────────────────────────────────────────────
    print("  Seeding upsell events...")
    seed_upsell_events(conn)
    upsell_count = conn.execute("select count(*) from upsell_events").fetchone()[0]
    accepted_count = conn.execute(
        "select count(*) from upsell_events where accepted = true"
    ).fetchone()[0]
    print(f"  {upsell_count} upsell_events inserted ({accepted_count} accepted).")

    # ── GA4 funnel data ────────────────────────────────────────────────────────
    print("  Seeding GA4 funnel data (with landing_page_type)...")
    seed_ga4_funnel(conn)
    funnel_rows = conn.execute("select count(*) from ga4_funnel").fetchone()[0]
    print(f"  {funnel_rows} ga4_funnel rows inserted.")

    # ── Abandoned checkouts ────────────────────────────────────────────────────
    print("  Seeding abandoned checkouts...")
    for i in range(120):
        created = ts(
            BASE_DATE + timedelta(days=int(RNG.uniform(0, DAYS - 1))),
            RNG.randint(0, 23), RNG.randint(0, 59)
        )
        abandoned = created + timedelta(hours=float(RNG.uniform(0.5, 4)))
        recovered = abandoned + timedelta(hours=float(RNG.uniform(0.5, 24))) if RNG.random() < 0.18 else None
        conn.execute(
            "insert into checkouts (id, created_at, abandoned_at, recovered_at, total) "
            "values (%s, %s, %s, %s, %s) on conflict do nothing",
            (f"chk{i:04d}", created, abandoned, recovered,
             float(RNG.choice([149.0, 228.0, 99.0]))),
        )
    conn.commit()
    checkout_count = conn.execute("select count(*) from checkouts").fetchone()[0]
    print(f"  {checkout_count} abandoned checkouts inserted.")

    # ── Omnisend data ──────────────────────────────────────────────────────────
    print("  Seeding Omnisend email metrics...")
    OMNISEND_FLOWS = [
        ("Welcome Series", ""),
        ("Abandoned Cart", ""),
        ("Post-Purchase", ""),
        ("Win-Back", ""),
        ("", "Aug Promo"),   # campaign (no flow name)
    ]
    for day_offset in range(DAYS):
        day = BASE_DATE + timedelta(days=day_offset)
        for flow_name, campaign_name in OMNISEND_FLOWS:
            # Vary campaign name by month
            camp = f"{campaign_name} {day_offset // 30 + 1}" if campaign_name else ""
            sends = int(RNG.uniform(20, 200) if flow_name == "Welcome Series" else RNG.uniform(5, 80))
            opens = int(sends * RNG.uniform(0.25, 0.45))
            clicks = int(opens * RNG.uniform(0.10, 0.25))
            rev = float(
                RNG.uniform(0, 150) if flow_name in ("Abandoned Cart", "Win-Back")
                else RNG.uniform(0, 80)
            )
            conn.execute(
                "insert into omnisend_sends (date, flow_name, campaign_name, sends, opens, clicks, attributed_revenue) "
                "values (%s, %s, %s, %s, %s, %s, %s) on conflict do nothing",
                (day, flow_name, camp, sends, opens, clicks, round(rev, 2)),
            )
    conn.commit()
    omni_count = conn.execute("select count(*) from omnisend_sends").fetchone()[0]
    print(f"  {omni_count} omnisend_sends rows inserted.")

    # ── Meta ad stats (campaign/adset/ad level) ────────────────────────────────
    print("  Seeding Meta ad stats...")
    META_CAMPAIGN = {"id": "camp_asc_01", "name": "DSL \u2013 Advantage+ Shopping"}
    META_ADSETS = [
        {"id": "adset_meredith", "name": "Meredith \u2013 Hair Loss Concern"},
        {"id": "adset_professional", "name": "Professional Women 35-55"},
        {"id": "adset_retarget", "name": "Retargeting \u2013 Site Visitors"},
    ]
    META_ADS = [
        {"id": "ad_001", "name": "Before/After \u2013 90 Day Results", "thumb": "https://placehold.co/60x60/222/fff?text=Ad1", "url": "https://www.facebook.com/adsmanager"},
        {"id": "ad_002", "name": "UGC \u2013 Meredith Testimonial 30s", "thumb": "https://placehold.co/60x60/333/fff?text=Ad2", "url": "https://www.facebook.com/adsmanager"},
        {"id": "ad_003", "name": "Clinical Study Stats \u2013 Static", "thumb": "https://placehold.co/60x60/444/fff?text=Ad3", "url": "https://www.facebook.com/adsmanager"},
        {"id": "ad_004", "name": "Lifestyle \u2013 Morning Routine", "thumb": "https://placehold.co/60x60/555/fff?text=Ad4", "url": "https://www.facebook.com/adsmanager"},
        {"id": "ad_005", "name": "Product Explainer \u2013 Serum 15s", "thumb": "https://placehold.co/60x60/666/fff?text=Ad5", "url": "https://www.facebook.com/adsmanager"},
    ]

    for day_offset in range(DAYS):
        day = BASE_DATE + timedelta(days=day_offset)
        is_weekend = (BASE_DATE + timedelta(days=day_offset)).weekday() >= 5
        for adset in META_ADSETS:
            for ad in META_ADS:
                base_spend = Decimal(str(round(RNG.uniform(8, 35) * (0.8 if is_weekend else 1.0), 2)))
                impr = int(float(base_spend) * RNG.uniform(200, 500))
                clicks = int(impr * RNG.uniform(0.01, 0.04))
                purchases = int(clicks * RNG.uniform(0.01, 0.05))
                pv = Decimal(str(round(purchases * float(RNG.choice([149.0, 228.0, 99.0])), 2)))
                conn.execute(
                    "insert into meta_ad_stats "
                    "(date, campaign_id, campaign_name, adset_id, adset_name, ad_id, ad_name, "
                    "spend, impressions, clicks, purchases, purchase_value, thumbnail_url, ads_manager_url) "
                    "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict do nothing",
                    (day, META_CAMPAIGN["id"], META_CAMPAIGN["name"],
                     adset["id"], adset["name"], ad["id"], ad["name"],
                     base_spend, impr, clicks, purchases, pv, ad["thumb"], ad["url"]),
                )
    conn.commit()
    meta_rows = conn.execute("select count(*) from meta_ad_stats").fetchone()[0]
    print(f"  {meta_rows} meta_ad_stats rows inserted.")

    # ── Pause lifecycle ────────────────────────────────────────────────────────
    print("  Seeding paused subscribers...")
    seed_paused_subscribers(conn, RNG)

    # ── Win-back / reactivation ────────────────────────────────────────────────
    print("  Seeding win-back customers...")
    seed_winback_customers(conn, RNG)

    # ── Long-history cohort (Jan 2025 — 19 months backdated) ──────────────────
    print("  Seeding long-history cohort (40 customers, Jan 2025, 18 months)...")
    seed_long_history_cohort(conn, RNG)

    conn.close()
    print("\nSeed complete. Run check_invariants.py to verify.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
