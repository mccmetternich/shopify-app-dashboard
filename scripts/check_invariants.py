#!/usr/bin/env python3
"""Run the Densologie data invariants against a live database. Read-only.

`tests/test_invariants.py` asserts these against a seeded fixture on every
pytest run. This script runs the same checks against real data — the fixture
cannot expose schema drift or pipeline bugs that only appear at production
volume.

Usage:

    DATABASE_URL='postgres://...' uv run python scripts/check_invariants.py

Exits non-zero if any invariant fails, so it can gate a deploy or CI run.
"""

import os
import sys

import psycopg

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "", scope: int | None = None) -> None:
    """Report one invariant.

    `scope` is the number of rows examined. A check that finds no violations
    because it had nothing to look at (scope == 0) is not evidence of health.
    """
    label = "PASS" if ok else "FAIL"
    suffix = ""
    if scope is not None:
        if scope:
            suffix = f"  ({scope} rows in scope)"
        else:
            suffix = "  (0 rows in scope — proves nothing)"
    print(f"{label}  {name}{suffix}")
    if not ok:
        if detail:
            print(f"      {detail}")
        FAILURES.append(name)


def rows(conn, sql: str, params=()) -> list:
    return conn.execute(sql, params).fetchall()


def scalar(conn, sql: str, params=(), default=None):
    result = conn.execute(sql, params).fetchone()
    return result[0] if result else default


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    # TimeZone=UTC keeps date_trunc boundaries consistent with the dashboard.
    conn = psycopg.connect(url, autocommit=True, options="-c TimeZone=UTC")

    # ── Invariant 1 ──────────────────────────────────────────────────────────
    # Every order's refunded amount must not exceed its total.
    bad = rows(conn, "select id from orders where refunded > total")
    check("orders.refunded <= orders.total for every row",
          not bad, detail=f"violating order ids: {[r[0] for r in bad[:5]]}")

    # ── Invariant 2 ──────────────────────────────────────────────────────────
    # Every order must reference a known customer (FK constraint also enforces
    # this, but FK violations surface as insert errors; the invariant catches
    # silent gaps if the FK is ever deferred or disabled).
    bad = rows(conn, """
        select o.id from orders o
        where not exists (select 1 from customers c where c.id = o.customer_id)
    """)
    check("All orders.customer_id values exist in customers",
          not bad, detail=f"orphaned order ids: {[r[0] for r in bad[:5]]}")

    # ── Invariant 3 ──────────────────────────────────────────────────────────
    # ad_spend must have no duplicate (date, campaign_id) pairs. The composite
    # PK normally prevents this; the check catches rows inserted before the
    # constraint existed or via a deferred path.
    bad = rows(conn, """
        select date, campaign_id
        from ad_spend
        group by date, campaign_id
        having count(*) > 1
    """)
    check("ad_spend has no duplicate (date, campaign_id) pairs",
          not bad, detail=f"first duplicates: {bad[:3]}")

    # ── Invariant 4 ──────────────────────────────────────────────────────────
    # source_utm must be NULL (unknown) or a non-empty JSON object. An empty
    # object `{}` means the pipeline wrote something when it should have written
    # nothing, which silently breaks UTM-aware queries.
    bad = rows(conn, "select id from orders where source_utm = '{}'::jsonb")
    check("source_utm is NULL or non-empty (never empty object {})",
          not bad, detail=f"order ids with empty utm: {[r[0] for r in bad[:5]]}")

    # ── Invariant 5 ──────────────────────────────────────────────────────────
    # Active subscriptions (churned_at IS NULL) must have a positive amount.
    # A zero-amount live subscription makes the MRR tile and the subscriber
    # count disagree.
    bad = rows(conn, """
        select id from subscription_revenue
        where churned_at is null and monthly_amount <= 0
    """)
    check("Active subscription_revenue rows have monthly_amount > 0",
          not bad, detail=f"violating ids: {[r[0] for r in bad[:5]]}")

    # ── Invariant 6 ──────────────────────────────────────────────────────────
    # is_new_customer = true must appear on at most one order per customer —
    # the first one chronologically. Multiple "new" orders per customer means
    # the pipeline double-counted new-customer revenue.
    bad = rows(conn, """
        select customer_id
        from orders
        where is_new_customer = true
        group by customer_id
        having count(*) > 1
    """)
    check("is_new_customer = true appears on at most one order per customer",
          not bad, detail=f"customer ids with multiple new-flags: {[r[0] for r in bad[:5]]}")

    # ── Sanity: table population ──────────────────────────────────────────────
    for table in ("orders", "customers", "ad_spend", "subscription_revenue"):
        n = scalar(conn, f"select count(*) from {table}")
        check(f"Table '{table}' is not empty",
              n > 0, detail=f"found {n} rows", scope=n)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} invariant(s) FAILED: {', '.join(FAILURES)}")
        return 1
    print("All invariants hold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
