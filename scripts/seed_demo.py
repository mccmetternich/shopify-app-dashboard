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
    for table in ("subscription_revenue", "orders", "ad_spend", "customers",
                  "inventory_levels"):
        conn.execute(f"truncate table {table} cascade")
    conn.commit()
    print("Tables truncated.")


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


def seed_subscriptions(conn, customers: list[dict]) -> None:
    """Seed subscriptions: ~60% of customers, realistic churn over 90 days."""
    subscribers = [c for c in customers if RNG.random() < 0.60]

    for i, customer in enumerate(subscribers):
        amount = RNG.choices(SUB_AMOUNTS, weights=[40, 40, 20])[0]
        # Subscription starts within 7 days of first order
        converted_at = customer["first_order_at"] + timedelta(
            days=RNG.randint(0, 7), hours=RNG.randint(0, 23)
        )

        # ~25% of subscribers churn within the 90-day window
        churned_at = None
        if RNG.random() < 0.25:
            # Churn happens 14–75 days after subscription start
            churn_offset = timedelta(days=RNG.randint(14, 75))
            churned_at = converted_at + churn_offset
            # Don't let churned_at exceed window end
            window_end = ts(BASE_DATE + timedelta(days=DAYS - 1), 23, 59)
            if churned_at > window_end:
                churned_at = None   # still active at window end

        sub_id = f"sub_{i:05d}"
        conn.execute(
            "insert into subscription_revenue "
            "(id, customer_id, monthly_amount, converted_at, churned_at) "
            "values (%s, %s, %s, %s, %s) "
            "on conflict (id) do nothing",
            (sub_id, customer["id"], amount, converted_at, churned_at),
        )
    conn.commit()


def seed_inventory(conn) -> None:
    """Seed inventory_levels with the serum SKU.

    The serum is the highest-revenue SKU and the one the Phase C days-of-cover
    tile will track. ~800 units on hand is a realistic starting point for a
    brand doing ~3 orders/day with a mix of SKUs.

    Formula for Phase C:
        days_of_cover = units_on_hand / (units_sold_last_14d / 14)
    Red flag threshold: < 60 days (reorder lead time + buffer).
    """
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

    seed_subscriptions(conn, customers)
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

    conn.close()
    print("\nSeed complete. Run check_invariants.py to verify.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
