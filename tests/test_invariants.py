"""Densologie Scoreboard data invariants — test harness.

These run on every pytest against the seeded fixture in conftest.py. The
invariant logic mirrors scripts/check_invariants.py but runs against a local
in-process fixture rather than a live database. The pattern (check name, ok
flag, detail string) is preserved; the content is entirely new for Phase A.

See scripts/check_invariants.py for the full invariant definitions.
"""

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rows(db, sql):
    return db.execute(sql).fetchall()


def _one(db, sql, params=()):
    return db.execute(sql, params).fetchone()[0]


# ---------------------------------------------------------------------------
# Invariant 1 — orders.total >= orders.refunded
# ---------------------------------------------------------------------------

def test_order_total_ge_refunded(db):
    """Every order row must have total >= refunded. A refund greater than the
    original sale is a data error, not a business event."""
    bad = _rows(db, """
        select id, total, refunded from orders
        where total < refunded
    """)
    assert bad == [], f"orders with refunded > total: {bad}"


# ---------------------------------------------------------------------------
# Invariant 2 — orders.customer_id exists in customers
# ---------------------------------------------------------------------------

def test_order_customer_fk(db):
    """Every orders.customer_id must exist in customers.id.
    Orphaned customer IDs mean the ingest pipeline split a write."""
    bad = _rows(db, """
        select o.id, o.customer_id from orders o
        where not exists (select 1 from customers c where c.id = o.customer_id)
    """)
    assert bad == [], f"orders with unknown customer_id: {bad}"


# ---------------------------------------------------------------------------
# Invariant 3 — ad_spend has no duplicate (date, campaign_id)
# ---------------------------------------------------------------------------

def test_ad_spend_no_duplicate_pk(db):
    """The primary key (date, campaign_id) should prevent duplicates; assert
    the table is actually consistent even if the PK were somehow bypassed."""
    bad = _rows(db, """
        select date, campaign_id, count(*) from ad_spend
        group by date, campaign_id having count(*) > 1
    """)
    assert bad == [], f"duplicate (date, campaign_id) pairs in ad_spend: {bad}"


# ---------------------------------------------------------------------------
# Invariant 4 — numeric metrics are NULL not 0 when data is absent
# ---------------------------------------------------------------------------
# NOTE: This invariant is a policy rule enforced by the ingest layer, not a
# SQL invariant on the table. We document it here and test the seed data.
# Rule: source_utm is NULL (not '{}') when no UTM data is present.

def test_source_utm_null_not_empty_json(db):
    """source_utm must be NULL when no UTM data exists. An empty JSON object
    is indistinguishable from 'UTM data arrived and was empty', which would
    make attribution models over-count attributed sessions."""
    bad = _rows(db, """
        select id from orders
        where source_utm is not null
          and (source_utm::text = '{}' or source_utm::text = 'null')
    """)
    assert bad == [], f"orders with source_utm set to empty/null JSON: {bad}"


# ---------------------------------------------------------------------------
# Invariant 5 — subscription_revenue.monthly_amount > 0 for active subs
# ---------------------------------------------------------------------------

def test_active_subscriptions_have_positive_amount(db):
    """An active subscription (churned_at IS NULL) must have a positive
    monthly_amount. A zero or negative amount means a data error slipped
    through the ingest layer."""
    bad = _rows(db, """
        select id, monthly_amount from subscription_revenue
        where churned_at is null and monthly_amount <= 0
    """)
    assert bad == [], f"active subscriptions with non-positive monthly_amount: {bad}"


# ---------------------------------------------------------------------------
# Invariant 6 — is_new_customer = true only on first order per customer
# ---------------------------------------------------------------------------

def test_is_new_customer_only_on_first_order(db):
    """is_new_customer must be TRUE only on the very first order for a
    customer_id. A customer flagged as 'new' on their second or later order
    would double-count new customers in acquisition metrics."""
    bad = _rows(db, """
        -- Find customer_ids that appear more than once in orders AND have
        -- is_new_customer = true on a non-first order.
        with ordered as (
            select id, customer_id, created_at, is_new_customer,
                   row_number() over (
                       partition by customer_id
                       order by created_at
                   ) as rn
            from orders
        )
        select id, customer_id from ordered
        where is_new_customer = true and rn > 1
    """)
    assert bad == [], (
        f"orders flagged is_new_customer=true on a non-first order: {bad}"
    )


# ---------------------------------------------------------------------------
# Schema smoke: core tables accept writes (self-seeding — no seed_demo.py needed)
# ---------------------------------------------------------------------------

def test_orders_table_accepts_rows(db):
    """Schema is wired: orders can be inserted and read back."""
    db.execute(
        "insert into customers (id, email_hash, first_order_at, country) "
        "values ('c_inv', 'h_inv', now(), 'US')"
    )
    db.execute(
        "insert into orders (id, customer_id, created_at, total, refunded, currency, "
        "is_new_customer, line_items) "
        "values ('o_inv', 'c_inv', now(), 149, 0, 'USD', true, '[]')"
    )
    db.commit()
    assert _one(db, "select count(*) from orders where id = 'o_inv'") == 1


def test_customers_table_accepts_rows(db):
    db.execute(
        "insert into customers (id, email_hash, first_order_at, country) "
        "values ('c_inv2', 'h_inv2', now(), 'US')"
    )
    db.commit()
    assert _one(db, "select count(*) from customers where id = 'c_inv2'") == 1


def test_ad_spend_table_accepts_rows(db):
    db.execute(
        "insert into ad_spend (date, campaign_id, campaign_name, platform, spend) "
        "values ('2026-01-01', 'cmp1', 'Test', 'meta', 100.00)"
    )
    db.commit()
    assert _one(db, "select count(*) from ad_spend") == 1


def test_subscription_revenue_table_accepts_rows(db):
    db.execute(
        "insert into customers (id, email_hash, first_order_at, country) "
        "values ('c_sub', 'h_sub', now(), 'US') on conflict do nothing"
    )
    db.execute(
        "insert into subscription_revenue "
        "(id, customer_id, monthly_amount, converted_at, sub_type, cash_collected, status) "
        "values ('sr1', 'c_sub', 99.00, now(), 'monthly', 99.00, 'active')"
    )
    db.commit()
    assert _one(db, "select count(*) from subscription_revenue where id = 'sr1'") == 1
