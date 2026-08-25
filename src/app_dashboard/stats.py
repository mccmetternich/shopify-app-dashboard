"""Read-side aggregates for the Densologie dashboard pages.

All functions query the Phase A/B schema:
    orders, customers, ad_spend, subscription_revenue,
    inventory_levels, usage_events.

Null-not-zero rule: every function returns None when data is absent rather than
0. A zero CAC means you spent nothing and got customers; a None CAC means the
data is not available. They are different facts.

The old Shopify-app-dashboard stubs (installed, active_mrr, app_events, shops,
subscriptions, transactions) are removed. Their implementations live in the
Phase A git history if ever needed for reference.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import psycopg


# ── Module-level constants ───────────────────────────────────────────────────

MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

MOVEMENT_KINDS = ("new", "reactivation", "expansion", "contraction", "churned")

# Kept for import compatibility with web.py and test_stats.py
COMPARED = ("revenue", "new_customers", "blended_cac", "mer",
            "subscription_share", "aov", "days_of_cover")

# Kept from old stats.py — used in web.py import
PLAN_LABELS = {"monthly": "Monthly", "annual": "Annual"}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _month_index(dt) -> int:
    """Months since year 0, so month arithmetic is plain integer subtraction."""
    return dt.year * 12 + dt.month - 1


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Overview ─────────────────────────────────────────────────────────────────

def overview_stats(conn: psycopg.Connection, window_days: int = 7) -> dict:
    """Return a dict keyed by METRICS slugs for the Overview page.

    window_days: 7 or 30.

    All monetary values are Decimal. All counts are int. None means the figure
    cannot be computed (not that it is zero). The caller must never substitute
    zero for a None — that would misrepresent the state of the data.
    """
    now = _utcnow()
    window_start = now - timedelta(days=window_days)

    def scalar(sql, params=()):
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None

    # Revenue: sum of (total - refunded) for orders in the window
    revenue = scalar(
        """
        select coalesce(sum(total - refunded), null)
        from orders
        where created_at >= %s
        """,
        (window_start,),
    )
    # Coalesce to None explicitly: an empty table returns null from sum(),
    # which psycopg maps to Python None. That is the right value.

    # New customers in the window (distinct to guard against data issues)
    new_customers = scalar(
        """
        select count(distinct customer_id)
        from orders
        where is_new_customer = true
          and created_at >= %s
        """,
        (window_start,),
    )
    # Zero new customers is a valid state; return 0, not None
    if new_customers is None:
        new_customers = 0

    # Total ad spend in the window
    total_spend = scalar(
        """
        select coalesce(sum(spend), null)
        from ad_spend
        where date >= %s::date
        """,
        (window_start,),
    )

    # Blended CAC: spend / new customers. Null when no new customers.
    if total_spend is not None and new_customers and new_customers > 0:
        blended_cac = total_spend / Decimal(new_customers)
    elif total_spend is None:
        blended_cac = None  # no spend data
    else:
        blended_cac = None  # zero new customers — CAC is undefined

    # MER: revenue / spend. Null when spend is zero or absent.
    if revenue is not None and total_spend and total_spend > 0:
        mer = revenue / total_spend
    else:
        mer = None

    # Subscription share: what share of new customers started a subscription
    # in the window. Uses converted_at on subscription_revenue.
    subs_in_window = scalar(
        """
        select count(distinct customer_id)
        from subscription_revenue
        where converted_at >= %s
        """,
        (window_start,),
    ) or 0

    if new_customers and new_customers > 0:
        raw_share = Decimal("100") * Decimal(subs_in_window) / Decimal(new_customers)
        subscription_share = min(raw_share, Decimal("100"))  # cap at 100%
    else:
        subscription_share = None

    # AOV: revenue / order count. Null when no orders.
    order_count = scalar(
        "select count(*) from orders where created_at >= %s",
        (window_start,),
    ) or 0

    if revenue is not None and order_count > 0:
        aov = revenue / Decimal(order_count)
    else:
        aov = None

    return {
        "revenue": revenue,
        "new_customers": new_customers,
        "blended_cac": blended_cac,
        "mer": mer,
        "subscription_share": subscription_share,
        "aov": aov,
        "days_of_cover": None,  # computed separately via days_of_cover(); not window-based
    }


def overview_comparison(current: dict, prior: dict) -> dict:
    """Return the same keys as `current` with _delta and _delta_pct attached.

    prior = same-length window immediately before the current window.
    Null-not-zero: if either value is None, delta and delta_pct are also None.
    """
    out = {}
    for key in current:
        c_val = current[key]
        p_val = prior.get(key)
        if c_val is None or p_val is None:
            out[key] = {
                "current": c_val,
                "prior": p_val,
                "change": None,
                "pct": None,
            }
        else:
            try:
                change = Decimal(str(c_val)) - Decimal(str(p_val))
                pct = (
                    round(100 * float(change) / float(p_val), 1)
                    if p_val and float(p_val) != 0.0
                    else None
                )
                out[key] = {
                    "current": c_val,
                    "prior": p_val,
                    "change": change,
                    "pct": pct,
                }
            except (TypeError, ValueError):
                out[key] = {"current": c_val, "prior": p_val, "change": None, "pct": None}
    return out


# ── Revenue by month ──────────────────────────────────────────────────────────

def revenue_by_month(conn: psycopg.Connection, months: int = 12) -> list[dict]:
    """Collected revenue per calendar month, oldest first.

    Returns [{month: "2026-01", revenue: Decimal, orders: int}].
    """
    rows = conn.execute(
        """
        with bounds as (
            select generate_series(
                date_trunc('month', now()) - make_interval(months => %s - 1),
                date_trunc('month', now()),
                interval '1 month'
            ) as month_start
        )
        select to_char(b.month_start, 'YYYY-MM') as month,
               coalesce(sum(o.total - o.refunded), 0) as revenue,
               count(o.id) as orders
        from bounds b
        left join orders o
               on o.created_at >= b.month_start
              and o.created_at < b.month_start + interval '1 month'
        group by b.month_start, month
        order by b.month_start
        """,
        (months,),
    ).fetchall()
    return [
        {"month": month, "revenue": revenue, "orders": orders}
        for month, revenue, orders in rows
    ]


# ── Customer cohorts ──────────────────────────────────────────────────────────

def customer_cohorts(conn: psycopg.Connection) -> dict:
    """Primary cohort table: cumulative net revenue per customer, N months after first order.

    Returns:
        {
            "months": ["2026-05", "2026-06", ...],
            "max_age": 3,
            "rows": [
                {"cohort": "2026-05", "size": 42, "values": [Decimal("149.00"), Decimal("210.50"), ...]},
            ],
            "target_ltgp": 390.0,
        }

    Values are cumulative revenue per customer through month N (i.e. M0 is
    revenue in the first calendar month of the cohort, M1 is M0 + revenue in
    the next month, etc.). None means the cohort has not yet reached that age.
    """
    now = _utcnow()
    this_month = _month_index(now)

    # Pull all orders joined to their customer's first_order_at
    rows = conn.execute(
        """
        select c.id,
               date_trunc('month', c.first_order_at) as cohort_month,
               date_trunc('month', o.created_at) as order_month,
               (o.total - o.refunded) as net
        from orders o
        join customers c on c.id = o.customer_id
        order by cohort_month, c.id, order_month
        """
    ).fetchall()

    if not rows:
        return {"months": [], "max_age": 0, "rows": [], "target_ltgp": 390.0}

    # Group by cohort month
    from collections import defaultdict
    # cohort_data[cohort_mi][customer_id][order_mi] += net
    cohort_customers: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(Decimal)))

    for cust_id, cohort_month_dt, order_month_dt, net in rows:
        cohort_mi = _month_index(cohort_month_dt)
        order_mi = _month_index(order_month_dt)
        cohort_customers[cohort_mi][cust_id][order_mi] += (net or Decimal("0"))

    all_cohort_months = sorted(cohort_customers.keys())
    if not all_cohort_months:
        return {"months": [], "max_age": 0, "rows": [], "target_ltgp": 390.0}

    max_age = max(this_month - cm for cm in all_cohort_months)
    max_age = min(max_age, 23)  # cap at 24 months for display

    result_rows = []
    month_labels = []

    for cohort_mi in all_cohort_months:
        label = f"{MONTH_NAMES[cohort_mi % 12]} {cohort_mi // 12}"
        ym = f"{cohort_mi // 12}-{1 + (cohort_mi % 12):02d}"
        if ym not in month_labels:
            month_labels.append(ym)

        customers = cohort_customers[cohort_mi]
        cohort_size = len(customers)
        observable_months = this_month - cohort_mi

        values = []
        for offset in range(min(observable_months + 1, max_age + 1)):
            target_mi = cohort_mi + offset
            # Cumulative revenue per customer through this offset
            total_cumulative = Decimal("0")
            for cust_revenues in customers.values():
                # Sum all orders from cohort_mi through cohort_mi + offset
                for order_mi, net in cust_revenues.items():
                    if order_mi <= target_mi:
                        total_cumulative += net
            if cohort_size > 0:
                values.append(total_cumulative / Decimal(cohort_size))
            else:
                values.append(None)

        result_rows.append({
            "cohort": ym,
            "label": label,
            "size": cohort_size,
            "cells": values,
        })

    return {
        "months": month_labels,
        "max_age": max_age,
        "rows": result_rows,
        "target_ltgp": 390.0,
    }


# ── Subscription retention ────────────────────────────────────────────────────

def retention_cohorts(conn: psycopg.Connection, max_offset: int = 8) -> dict:
    """Monthly subscription cohorts: % of each converted_at-month cohort still
    active N months after converting. Used by the old /reports/retention page.

    Returns {"cohorts": [...], "max_offset": N} in the same shape as the old
    implementation, so the existing retention.html template keeps working.
    """
    rows = conn.execute(
        "select converted_at, churned_at from subscription_revenue where converted_at is not null"
    ).fetchall()
    now = _utcnow()
    this_month = now.year * 12 + (now.month - 1)

    cohorts: dict[int, list] = {}
    for converted_at, churned_at in rows:
        cm = converted_at.year * 12 + (converted_at.month - 1)
        churn_offset = None
        if churned_at is not None:
            churn_offset = (churned_at.year * 12 + churned_at.month - 1) - cm
        cohorts.setdefault(cm, []).append(churn_offset)

    out = []
    for cm in sorted(cohorts):
        subs = cohorts[cm]
        observable = min(this_month - cm, max_offset)
        cells = []
        for offset in range(0, observable + 1):
            active = sum(1 for c in subs if c is None or c > offset)
            cells.append(round(100 * active / len(subs)))
        out.append({
            "label": f"{1 + (cm % 12):02d}/{cm // 12}",
            "size": len(subs),
            "cells": cells,
        })
    return {"cohorts": out, "max_offset": max_offset}


def subscription_retention(conn: psycopg.Connection) -> dict:
    """Secondary cohort table: subscription retention by converted_at month.

    Returns the same structure as customer_cohorts but values are retention
    percentages (0-100). M0 is always 100 (everyone is active at month 0).

    Uses the same retention_cohorts() engine, reformatted for the cohorts page.
    """
    now = _utcnow()
    this_month = _month_index(now)

    rows = conn.execute(
        "select converted_at, churned_at from subscription_revenue where converted_at is not null"
    ).fetchall()

    if not rows:
        return {"months": [], "max_age": 0, "rows": [], "target_ltgp": None}

    cohorts: dict[int, list] = {}
    for converted_at, churned_at in rows:
        cm = _month_index(converted_at)
        churn_offset = None
        if churned_at is not None:
            churn_offset = _month_index(churned_at) - cm
        cohorts.setdefault(cm, []).append(churn_offset)

    all_cohort_months = sorted(cohorts.keys())
    max_age = max(this_month - cm for cm in all_cohort_months)
    max_age = min(max_age, 23)

    month_labels = []
    result_rows = []

    for cm in all_cohort_months:
        ym = f"{cm // 12}-{1 + (cm % 12):02d}"
        label = f"{MONTH_NAMES[cm % 12]} {cm // 12}"
        month_labels.append(ym)

        subs = cohorts[cm]
        cohort_size = len(subs)
        observable = min(this_month - cm, max_age)

        values = []
        for offset in range(0, observable + 1):
            active = sum(1 for c in subs if c is None or c > offset)
            pct = round(100 * active / cohort_size) if cohort_size else None
            values.append(pct)

        result_rows.append({
            "cohort": ym,
            "label": label,
            "size": cohort_size,
            "cells": values,
        })

    return {
        "months": month_labels,
        "max_age": max_age,
        "rows": result_rows,
        "target_ltgp": None,
    }


# ── Days of cover ─────────────────────────────────────────────────────────────

def days_of_cover(conn: psycopg.Connection, serum_sku: str) -> int | None:
    """Inventory cover for the serum SKU in days.

    Formula: units_on_hand / (units_sold_last_14_days / 14).

    Returns None when:
    - fewer than 14 days of orders exist in the database, OR
    - the SKU has no inventory record, OR
    - trailing unit sales are zero (would divide by zero).

    The SKU is matched against JSON line_items using the jsonb_array_elements
    operator, so any order containing that SKU in any line item position counts.
    """
    # Check whether we have at least 14 days of order data
    earliest = conn.execute(
        "select min(created_at) from orders"
    ).fetchone()[0]

    if earliest is None:
        return None

    now = _utcnow()
    data_age_days = (now - earliest).days
    if data_age_days < 14:
        return None

    # Units on hand for this SKU
    inv_row = conn.execute(
        "select units_on_hand from inventory_levels where sku = %s",
        (serum_sku,),
    ).fetchone()
    if inv_row is None:
        return None
    units_on_hand = inv_row[0]

    # Units sold in last 14 days, pulling from JSONB line_items
    # line_items is a JSON array like [{"sku": "...", "quantity": 1, ...}]
    cutoff = now - timedelta(days=14)
    units_sold_row = conn.execute(
        """
        select coalesce(sum((item->>'quantity')::int), 0)
        from orders o,
             jsonb_array_elements(o.line_items) as item
        where o.created_at >= %s
          and item->>'sku' = %s
        """,
        (cutoff, serum_sku),
    ).fetchone()

    units_sold_14d = int(units_sold_row[0]) if units_sold_row and units_sold_row[0] else 0
    if units_sold_14d == 0:
        return None

    daily_rate = units_sold_14d / 14.0
    return int(units_on_hand / daily_rate)


# ── Survey tally ──────────────────────────────────────────────────────────────

def survey_tally(conn: psycopg.Connection, window_days: int = 90) -> list[dict]:
    """Tally survey_response events grouped by 'heard_via' property.

    Reads usage_events where event_type = 'survey_response' and groups by
    properties->>'heard_via'. Returns a list of:
        [{"heard_via": str, "count": int, "pct": float}, ...]
    sorted by count descending.

    window_days: look back this many days. 0 means all time.
    Responses with a missing or null 'heard_via' are grouped as 'unknown'.
    pct is the share of all responses in the window (0-100, rounded to 1 dp).
    """
    if window_days and window_days > 0:
        rows = conn.execute(
            """
            select
                coalesce(properties->>'heard_via', 'unknown') as heard_via,
                count(*) as n
            from usage_events
            where event_type = 'survey_response'
              and received_at >= now() - make_interval(days => %s)
            group by 1
            order by 2 desc
            """,
            (window_days,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            select
                coalesce(properties->>'heard_via', 'unknown') as heard_via,
                count(*) as n
            from usage_events
            where event_type = 'survey_response'
            group by 1
            order by 2 desc
            """
        ).fetchall()

    total = sum(r[1] for r in rows) or 1
    return [
        {
            "heard_via": heard_via,
            "count": count,
            "pct": round(100 * count / total, 1),
        }
        for heard_via, count in rows
    ]


# ── Stubs kept for import compatibility with web.py ───────────────────────────
# These are referenced by the existing web.py imports but their pages have been
# replaced by Densologie-specific equivalents. They return empty/no-op values
# rather than crashing.

def install_retention_cohorts(conn: psycopg.Connection, max_offset: int = 8) -> dict:
    """Stub — no install lifecycle events in the Densologie schema."""
    return {"cohorts": [], "max_offset": max_offset}


def plan_mix(conn: psycopg.Connection) -> list[dict]:
    """Active subscriptions split by billing interval. Stub until plan types
    are added to subscription_revenue."""
    return []


def uninstall_reasons(conn: psycopg.Connection) -> dict:
    """Stub — replaced by survey_tally for Densologie."""
    return {
        "buckets": [],
        "mandatory_from": None,
        "era": {"pre": {"total": 0, "with_reason": 0, "coverage_pct": 0.0},
                "post": {"total": 0, "with_reason": 0, "coverage_pct": 0.0}},
        "total": 0, "with_reason": 0, "coverage_pct": 0.0,
        "languages": [],
    }


def monthly_activity(conn: psycopg.Connection, months: int = 6) -> list[dict]:
    """Orders vs refunds per calendar month. Replaces the old installs/uninstalls chart."""
    rows = conn.execute(
        """
        with bounds as (
            select generate_series(
                date_trunc('month', now()) - make_interval(months => %s - 1),
                date_trunc('month', now()),
                interval '1 month'
            ) as month_start
        )
        select to_char(b.month_start, 'Mon YYYY') as label,
               count(o.id) as orders,
               count(o.id) filter (where o.refunded > 0) as refunds
        from bounds b
        left join orders o
               on o.created_at >= b.month_start
              and o.created_at < b.month_start + interval '1 month'
        group by b.month_start, label
        order by b.month_start
        """,
        (months,),
    ).fetchall()
    return [
        {"label": label, "installs": orders, "uninstalls": refunds}
        for label, orders, refunds in rows
    ]


def recent_events(conn: psycopg.Connection, limit: int = 12) -> list[dict]:
    """Most recent orders as a 'latest activity' feed."""
    rows = conn.execute(
        """
        select o.id, c.id as customer_id, o.total, o.created_at, o.is_new_customer
        from orders o
        left join customers c on c.id = o.customer_id
        order by o.created_at desc
        limit %s
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "type": "new_customer" if is_new else "order",
            "shop": f"Customer {cust_id or order_id}",
            "at": at,
        }
        for order_id, cust_id, total, at, is_new in rows
    ]


def collected_revenue(conn: psycopg.Connection) -> dict:
    """Aggregate of all orders: gross, refunded, net, and 30-day slice."""
    row = conn.execute(
        """
        select coalesce(sum(total), 0),
               coalesce(sum(refunded), 0),
               coalesce(sum(total - refunded), 0),
               count(*),
               min(created_at), max(created_at)
        from orders
        """
    ).fetchone()
    gross, refunded, net, count, first, last = row

    net_30d = conn.execute(
        """
        select coalesce(sum(total - refunded), 0) from orders
        where created_at >= now() - interval '30 days'
        """
    ).fetchone()[0]

    return {
        "gross": gross,
        "refunded": refunded,
        "net": net,
        "net_30d": net_30d,
        "count": count,
        "first_at": first,
        "last_at": last,
        "taken": Decimal("0"),
        "taken_pct": 0.0,
        "refund_count": conn.execute(
            "select count(*) from orders where refunded > 0"
        ).fetchone()[0],
    }


def mrr_trend(conn: psycopg.Connection, months: int = 12) -> list[dict]:
    """Active subscription MRR at each month end, over the last N months."""
    rows = conn.execute(
        """
        with bounds as (
            select generate_series(
                date_trunc('month', now()) - make_interval(months => %s - 1),
                date_trunc('month', now()),
                interval '1 month'
            ) as month_start
        )
        select to_char(b.month_start, 'Mon YYYY') as label,
               b.month_start,
               coalesce(sum(sub.monthly_amount) filter (
                   where sub.converted_at < b.month_start + interval '1 month'
                     and (sub.churned_at is null
                          or sub.churned_at >= b.month_start + interval '1 month')
               ), 0) as mrr
        from bounds b left join subscription_revenue sub on true
        group by 1, 2
        order by 2
        """,
        (months,),
    ).fetchall()
    return [{"label": label, "mrr": mrr} for label, _, mrr in rows]


def mrr_movements(conn: psycopg.Connection, months: int = 12) -> list[dict]:
    """Subscription MRR movement waterfall (new/expansion/churn) per month."""
    rows = conn.execute(
        """select customer_id, coalesce(monthly_amount, 0), converted_at, churned_at
           from subscription_revenue where converted_at is not null"""
    ).fetchall()

    now = _utcnow()
    last = _month_index(now)
    first = last - months + 1

    if not rows:
        return []

    earliest = min((_month_index(r[2]) for r in rows), default=first)
    span = range(min(earliest, first) - 1, last + 1)
    by_cust: dict[str, list[Decimal]] = {}
    for cust_id, amount, converted_at, churned_at in rows:
        conv = _month_index(converted_at)
        churn = _month_index(churned_at) if churned_at is not None else None
        series = by_cust.setdefault(cust_id, [Decimal("0")] * len(span))
        for i, m in enumerate(span):
            if conv <= m and (churn is None or churn > m):
                series[i] += amount

    def _attribute(bucket, prev, curr, returning):
        if curr == prev:
            return
        if prev == 0:
            bucket["reactivation" if returning else "new"] += curr
        elif curr == 0:
            bucket["churned"] -= prev
        elif curr > prev:
            bucket["expansion"] += curr - prev
        else:
            bucket["contraction"] += curr - prev

    out = []
    for i, m in enumerate(span):
        if i == 0 or m < first:
            continue
        bucket = {k: Decimal("0") for k in MOVEMENT_KINDS}
        for series in by_cust.values():
            _attribute(bucket, series[i - 1], series[i],
                       returning=any(v > 0 for v in series[:i]))
        out.append({
            "label": f"{MONTH_NAMES[m % 12]} {m // 12}",
            **{k: bucket[k] for k in MOVEMENT_KINDS},
            "net": sum(bucket.values()),
        })
    return out


def mrr_at(conn: psycopg.Connection, t) -> Decimal:
    """Total active MRR at an instant."""
    return conn.execute(
        """select coalesce(sum(monthly_amount), 0) from subscription_revenue
           where converted_at <= %s and (churned_at is null or churned_at > %s)""",
        (t, t),
    ).fetchone()[0]


def paying_at(conn: psycopg.Connection, t) -> int:
    """Count of active subscribers at an instant."""
    return conn.execute(
        """select count(distinct customer_id) from subscription_revenue
           where converted_at <= %s and (churned_at is null or churned_at > %s)""",
        (t, t),
    ).fetchone()[0]


def mrr_movement_between(conn: psycopg.Connection, start, end) -> dict:
    """MRR movement buckets over an arbitrary span (used by digest)."""
    rows = conn.execute(
        """select customer_id, coalesce(monthly_amount, 0), converted_at, churned_at
           from subscription_revenue where converted_at is not null"""
    ).fetchall()

    def at(t):
        totals: dict[str, Decimal] = {}
        for cust_id, amount, converted_at, churned_at in rows:
            if converted_at <= t and (churned_at is None or churned_at > t):
                totals[cust_id] = totals.get(cust_id, Decimal("0")) + amount
        return totals

    before, after = at(start), at(end)
    ever_before = {r[0] for r in rows if r[2] <= start}

    def _attribute(bucket, prev, curr, returning):
        if curr == prev:
            return
        if prev == 0:
            bucket["reactivation" if returning else "new"] += curr
        elif curr == 0:
            bucket["churned"] -= prev
        elif curr > prev:
            bucket["expansion"] += curr - prev
        else:
            bucket["contraction"] += curr - prev

    bucket = {k: Decimal("0") for k in MOVEMENT_KINDS}
    for cust_id in set(before) | set(after):
        _attribute(bucket, before.get(cust_id, Decimal("0")),
                   after.get(cust_id, Decimal("0")), cust_id in ever_before)
    bucket["net"] = sum(bucket.values())
    return bucket


# ── Stubs kept to avoid import errors in legacy test files ────────────────────

def installed_at_time(conn, t) -> int:
    return 0


def overview_comparison_legacy(conn, current: dict, days: int = 30) -> dict:
    """Legacy signature kept for any code that calls overview_comparison(conn, current, days).
    Routes to the new signature."""
    now = _utcnow()
    cutoff = now - timedelta(days=days)
    prior_start = cutoff - timedelta(days=days)
    prior = overview_stats(conn, window_days=days)
    # Patch days_of_cover into prior (point metric, skip for simplicity)
    prior["days_of_cover"] = None
    return overview_comparison(current, prior)


def unit_economics(conn, days: int = 90) -> dict:
    """Stub kept for test_stats.py import compatibility."""
    return {
        "arpu": Decimal("0"),
        "paying": 0,
        "window_days": days,
        "subs_at_start": 0,
        "churned_in_window": 0,
        "monthly_churn_pct": 0.0,
        "ltv": None,
    }


def country_breakdown(conn, top: int = 10) -> list[dict]:
    """Stub — no country data in orders schema yet."""
    return []


def review_candidates(conn, min_days: int = 30) -> list[dict]:
    """Stub — no reviews in Densologie schema."""
    return []


def trial_watch(conn, days: int = 14, now=None) -> list[dict]:
    """Stub — no trial concept in Densologie schema."""
    return []


def annual_upgrade_candidates(conn, min_months: int = 3) -> list[dict]:
    """Stub — no annual plan concept in Densologie schema yet."""
    return []


# ── Funnel stats (GA4) ────────────────────────────────────────────────────────

def funnel_stats(conn: psycopg.Connection, window_days: int) -> dict:
    """Four-step acquisition funnel from GA4 data for the window.

    Returns:
        {
            "sessions": int,
            "add_to_carts": int,
            "begin_checkouts": int,
            "purchases": int,
            "atc_rate": float | None,      # ATC / sessions
            "checkout_rate": float | None, # begin_checkouts / ATC
            "purchase_rate": float | None, # purchases / begin_checkouts
            "overall_rate": float | None,  # purchases / sessions
            "has_data": bool,
        }
    """
    cutoff = _utcnow() - timedelta(days=window_days)
    row = conn.execute(
        """
        select
            coalesce(sum(sessions), 0),
            coalesce(sum(add_to_carts), 0),
            coalesce(sum(begin_checkouts), 0),
            coalesce(sum(purchases), 0)
        from ga4_funnel
        where date >= %s::date
        """,
        (cutoff.date(),),
    ).fetchone()

    if not row or row[0] == 0:
        return {
            "sessions": 0, "add_to_carts": 0, "begin_checkouts": 0, "purchases": 0,
            "atc_rate": None, "checkout_rate": None, "purchase_rate": None,
            "overall_rate": None, "has_data": False,
        }

    sessions, atc, bc, purchases = row
    return {
        "sessions": sessions,
        "add_to_carts": atc,
        "begin_checkouts": bc,
        "purchases": purchases,
        "atc_rate": round(100 * atc / sessions, 1) if sessions else None,
        "checkout_rate": round(100 * bc / atc, 1) if atc else None,
        "purchase_rate": round(100 * purchases / bc, 1) if bc else None,
        "overall_rate": round(100 * purchases / sessions, 1) if sessions else None,
        "has_data": True,
    }


def funnel_by_source(conn: psycopg.Connection, window_days: int) -> list[dict]:
    """Funnel breakdown by utm_source for the window.

    Returns list of dicts, one per source, sorted by sessions desc:
        [{"source": "meta", "sessions": 120, "add_to_carts": 30, ...}, ...]
    """
    cutoff = _utcnow() - timedelta(days=window_days)
    rows = conn.execute(
        """
        select
            case when utm_source = '' then 'organic' else utm_source end as source,
            sum(sessions) as sessions,
            sum(add_to_carts) as atc,
            sum(begin_checkouts) as bc,
            sum(purchases) as purchases
        from ga4_funnel
        where date >= %s::date
        group by 1
        order by sessions desc
        """,
        (cutoff.date(),),
    ).fetchall()
    return [
        {
            "source": r[0],
            "sessions": r[1],
            "add_to_carts": r[2],
            "begin_checkouts": r[3],
            "purchases": r[4],
            "purchase_rate": round(100 * r[4] / r[1], 1) if r[1] else None,
        }
        for r in rows
    ]


def abandoned_checkout_stats(conn: psycopg.Connection, window_days: int) -> dict:
    """Abandoned checkout count and recovery for the window.

    Returns:
        {"abandoned": int, "recovered": int, "recovered_revenue": Decimal | None}
    """
    cutoff = _utcnow() - timedelta(days=window_days)
    row = conn.execute(
        """
        select
            count(*) filter (where abandoned_at is not null),
            count(*) filter (where recovered_at is not null),
            sum(total) filter (where recovered_at is not null)
        from checkouts
        where created_at >= %s
        """,
        (cutoff,),
    ).fetchone()
    if not row:
        return {"abandoned": 0, "recovered": 0, "recovered_revenue": None}
    return {
        "abandoned": row[0] or 0,
        "recovered": row[1] or 0,
        "recovered_revenue": row[2],
    }


def discount_usage(conn: psycopg.Connection, window_days: int) -> dict:
    """Discount code usage for the window.

    Returns:
        {
            "orders_with_discount": int,
            "total_discount": Decimal | None,
            "top_codes": [{"code": str, "count": int, "total_discount": Decimal}],
        }
    """
    cutoff = _utcnow() - timedelta(days=window_days)
    summary = conn.execute(
        """
        select count(*), sum(discount_amount)
        from orders
        where created_at >= %s and discount_code is not null and discount_amount > 0
        """,
        (cutoff,),
    ).fetchone()
    top = conn.execute(
        """
        select discount_code, count(*), sum(discount_amount)
        from orders
        where created_at >= %s and discount_code is not null and discount_amount > 0
        group by discount_code
        order by sum(discount_amount) desc
        limit 5
        """,
        (cutoff,),
    ).fetchall()
    return {
        "orders_with_discount": summary[0] or 0 if summary else 0,
        "total_discount": summary[1] if summary else None,
        "top_codes": [
            {"code": r[0], "count": r[1], "total_discount": r[2]}
            for r in top
        ],
    }


def revenue_by_sku(conn: psycopg.Connection, window_days: int) -> list[dict]:
    """Revenue and units sold per SKU for the window, from line_items JSONB.

    Returns list sorted by revenue desc:
        [{"sku": "HAIR-SERUM-50ML", "units": 42, "revenue": Decimal("6258.00")}, ...]
    """
    cutoff = _utcnow() - timedelta(days=window_days)
    rows = conn.execute(
        """
        select
            item->>'sku' as sku,
            sum((item->>'quantity')::int) as units,
            sum((item->>'quantity')::int * (o.total - o.refunded) / nullif(
                (select sum((li->>'quantity')::int) from jsonb_array_elements(o.line_items) li), 0
            )) as revenue
        from orders o,
             jsonb_array_elements(o.line_items) item
        where o.created_at >= %s
          and o.line_items != '[]'::jsonb
        group by 1
        order by revenue desc nulls last
        """,
        (cutoff,),
    ).fetchall()
    return [
        {"sku": r[0], "units": r[1] or 0, "revenue": r[2]}
        for r in rows if r[0]
    ]


def repeat_purchase_rate(conn: psycopg.Connection, window_days: int) -> dict:
    """% of customers who placed more than one order within the window.

    Returns: {"rate": Decimal | None, "repeat_customers": int, "total_customers": int}
    """
    cutoff = _utcnow() - timedelta(days=window_days)
    row = conn.execute(
        """
        select
            count(*) as total,
            count(*) filter (where order_count > 1) as repeat_buyers
        from (
            select customer_id, count(*) as order_count
            from orders
            where created_at >= %s
            group by customer_id
        ) t
        """,
        (cutoff,),
    ).fetchone()
    if not row or not row[0]:
        return {"rate": None, "repeat_customers": 0, "total_customers": 0}
    total, repeat = row
    rate = Decimal("100") * Decimal(repeat) / Decimal(total) if total else None
    return {"rate": rate, "repeat_customers": repeat, "total_customers": total}


def refund_rate(conn: psycopg.Connection, window_days: int) -> dict:
    """% of orders that had any refund for the window.

    Returns: {"rate": Decimal | None, "refunded_orders": int, "total_orders": int}
    """
    cutoff = _utcnow() - timedelta(days=window_days)
    row = conn.execute(
        """
        select
            count(*) as total,
            count(*) filter (where refunded > 0) as refunded
        from orders
        where created_at >= %s
        """,
        (cutoff,),
    ).fetchone()
    if not row or not row[0]:
        return {"rate": None, "refunded_orders": 0, "total_orders": 0}
    total, refunded = row
    rate = Decimal("100") * Decimal(refunded) / Decimal(total) if total else None
    return {"rate": rate, "refunded_orders": refunded, "total_orders": total}


def generate_summary(stats: dict, comparison: dict, window_days: int) -> str:
    """Generate a plain-language one-liner summarising the window's key numbers."""
    parts = []
    if stats.get("revenue") is not None:
        parts.append(f"${stats['revenue']:,.0f} revenue")
    if stats.get("new_customers"):
        cac_str = f" at ${stats['blended_cac']:,.0f} CAC" if stats.get("blended_cac") else ""
        parts.append(f"{stats['new_customers']} new customers{cac_str}")
    if stats.get("mer") is not None:
        parts.append(f"{float(stats['mer']):.1f}x MER")

    sentence = " · ".join(parts) + "." if parts else "No data for this window."

    # Weakest metric: biggest negative % change
    watch = None
    worst_pct = None
    label_map = {
        "revenue": "Revenue",
        "blended_cac": "CAC",
        "mer": "MER",
        "subscription_share": "Subscription share",
    }
    for key, label in label_map.items():
        comp = comparison.get(key, {})
        pct = comp.get("pct")
        if pct is None:
            continue
        # For CAC, higher is worse
        if key == "blended_cac":
            pct = -pct
        if worst_pct is None or pct < worst_pct:
            worst_pct = pct
            watch_pct = comp.get("pct", 0)
            direction = "up" if (watch_pct or 0) > 0 else "down"
            watch = f"{label} {direction} {abs(watch_pct or 0):.0f}% vs prior period"

    if watch and (worst_pct is not None) and worst_pct < -5:
        sentence += f"  Watch: {watch}."

    return sentence


def _prior_window(window_days: int):
    """Returns (prior_start, prior_end) datetimes for the window before the current one."""
    now = _utcnow()
    current_start = now - timedelta(days=window_days)
    prior_end = current_start
    prior_start = prior_end - timedelta(days=window_days)
    return prior_start, prior_end


def all_prior_stats(conn: psycopg.Connection, window_days: int) -> dict:
    """Compute prior-period values for comparison display."""
    prior_start, prior_end = _prior_window(window_days)

    def scalar(sql, params=()):
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None

    prior_cutoff_date = prior_start.date()
    current_cutoff_date = prior_end.date()

    return {
        "omnisend_sends": scalar(
            "select sum(sends) from omnisend_sends where date >= %s::date and date < %s::date",
            (prior_cutoff_date, current_cutoff_date),
        ) or 0,
        "omnisend_attributed": scalar(
            "select sum(attributed_revenue) from omnisend_sends "
            "where date >= %s::date and date < %s::date",
            (prior_cutoff_date, current_cutoff_date),
        ),
        "meta_spend": scalar(
            "select sum(spend) from meta_ad_stats where date >= %s::date and date < %s::date",
            (prior_cutoff_date, current_cutoff_date),
        ),
        "repeat_rate": scalar(
            "select round(100.0 * count(*) filter(where order_count > 1) / nullif(count(*),0), 1) "
            "from (select customer_id, count(*) as order_count from orders "
            "where created_at >= %s and created_at < %s group by customer_id) t",
            (prior_start, prior_end),
        ),
        "refund_rate": scalar(
            "select round(100.0 * count(*) filter(where refunded>0) / nullif(count(*),0), 1) "
            "from orders where created_at >= %s and created_at < %s",
            (prior_start, prior_end),
        ),
    }


def kpi_sparklines(conn: psycopg.Connection, days: int = 7) -> dict:
    """Return the last `days` days of daily values for key KPIs, for sparklines."""
    cutoff = _utcnow() - timedelta(days=days)

    rev_rows = conn.execute(
        """
        select date_trunc('day', created_at)::date as d, sum(total - refunded)
        from orders where created_at >= %s
        group by d order by d
        """, (cutoff,)
    ).fetchall()

    nc_rows = conn.execute(
        """
        select date_trunc('day', created_at)::date as d, count(distinct customer_id)
        from orders where is_new_customer = true and created_at >= %s
        group by d order by d
        """, (cutoff,)
    ).fetchall()

    spend_rows = conn.execute(
        "select date, sum(spend) from ad_spend where date >= %s::date group by date order by date",
        (cutoff.date(),)
    ).fetchall()

    def to_series(rows, n=days):
        d = {r[0]: r[1] for r in rows}
        today = _utcnow().date()
        return [d.get(today - timedelta(days=n - 1 - i)) for i in range(n)]

    return {
        "revenue": to_series(rev_rows),
        "new_customers": to_series(nc_rows),
        "ad_spend": to_series(spend_rows),
    }


def meta_channel_vitals(conn: psycopg.Connection, window_days: int) -> dict:
    """Meta channel-level totals for the window."""
    cutoff = _utcnow() - timedelta(days=window_days)
    row = conn.execute(
        """
        select sum(spend), sum(impressions), sum(clicks),
               sum(purchases), sum(purchase_value)
        from meta_ad_stats
        where date >= %s::date
        """,
        (cutoff.date(),),
    ).fetchone()
    if not row or not row[0]:
        return {"spend": None, "impressions": 0, "clicks": 0,
                "ctr": None, "cpc": None, "cpm": None,
                "purchases": 0, "purchase_value": Decimal("0"),
                "roas": None, "has_data": False}
    spend, impr, clicks, purchases, pv = row
    ctr = round(100 * clicks / impr, 2) if impr else None
    cpc = (spend / Decimal(clicks)) if clicks else None
    cpm = (spend / Decimal(impr) * 1000) if impr else None
    roas = round(float(pv) / float(spend), 2) if spend and float(spend) > 0 else None
    return {
        "spend": spend, "impressions": impr or 0, "clicks": clicks or 0,
        "ctr": ctr, "cpc": cpc, "cpm": cpm,
        "purchases": purchases or 0, "purchase_value": pv or Decimal("0"),
        "roas": roas, "has_data": True,
    }


def meta_campaign_breakdown(conn: psycopg.Connection, window_days: int) -> list[dict]:
    """Spend/performance per campaign and ad set for the window."""
    cutoff = _utcnow() - timedelta(days=window_days)
    rows = conn.execute(
        """
        select campaign_name,
               coalesce(nullif(adset_name,''), '—') as adset_name,
               sum(spend) as spend,
               sum(purchases) as purchases,
               sum(purchase_value) as pv
        from meta_ad_stats
        where date >= %s::date
        group by campaign_name, adset_name
        order by spend desc
        """,
        (cutoff.date(),),
    ).fetchall()
    out = []
    for r in rows:
        spend = r[2] or Decimal("0")
        purch = r[3] or 0
        pv = r[4] or Decimal("0")
        cpa = (spend / Decimal(purch)) if purch else None
        roas = round(float(pv) / float(spend), 2) if spend and float(spend) > 0 else None
        out.append({
            "campaign": r[0], "adset": r[1],
            "spend": spend, "purchases": purch,
            "cpa": cpa, "roas": roas,
        })
    return out


def meta_top_ads(conn: psycopg.Connection, window_days: int, limit: int = 5) -> list[dict]:
    """Top N ads by spend with thumbnail and Ads Manager link."""
    cutoff = _utcnow() - timedelta(days=window_days)
    rows = conn.execute(
        """
        select ad_name,
               sum(spend) as spend,
               sum(clicks) as clicks,
               sum(impressions) as impr,
               sum(purchases) as purchases,
               sum(purchase_value) as pv,
               max(thumbnail_url) as thumb,
               max(ads_manager_url) as url
        from meta_ad_stats
        where date >= %s::date and ad_id != ''
        group by ad_name
        order by spend desc
        limit %s
        """,
        (cutoff.date(), limit),
    ).fetchall()
    out = []
    for r in rows:
        spend = r[1] or Decimal("0")
        impr = r[3] or 0
        pv = r[5] or Decimal("0")
        ctr = round(100 * r[2] / impr, 2) if impr else None
        roas = round(float(pv) / float(spend), 2) if spend and float(spend) > 0 else None
        out.append({
            "name": r[0], "spend": spend, "clicks": r[2] or 0,
            "ctr": ctr, "purchases": r[4] or 0, "roas": roas,
            "thumbnail_url": r[6], "ads_manager_url": r[7],
        })
    return out


# ── Gate 2 metrics ───────────────────────────────────────────────────────────

from typing import Optional


def gross_profit_per_order(conn: psycopg.Connection, order_id: int) -> Optional[Decimal]:
    """Returns gross profit for a single order using cost_inputs + cost_settings.
    Returns None if any line item SKU is missing from cost_inputs."""
    row = conn.execute(
        "select total, refunded, discount_amount, line_items from orders where id = %s",
        (order_id,),
    ).fetchone()
    if row is None:
        return None

    total, refunded, discount_amount, line_items = row
    discount_amount = discount_amount or Decimal("0")
    line_items = line_items or []

    # Load cost settings
    settings = {
        r[0]: Decimal(str(r[1]))
        for r in conn.execute("select key, value from cost_settings").fetchall()
    }
    if not settings:
        return None

    shipping = settings.get("shipping_cost_per_order", Decimal("0"))
    payment_fee_pct = settings.get("payment_fee_pct", Decimal("0"))

    # Sum COGS for all line items
    total_cogs = Decimal("0")
    for item in line_items:
        sku = item.get("sku") if isinstance(item, dict) else None
        quantity = Decimal(str(item.get("quantity", 1))) if isinstance(item, dict) else Decimal("1")
        if not sku:
            return None
        cogs_row = conn.execute(
            "select cogs_per_unit from cost_inputs where sku = %s", (sku,)
        ).fetchone()
        if cogs_row is None:
            return None
        total_cogs += quantity * cogs_row[0]

    net_revenue = total - refunded - discount_amount
    payment_fee = total * payment_fee_pct
    gp = net_revenue - total_cogs - shipping - payment_fee
    return gp


def mrr_recognized(conn: psycopg.Connection, year: int, month: int) -> Optional[Decimal]:
    """Sum of monthly_amount for all active (non-paused) subs during the given month.
    Paused subs are excluded per the lifecycle spec — their MRR is deferred, not recognized."""
    # A sub is active in a month if: converted_at < end of month AND (churned_at is null OR churned_at >= start of month)
    # AND status != 'paused' (paused subs do not have MRR recognized)
    row = conn.execute(
        """
        select coalesce(sum(monthly_amount), null)
        from subscription_revenue
        where converted_at < make_date(%s, %s, 1)::timestamptz + interval '1 month'
          and (churned_at is null or churned_at >= make_date(%s, %s, 1)::timestamptz)
          and status != 'paused'
        """,
        (year, month, year, month),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def cash_collected_in_month(conn: psycopg.Connection, year: int, month: int) -> Optional[Decimal]:
    """Sum of cash_collected where cash_collected > 0 for subs active in given month."""
    # Cash is collected at the start of the billing period (converted_at for the first month,
    # or renewal date for subsequent periods). For prepaid: cash_collected > 0 only in month 0.
    # We look for subs whose converted_at falls within this month (month 0 = initial charge),
    # plus monthly subs active in this month (each month has a charge).
    row = conn.execute(
        """
        select coalesce(sum(cash_collected), null)
        from subscription_revenue
        where cash_collected > 0
          and (
            -- initial cash collected: converted_at in this month
            (date_trunc('month', converted_at) = make_date(%s, %s, 1)::timestamptz)
            OR
            -- monthly subs: active and it's a renewal month
            (sub_type = 'monthly'
             and converted_at < make_date(%s, %s, 1)::timestamptz + interval '1 month'
             and (churned_at is null or churned_at >= make_date(%s, %s, 1)::timestamptz)
             and date_trunc('month', converted_at) != make_date(%s, %s, 1)::timestamptz)
          )
        """,
        (year, month, year, month, year, month, year, month),
    ).fetchone()
    return row[0] if row and row[0] is not None else Decimal("0")


def logo_churn_voluntary(conn: psycopg.Connection, year: int, month: int) -> Optional[Decimal]:
    """Voluntary churn rate for given month. Returns None if no subs at month start."""
    month_start = f"{year}-{month:02d}-01"
    at_start = conn.execute(
        """
        select count(*)
        from subscription_revenue
        where converted_at < %s::timestamptz
          and (churned_at is null or churned_at >= %s::timestamptz)
        """,
        (month_start, month_start),
    ).fetchone()[0]
    if not at_start:
        return None

    churned = conn.execute(
        """
        select count(*)
        from subscription_revenue
        where churn_type = 'voluntary'
          and date_trunc('month', churned_at) = %s::timestamptz
        """,
        (month_start,),
    ).fetchone()[0]

    return Decimal(str(churned)) / Decimal(str(at_start))


def logo_churn_involuntary(conn: psycopg.Connection, year: int, month: int) -> Optional[Decimal]:
    """Involuntary churn rate (confirmed, >=14 days dunning). Returns None if no subs at month start."""
    month_start = f"{year}-{month:02d}-01"
    at_start = conn.execute(
        """
        select count(*)
        from subscription_revenue
        where converted_at < %s::timestamptz
          and (churned_at is null or churned_at >= %s::timestamptz)
        """,
        (month_start, month_start),
    ).fetchone()[0]
    if not at_start:
        return None

    churned = conn.execute(
        """
        select count(*)
        from subscription_revenue
        where churn_type = 'involuntary'
          and date_trunc('month', churned_at) = %s::timestamptz
          and churned_at is not null
          and dunning_started_at is not null
          and (churned_at - dunning_started_at) >= interval '14 days'
        """,
        (month_start,),
    ).fetchone()[0]

    return Decimal(str(churned)) / Decimal(str(at_start))


def subs_in_dunning(conn: psycopg.Connection) -> dict:
    """Returns {'count': int, 'at_risk_mrr': Decimal} for subs in active dunning window (<14 days)."""
    # In dunning = dunning_started_at is within last 14 days AND no churned_at
    # AND no dunning_resolved event after the dunning_start
    row = conn.execute(
        """
        select count(*), coalesce(sum(sr.monthly_amount), 0)
        from subscription_revenue sr
        where sr.churned_at is null
          and sr.dunning_started_at is not null
          and sr.dunning_started_at > now() - interval '14 days'
          and not exists (
            select 1 from subscription_events se
            where se.subscription_id = sr.id
              and se.event_type = 'dunning_resolved'
              and se.event_date >= sr.dunning_started_at::date
          )
        """
    ).fetchone()
    return {
        "count": row[0] if row else 0,
        "at_risk_mrr": row[1] if row else Decimal("0"),
    }


def rev_churn_voluntary(conn: psycopg.Connection, year: int, month: int) -> Optional[Decimal]:
    """Revenue churn rate (voluntary) for given month."""
    month_start = f"{year}-{month:02d}-01"
    mrr_at_start = conn.execute(
        """
        select coalesce(sum(monthly_amount), null)
        from subscription_revenue
        where converted_at < %s::timestamptz
          and (churned_at is null or churned_at >= %s::timestamptz)
        """,
        (month_start, month_start),
    ).fetchone()[0]
    if not mrr_at_start:
        return None

    churned_mrr = conn.execute(
        """
        select coalesce(sum(monthly_amount), null)
        from subscription_revenue
        where churn_type = 'voluntary'
          and date_trunc('month', churned_at) = %s::timestamptz
        """,
        (month_start,),
    ).fetchone()[0]
    if churned_mrr is None:
        return Decimal("0")
    return churned_mrr / mrr_at_start


def rev_churn_involuntary(conn: psycopg.Connection, year: int, month: int) -> Optional[Decimal]:
    """Revenue churn rate (involuntary, confirmed >=14 days) for given month."""
    month_start = f"{year}-{month:02d}-01"
    mrr_at_start = conn.execute(
        """
        select coalesce(sum(monthly_amount), null)
        from subscription_revenue
        where converted_at < %s::timestamptz
          and (churned_at is null or churned_at >= %s::timestamptz)
        """,
        (month_start, month_start),
    ).fetchone()[0]
    if not mrr_at_start:
        return None

    churned_mrr = conn.execute(
        """
        select coalesce(sum(monthly_amount), null)
        from subscription_revenue
        where churn_type = 'involuntary'
          and date_trunc('month', churned_at) = %s::timestamptz
          and churned_at is not null
          and dunning_started_at is not null
          and (churned_at - dunning_started_at) >= interval '14 days'
        """,
        (month_start,),
    ).fetchone()[0]
    if churned_mrr is None:
        return Decimal("0")
    raw = churned_mrr / mrr_at_start
    return raw.quantize(Decimal("0.01"))


def skip_rate(conn: psycopg.Connection, year: int, month: int) -> Optional[Decimal]:
    """Skip rate = skips in month / active subs at month start."""
    month_start = f"{year}-{month:02d}-01"
    at_start = conn.execute(
        """
        select count(*)
        from subscription_revenue
        where converted_at < %s::timestamptz
          and (churned_at is null or churned_at >= %s::timestamptz)
        """,
        (month_start, month_start),
    ).fetchone()[0]
    if not at_start:
        return None

    skips = conn.execute(
        """
        select count(*)
        from subscription_events
        where event_type = 'skip'
          and date_trunc('month', event_date::timestamptz) = %s::timestamptz
        """,
        (month_start,),
    ).fetchone()[0]

    return Decimal(str(skips)) / Decimal(str(at_start))


def subscription_waterfall(conn: psycopg.Connection, year: int, month: int) -> dict:
    """Returns beginning_mrr, new_mrr, expansion_mrr, contraction_mrr,
       churned_mrr_voluntary, churned_mrr_involuntary, ending_mrr.
       All values from subscription_events."""
    month_start = f"{year}-{month:02d}-01"

    def _sum_events(event_types, extra_where="", params=()):
        type_placeholders = ",".join(["%s"] * len(event_types))
        sql = f"""
            select coalesce(sum(mrr_delta), 0)
            from subscription_events
            where event_type in ({type_placeholders})
              and date_trunc('month', event_date::timestamptz) = %s::timestamptz
              {extra_where}
        """
        return conn.execute(sql, list(event_types) + [month_start] + list(params)).fetchone()[0]

    # Beginning MRR = sum of 'new' events BEFORE this month
    beginning_mrr = conn.execute(
        """
        select coalesce(sum(mrr_delta), 0)
        from subscription_events
        where event_type = 'new'
          and event_date < %s::date
        """,
        (month_start,),
    ).fetchone()[0]
    # Subtract churns before this month
    churned_before = conn.execute(
        """
        select coalesce(abs(sum(mrr_delta)), 0)
        from subscription_events
        where event_type = 'churn'
          and event_date < %s::date
        """,
        (month_start,),
    ).fetchone()[0]
    beginning_mrr = (beginning_mrr or Decimal("0")) - (churned_before or Decimal("0"))

    new_mrr = _sum_events(("new",)) or Decimal("0")
    expansion_mrr = _sum_events(("expansion",)) or Decimal("0")

    # Contraction: stored as negative mrr_delta; return absolute value
    contraction_raw = conn.execute(
        """
        select coalesce(sum(mrr_delta), 0)
        from subscription_events
        where event_type = 'contraction'
          and date_trunc('month', event_date::timestamptz) = %s::timestamptz
        """,
        (month_start,),
    ).fetchone()[0] or Decimal("0")
    contraction_mrr = abs(contraction_raw)

    # Voluntary churn: stored as negative mrr_delta
    vol_churn_raw = conn.execute(
        """
        select coalesce(sum(se.mrr_delta), 0)
        from subscription_events se
        join subscription_revenue sr on sr.id = se.subscription_id
        where se.event_type = 'churn'
          and date_trunc('month', se.event_date::timestamptz) = %s::timestamptz
          and sr.churn_type = 'voluntary'
        """,
        (month_start,),
    ).fetchone()[0] or Decimal("0")
    churned_mrr_voluntary = abs(vol_churn_raw)

    # Involuntary churn (confirmed >=14 days)
    invol_churn_raw = conn.execute(
        """
        select coalesce(sum(se.mrr_delta), 0)
        from subscription_events se
        join subscription_revenue sr on sr.id = se.subscription_id
        where se.event_type = 'churn'
          and date_trunc('month', se.event_date::timestamptz) = %s::timestamptz
          and sr.churn_type = 'involuntary'
          and sr.dunning_started_at is not null
          and (sr.churned_at - sr.dunning_started_at) >= interval '14 days'
        """,
        (month_start,),
    ).fetchone()[0] or Decimal("0")
    churned_mrr_involuntary = abs(invol_churn_raw)

    ending_mrr = (beginning_mrr + new_mrr + expansion_mrr
                  - contraction_mrr - churned_mrr_voluntary - churned_mrr_involuntary)

    return {
        "beginning_mrr": beginning_mrr,
        "new_mrr": new_mrr,
        "expansion_mrr": expansion_mrr,
        "contraction_mrr": contraction_mrr,
        "churned_mrr_voluntary": churned_mrr_voluntary,
        "churned_mrr_involuntary": churned_mrr_involuntary,
        "ending_mrr": ending_mrr,
    }


def three_revenue_streams(conn: psycopg.Connection, window_days: int = 30) -> dict:
    """Returns new_customer_revenue, subscription_recurring_revenue,
       non_sub_repeat_revenue, total. Null-not-zero.

    The three streams are mutually exclusive:
      - new_customer: is_new_customer = true (regardless of sub flag)
      - subscription_recurring: is_subscription_order = true AND is_new_customer = false
      - non_sub_repeat: is_new_customer = false AND is_subscription_order = false
    """
    cutoff = _utcnow() - timedelta(days=window_days)

    new_rev = conn.execute(
        """
        select coalesce(sum(total - refunded), null)
        from orders
        where is_new_customer = true and created_at >= %s
        """,
        (cutoff,),
    ).fetchone()[0]

    sub_rev = conn.execute(
        """
        select coalesce(sum(total - refunded), null)
        from orders
        where is_subscription_order = true
          and is_new_customer = false
          and created_at >= %s
        """,
        (cutoff,),
    ).fetchone()[0]

    non_sub_rev = conn.execute(
        """
        select coalesce(sum(total - refunded), null)
        from orders
        where is_new_customer = false
          and is_subscription_order = false
          and created_at >= %s
        """,
        (cutoff,),
    ).fetchone()[0]

    # Total: sum of all three (convert None to 0 for summation, then return None if all None)
    all_none = (new_rev is None and sub_rev is None and non_sub_rev is None)
    if all_none:
        total = None
    else:
        total = (new_rev or Decimal("0")) + (sub_rev or Decimal("0")) + (non_sub_rev or Decimal("0"))

    return {
        "new_customer_revenue": new_rev,
        "subscription_recurring_revenue": sub_rev,
        "non_sub_repeat_revenue": non_sub_rev,
        "total": total,
    }


def cohort_ltv_12m(conn: psycopg.Connection) -> list[dict]:
    """Returns list of {cohort_label, ltv, cohort_size, is_estimated} for
       cohorts with >=12 months of history."""
    now = _utcnow()
    cutoff_cohort = now - timedelta(days=365)

    rows = conn.execute(
        """
        select
            to_char(date_trunc('month', c.first_order_at), 'YYYY-MM') as cohort_label,
            count(distinct c.id) as cohort_size,
            date_trunc('month', c.first_order_at) as cohort_start
        from customers c
        where c.first_order_at < %s
        group by cohort_label, cohort_start
        order by cohort_start
        """,
        (cutoff_cohort,),
    ).fetchall()

    result = []
    for cohort_label, cohort_size, cohort_start in rows:
        cohort_end = cohort_start + timedelta(days=365)
        # Get all orders from cohort members in first 12 months
        order_rows = conn.execute(
            """
            select o.id, o.total, o.refunded, o.discount_amount, o.line_items
            from orders o
            join customers c on c.id = o.customer_id
            where date_trunc('month', c.first_order_at) = %s
              and o.created_at >= %s
              and o.created_at < %s
            """,
            (cohort_start, cohort_start, cohort_end),
        ).fetchall()

        total_gp = Decimal("0")
        is_estimated = False

        settings = {
            r[0]: Decimal(str(r[1]))
            for r in conn.execute("select key, value from cost_settings").fetchall()
        }
        shipping = settings.get("shipping_cost_per_order", Decimal("0"))
        payment_fee_pct = settings.get("payment_fee_pct", Decimal("0"))

        for order_id, total, refunded, discount_amount, line_items in order_rows:
            discount_amount = discount_amount or Decimal("0")
            line_items = line_items or []
            cogs = Decimal("0")
            cogs_ok = True
            for item in line_items:
                sku = item.get("sku") if isinstance(item, dict) else None
                qty = Decimal(str(item.get("quantity", 1))) if isinstance(item, dict) else Decimal("1")
                if sku:
                    cogs_row = conn.execute(
                        "select cogs_per_unit from cost_inputs where sku = %s", (sku,)
                    ).fetchone()
                    if cogs_row is None:
                        cogs_ok = False
                        break
                    cogs += qty * cogs_row[0]
                else:
                    cogs_ok = False
                    break
            if not cogs_ok:
                is_estimated = True
                continue
            gp = (total - refunded - discount_amount) - cogs - shipping - (total * payment_fee_pct)
            total_gp += gp

        if cohort_size > 0:
            ltv = total_gp / Decimal(cohort_size)
        else:
            ltv = Decimal("0")

        result.append({
            "cohort_label": cohort_label,
            "ltv": ltv,
            "cohort_size": cohort_size,
            "is_estimated": is_estimated,
        })

    return result


def theoretical_ltv(conn: psycopg.Connection) -> Optional[Decimal]:
    """avg_monthly_GP_per_active_sub / monthly_logo_churn_total. None if no data."""
    # Active sub GP from sub orders in last 90 days / active subs / 3
    active_subs = conn.execute(
        "select count(*) from subscription_revenue where churned_at is null"
    ).fetchone()[0]
    if not active_subs:
        return None

    cutoff = _utcnow() - timedelta(days=90)
    settings = {
        r[0]: Decimal(str(r[1]))
        for r in conn.execute("select key, value from cost_settings").fetchall()
    }
    if not settings:
        return None

    shipping = settings.get("shipping_cost_per_order", Decimal("0"))
    payment_fee_pct = settings.get("payment_fee_pct", Decimal("0"))

    sub_orders = conn.execute(
        """
        select o.total, o.refunded, o.discount_amount, o.line_items
        from orders o
        where o.is_subscription_order = true
          and o.created_at >= %s
        """,
        (cutoff,),
    ).fetchall()

    if not sub_orders:
        return None

    total_gp = Decimal("0")
    for total, refunded, discount_amount, line_items in sub_orders:
        discount_amount = discount_amount or Decimal("0")
        line_items = line_items or []
        cogs = Decimal("0")
        for item in line_items:
            sku = item.get("sku") if isinstance(item, dict) else None
            qty = Decimal(str(item.get("quantity", 1))) if isinstance(item, dict) else Decimal("1")
            if sku:
                cogs_row = conn.execute(
                    "select cogs_per_unit from cost_inputs where sku = %s", (sku,)
                ).fetchone()
                if cogs_row:
                    cogs += qty * cogs_row[0]
        gp = (total - refunded - discount_amount) - cogs - shipping - (total * payment_fee_pct)
        total_gp += gp

    avg_monthly_gp = total_gp / Decimal(active_subs) / Decimal("3")

    # Last full month's total logo churn
    now = _utcnow()
    last_month = now.replace(day=1) - timedelta(days=1)
    vol_churn = logo_churn_voluntary(conn, last_month.year, last_month.month)
    invol_churn = logo_churn_involuntary(conn, last_month.year, last_month.month)

    if vol_churn is None and invol_churn is None:
        return None

    total_churn = (vol_churn or Decimal("0")) + (invol_churn or Decimal("0"))
    if total_churn == 0:
        return None

    return avg_monthly_gp / total_churn


def payback_timing(conn: psycopg.Connection) -> list[dict]:
    """Returns list of {cohort_label, cac, gp_by_month: list[Decimal], payback_month: Optional[int]}"""
    now = _utcnow()
    settings = {
        r[0]: Decimal(str(r[1]))
        for r in conn.execute("select key, value from cost_settings").fetchall()
    }
    shipping = settings.get("shipping_cost_per_order", Decimal("0"))
    payment_fee_pct = settings.get("payment_fee_pct", Decimal("0"))

    cohort_rows = conn.execute(
        """
        select to_char(date_trunc('month', first_order_at), 'YYYY-MM') as label,
               date_trunc('month', first_order_at) as month_start,
               count(*) as cohort_size
        from customers
        group by label, month_start
        order by month_start
        """
    ).fetchall()

    result = []
    for cohort_label, month_start, cohort_size in cohort_rows:
        # CAC for this cohort month
        spend = conn.execute(
            "select coalesce(sum(spend), null) from ad_spend where date_trunc('month', date::timestamptz) = %s",
            (month_start,),
        ).fetchone()[0]
        new_custs = conn.execute(
            """
            select count(distinct customer_id) from orders
            where is_new_customer = true
              and date_trunc('month', created_at) = %s
            """,
            (month_start,),
        ).fetchone()[0]

        if not spend or not new_custs:
            cac = None
        else:
            cac = spend / Decimal(new_custs)

        # GP per customer by month offset
        max_offset = min(23, (now.year * 12 + now.month - 1) - (month_start.year * 12 + month_start.month - 1))
        gp_by_month = []
        cum_gp = Decimal("0")
        payback_month = None

        for offset in range(max_offset + 1):
            target_start = month_start + timedelta(days=30 * offset)
            target_end = month_start + timedelta(days=30 * (offset + 1))

            order_rows = conn.execute(
                """
                select o.total, o.refunded, o.discount_amount, o.line_items
                from orders o
                join customers c on c.id = o.customer_id
                where date_trunc('month', c.first_order_at) = %s
                  and o.created_at >= %s and o.created_at < %s
                """,
                (month_start, target_start, target_end),
            ).fetchall()

            month_gp = Decimal("0")
            for total, refunded, discount_amount, line_items in order_rows:
                discount_amount = discount_amount or Decimal("0")
                line_items = line_items or []
                cogs = Decimal("0")
                for item in line_items:
                    sku = item.get("sku") if isinstance(item, dict) else None
                    qty = Decimal(str(item.get("quantity", 1))) if isinstance(item, dict) else Decimal("1")
                    if sku:
                        cogs_row = conn.execute(
                            "select cogs_per_unit from cost_inputs where sku = %s", (sku,)
                        ).fetchone()
                        if cogs_row:
                            cogs += qty * cogs_row[0]
                gp = (total - refunded - discount_amount) - cogs - shipping - (total * payment_fee_pct)
                month_gp += gp

            if cohort_size > 0:
                cum_gp += month_gp / Decimal(cohort_size)
            gp_by_month.append(cum_gp)

            if payback_month is None and cac is not None and cum_gp >= cac:
                payback_month = offset

        result.append({
            "cohort_label": cohort_label,
            "cac": cac,
            "gp_by_month": gp_by_month,
            "payback_month": payback_month,
        })

    return result


def upsell_stats(conn: psycopg.Connection, window_days: int = 30) -> dict:
    """Returns dict with take_rates for each upsell_type. Null-not-zero."""
    cutoff = _utcnow() - timedelta(days=window_days)

    order_count = conn.execute(
        "select count(*) from orders where created_at >= %s",
        (cutoff,),
    ).fetchone()[0]

    upsell_types = ["priority_shipping", "upsell_t1", "upsell_t2", "upsell_t3", "aftersell"]
    result = {}

    for utype in upsell_types:
        accepted = conn.execute(
            """
            select count(*)
            from upsell_events ue
            join orders o on o.id = ue.order_id
            where ue.upsell_type = %s
              and ue.accepted = true
              and o.created_at >= %s
            """,
            (utype, cutoff),
        ).fetchone()[0]

        total_events = conn.execute(
            """
            select count(*)
            from upsell_events ue
            join orders o on o.id = ue.order_id
            where ue.upsell_type = %s
              and o.created_at >= %s
            """,
            (utype, cutoff),
        ).fetchone()[0]

        if total_events == 0 or order_count == 0:
            result[utype] = {"take_rate": None, "accepted": 0, "total": 0}
        else:
            take_rate = Decimal("100") * Decimal(str(accepted)) / Decimal(str(order_count))
            result[utype] = {
                "take_rate": take_rate,
                "accepted": accepted,
                "total": total_events,
            }

    return result


def active_subscribers(conn: psycopg.Connection, as_of=None) -> int:
    """Count of active (non-paused, non-churned) subscribers as of a given datetime.
    If as_of is None, uses now(). Excludes status='paused' and status='churned'."""
    if as_of is None:
        as_of = _utcnow()
    row = conn.execute(
        """
        select count(*)
        from subscription_revenue
        where converted_at <= %s
          and status = 'active'
          and (churned_at is null or churned_at > %s)
        """,
        (as_of, as_of),
    ).fetchone()
    return row[0] if row else 0


def paused_subscribers(conn: psycopg.Connection) -> dict:
    """Returns {'count': int, 'deferred_mrr': Decimal}.
    Deferred MRR = sum of monthly_amount for status='paused' subs.
    Returns {'count': 0, 'deferred_mrr': None} if no paused subs."""
    row = conn.execute(
        """
        select count(*), sum(monthly_amount)
        from subscription_revenue
        where status = 'paused'
        """
    ).fetchone()
    count = row[0] if row else 0
    deferred_mrr = row[1] if row and row[1] is not None else None
    return {"count": count, "deferred_mrr": deferred_mrr}


def pause_rate(conn: psycopg.Connection, year: int, month: int) -> Optional[Decimal]:
    """Pauses starting in the given month / active subs at month start.
    Returns None if no active subs at month start, or if subscription_events is empty
    (cannot distinguish zero pauses from untracked pauses)."""
    month_start = f"{year}-{month:02d}-01"
    at_start = conn.execute(
        """
        select count(*)
        from subscription_revenue
        where converted_at < %s::timestamptz
          and status = 'active'
          and (churned_at is null or churned_at >= %s::timestamptz)
        """,
        (month_start, month_start),
    ).fetchone()[0]
    if not at_start:
        return None

    has_events = conn.execute(
        "select exists(select 1 from subscription_events limit 1)"
    ).fetchone()[0]
    if not has_events:
        return None

    pauses = conn.execute(
        """
        select count(*)
        from subscription_events
        where event_type = 'pause'
          and date_trunc('month', event_date::timestamptz) = %s::timestamptz
        """,
        (month_start,),
    ).fetchone()[0]

    return Decimal(str(pauses)) / Decimal(str(at_start))


def pause_outcome_split(conn: psycopg.Connection) -> Optional[dict]:
    """Of all subs that have ever been paused (paused_at IS NOT NULL):
    Returns {'reactivated_pct': Decimal, 'cancelled_pct': Decimal, 'still_paused_pct': Decimal, 'total_paused': int}.
    Returns None if no subs have ever been paused."""
    row = conn.execute(
        """
        select
            count(*) as total,
            count(*) filter (where paused_outcome = 'reactivated') as reactivated,
            count(*) filter (where paused_outcome = 'cancelled') as cancelled,
            count(*) filter (where paused_outcome is null) as still_paused
        from subscription_revenue
        where paused_at is not null
        """
    ).fetchone()
    if not row or not row[0]:
        return None
    total, reactivated, cancelled, still_paused = row
    return {
        "reactivated_pct": Decimal("100") * Decimal(str(reactivated)) / Decimal(str(total)),
        "cancelled_pct": Decimal("100") * Decimal(str(cancelled)) / Decimal(str(total)),
        "still_paused_pct": Decimal("100") * Decimal(str(still_paused)) / Decimal(str(total)),
        "total_paused": total,
    }


def reactivation_stats(conn: psycopg.Connection, window_days: int = 90) -> Optional[dict]:
    """Returns {'count': int, 'recovered_mrr': Decimal, 'avg_gap_days': Decimal}.
    count = winback subscription_events in window.
    recovered_mrr = sum of mrr_delta for those events.
    avg_gap_days = average days between parent sub's churned_at and winback event_date.
    Returns None if no winbacks in window."""
    cutoff = _utcnow() - timedelta(days=window_days)
    rows = conn.execute(
        """
        select se.mrr_delta,
               se.event_date,
               sr.churned_at
        from subscription_events se
        join subscription_revenue sr on sr.id = se.subscription_id
        where se.event_type = 'winback'
          and se.event_date >= %s::date
        """,
        (cutoff.date(),),
    ).fetchall()
    if not rows:
        return None

    count = len(rows)
    recovered_mrr = sum(r[0] for r in rows if r[0] is not None) or Decimal("0")
    gap_days_list = []
    for mrr_delta, event_date, churned_at in rows:
        if churned_at is not None:
            # event_date is a date, churned_at is a timestamptz
            if hasattr(event_date, 'date'):
                ed = event_date
            else:
                from datetime import date as _date
                ed = event_date
            from datetime import datetime as _dt
            if isinstance(ed, _dt):
                ed = ed.date()
            gap = (ed - churned_at.date()).days
            if gap >= 0:
                gap_days_list.append(gap)

    avg_gap_days = (
        Decimal(str(sum(gap_days_list))) / Decimal(str(len(gap_days_list)))
        if gap_days_list else Decimal("0")
    )

    return {
        "count": count,
        "recovered_mrr": recovered_mrr,
        "avg_gap_days": avg_gap_days,
    }


def reactivation_rate_by_cohort(conn: psycopg.Connection) -> list[dict]:
    """For each cohort (first_order month), returns:
    {'cohort_label': str, 'cohort_size': int, 'winback_count': int, 'winback_rate_pct': Decimal}
    Only for cohorts with at least one win-back."""
    rows = conn.execute(
        """
        select
            to_char(date_trunc('month', c.first_order_at), 'YYYY-MM') as cohort_label,
            count(distinct c.id) as cohort_size,
            sum(c.winback_count) as winbacks
        from customers c
        group by cohort_label
        having sum(c.winback_count) > 0
        order by cohort_label
        """
    ).fetchall()
    result = []
    for cohort_label, cohort_size, winbacks in rows:
        winback_count = int(winbacks or 0)
        rate = Decimal("100") * Decimal(str(winback_count)) / Decimal(str(cohort_size)) if cohort_size else Decimal("0")
        result.append({
            "cohort_label": cohort_label,
            "cohort_size": cohort_size,
            "winback_count": winback_count,
            "winback_rate_pct": rate,
        })
    return result


def blended_cac_excl_reactivations(conn: psycopg.Connection, window_days: int = 30) -> Optional[Decimal]:
    """CAC = ad_spend / new_customers where new_customers EXCLUDES winback events.
    Uses is_new_customer=true AND winback_count=0 (not a returning subscriber).
    Returns None if no new customers in window."""
    cutoff = _utcnow() - timedelta(days=window_days)
    total_spend = conn.execute(
        """
        select coalesce(sum(spend), null)
        from ad_spend
        where date >= %s::date
        """,
        (cutoff.date(),),
    ).fetchone()[0]
    if not total_spend:
        return None

    new_customers = conn.execute(
        """
        select count(distinct o.customer_id)
        from orders o
        join customers c on c.id = o.customer_id
        where o.is_new_customer = true
          and c.winback_count = 0
          and o.created_at >= %s
        """,
        (cutoff,),
    ).fetchone()[0]

    if not new_customers:
        return None

    return total_spend / Decimal(str(new_customers))


# Maximum allowed difference between event-derived and measured ending MRR before
# the waterfall is replaced by an out-of-sync message. $5 absorbs decimal rounding
# but catches any missing event (minimum real subscriber MRR ~$30+).
_WATERFALL_RECONCILE_TOLERANCE = Decimal("5.00")


def _ending_mrr_actual(conn: psycopg.Connection, year: int, month: int) -> Optional[Decimal]:
    """Independent MRR measurement for the end of the given month.

    Current month: sums monthly_amount for status='active' subscriptions right now.
    Past months: reads the last subscription_snapshot of that month (mrr_recognized column).

    Returns None if the measurement is unavailable (no snapshot for a past month).
    The caller treats None as 'measurement unavailable — do not gate the waterfall.'
    """
    now = _utcnow()
    if year == now.year and month == now.month:
        row = conn.execute(
            "select coalesce(sum(monthly_amount), null) from subscription_revenue where status = 'active'"
        ).fetchone()
        return row[0] if row else None
    else:
        row = conn.execute(
            """
            select mrr_recognized
            from subscription_snapshots
            where snapshot_date >= make_date(%s, %s, 1)
              and snapshot_date <  (make_date(%s, %s, 1) + interval '1 month')::date
            order by snapshot_date desc
            limit 1
            """,
            (year, month, year, month),
        ).fetchone()
        return row[0] if row and row[0] is not None else None


def subscription_waterfall_v2(conn: psycopg.Connection, year: int, month: int) -> Optional[dict]:
    """MRR waterfall with reactivation bucket and independent reconciliation check.

    Returns None when subscription_events is empty.

    Otherwise always returns a dict. Check result['reconciled']:
      True  — event-derived ending MRR agrees with measured ending MRR within
              _WATERFALL_RECONCILE_TOLERANCE. Safe to render.
      False — gap detected; result['reconcile_delta'] = measured − event-derived.
              Negative = events overstate MRR (missing churn/contraction).
              Positive = events understate MRR (missing expansion).
              Render the gap to the operator instead of the buckets.

    reconcile_delta is None when no independent measurement is available (no
    snapshot for a past month). In that case reconciled=True and the waterfall
    renders — absence of a check is not evidence of a problem.

    Bug fixed here: the v1 base['ending_mrr'] did not include reactivation_mrr.
    """
    has_events = conn.execute(
        "select exists(select 1 from subscription_events limit 1)"
    ).fetchone()[0]
    if not has_events:
        return None

    base = subscription_waterfall(conn, year, month)
    month_start = f"{year}-{month:02d}-01"
    reactivation_mrr = conn.execute(
        """
        select coalesce(sum(mrr_delta), 0)
        from subscription_events
        where event_type = 'winback'
          and date_trunc('month', event_date::timestamptz) = %s::timestamptz
        """,
        (month_start,),
    ).fetchone()[0] or Decimal("0")

    # ending_mrr derived entirely from events (tautology without a check).
    ending_mrr_events = (
        base["ending_mrr"] + reactivation_mrr
        # base["ending_mrr"] = beginning + new + expansion - contraction - voluntary - involuntary
        # Add reactivation here; it was missing from the v1 formula.
    )

    # Independent measurement — breaks the tautology.
    ending_mrr_actual = _ending_mrr_actual(conn, year, month)
    if ending_mrr_actual is not None:
        reconcile_delta = ending_mrr_actual - ending_mrr_events
        reconciled = abs(reconcile_delta) <= _WATERFALL_RECONCILE_TOLERANCE
    else:
        reconcile_delta = None
        reconciled = True  # no measurement available; don't gate on absence of data

    return {
        "beginning_mrr":        base["beginning_mrr"],
        "new_mrr":              base["new_mrr"],
        "reactivation_mrr":     reactivation_mrr,
        "expansion_mrr":        base["expansion_mrr"],
        "contraction_mrr":      base["contraction_mrr"],
        "churned_mrr_voluntary":   base["churned_mrr_voluntary"],
        "churned_mrr_involuntary": base["churned_mrr_involuntary"],
        "ending_mrr":           ending_mrr_events,
        "ending_mrr_actual":    ending_mrr_actual,
        "reconciled":           reconciled,
        "reconcile_delta":      reconcile_delta,
    }


def get_subscriber_state_counts(conn: psycopg.Connection) -> dict:
    """Returns {'active': int, 'paused': int, 'churned': int, 'total': int}.
    Used for reconciliation test."""
    row = conn.execute(
        """
        select
            count(*) filter (where status = 'active') as active,
            count(*) filter (where status = 'paused') as paused,
            count(*) filter (where status = 'churned') as churned,
            count(*) as total
        from subscription_revenue
        """
    ).fetchone()
    if not row:
        return {"active": 0, "paused": 0, "churned": 0, "total": 0}
    return {
        "active": row[0] or 0,
        "paused": row[1] or 0,
        "churned": row[2] or 0,
        "total": row[3] or 0,
    }


def landing_page_funnel(conn: psycopg.Connection, window_days: int = 30) -> list[dict]:
    """Returns list of {page_type, sessions, atc_rate, checkout_rate, purchase_rate}
       ordered by sessions desc. Excludes direct_checkout from funnel math."""
    cutoff = _utcnow() - timedelta(days=window_days)

    rows = conn.execute(
        """
        select
            landing_page_type,
            sum(sessions) as sessions,
            sum(add_to_carts) as atcs,
            sum(begin_checkouts) as bcs,
            sum(purchases) as purchases
        from ga4_funnel
        where date >= %s::date
          and landing_page_type != 'direct_checkout'
        group by landing_page_type
        order by sessions desc
        """,
        (cutoff.date(),),
    ).fetchall()

    result = []
    for page_type, sessions, atcs, bcs, purchases in rows:
        atc_rate = round(100 * atcs / sessions, 1) if sessions else None
        checkout_rate = round(100 * bcs / sessions, 1) if sessions else None
        purchase_rate = round(100 * purchases / sessions, 1) if sessions else None
        result.append({
            "page_type": page_type,
            "sessions": sessions,
            "atc_rate": atc_rate,
            "checkout_rate": checkout_rate,
            "purchase_rate": purchase_rate,
        })

    return result


def offer_segmented_cohorts(conn: psycopg.Connection) -> dict:
    """Returns dict keyed by offer_tag, each value a cohort grid identical to
       the existing cohort_revenue_per_customer structure."""
    now = _utcnow()
    this_month = _month_index(now)

    offer_tags = ["full-price", "coupon-only", "steep-intro-discount", "reactivation"]
    result = {}

    for offer in offer_tags:
        rows = conn.execute(
            """
            select c.id,
                   date_trunc('month', c.first_order_at) as cohort_month,
                   date_trunc('month', o.created_at) as order_month,
                   (o.total - o.refunded) as net
            from orders o
            join customers c on c.id = o.customer_id
            where c.acquisition_offer = %s
            order by cohort_month, c.id, order_month
            """,
            (offer,),
        ).fetchall()

        if not rows:
            result[offer] = {"months": [], "max_age": 0, "rows": [], "target_ltgp": 390.0}
            continue

        from collections import defaultdict
        cohort_customers: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(Decimal)))
        for cust_id, cohort_month_dt, order_month_dt, net in rows:
            cohort_mi = _month_index(cohort_month_dt)
            order_mi = _month_index(order_month_dt)
            cohort_customers[cohort_mi][cust_id][order_mi] += (net or Decimal("0"))

        all_cohort_months = sorted(cohort_customers.keys())
        max_age = max(this_month - cm for cm in all_cohort_months)
        max_age = min(max_age, 23)

        result_rows = []
        month_labels = []

        for cohort_mi in all_cohort_months:
            ym = f"{cohort_mi // 12}-{1 + (cohort_mi % 12):02d}"
            month_labels.append(ym)
            customers = cohort_customers[cohort_mi]
            cohort_size = len(customers)
            observable_months = this_month - cohort_mi

            values = []
            for offset in range(min(observable_months + 1, max_age + 1)):
                target_mi = cohort_mi + offset
                total_cumulative = Decimal("0")
                for cust_revenues in customers.values():
                    for order_mi, net in cust_revenues.items():
                        if order_mi <= target_mi:
                            total_cumulative += net
                values.append(total_cumulative / Decimal(cohort_size) if cohort_size else None)

            result_rows.append({
                "cohort": ym,
                "size": cohort_size,
                "cells": values,
            })

        result[offer] = {
            "months": month_labels,
            "max_age": max_age,
            "rows": result_rows,
            "target_ltgp": 390.0,
        }

    return result


def subscription_retention_by_offset(conn: psycopg.Connection) -> dict:
    """Returns {cohort_label: {M1: pct, M3: pct, M6: pct, M12: pct}} for all cohorts.
       None for offsets not yet observable."""
    now = _utcnow()
    this_month = _month_index(now)

    rows = conn.execute(
        "select id, converted_at, churned_at from subscription_revenue where converted_at is not null"
    ).fetchall()

    if not rows:
        return {}

    from collections import defaultdict
    cohorts: dict[int, list] = defaultdict(list)
    for sub_id, converted_at, churned_at in rows:
        cm = _month_index(converted_at)
        churn_offset = None
        if churned_at is not None:
            churn_offset = _month_index(churned_at) - cm
        cohorts[cm].append(churn_offset)

    result = {}
    offsets = [1, 3, 6, 12]
    for cm in sorted(cohorts.keys()):
        ym = f"{cm // 12}-{1 + (cm % 12):02d}"
        subs = cohorts[cm]
        cohort_size = len(subs)
        observable_months = this_month - cm

        row = {}
        for offset in offsets:
            if observable_months < offset:
                row[f"M{offset}"] = None
            else:
                active = sum(1 for c in subs if c is None or c > offset)
                row[f"M{offset}"] = round(100 * active / cohort_size) if cohort_size else None
        result[ym] = row

    return result


def omnisend_summary(conn: psycopg.Connection, window_days: int, total_revenue) -> dict:
    """Omnisend email/SMS summary for the window.

    Returns:
        {
            "sends": int, "opens": int, "clicks": int,
            "open_rate": float | None, "click_rate": float | None,
            "attributed_revenue": Decimal | None,
            "revenue_share": Decimal | None,   # % of total window revenue
            "top_flows": [{"name": str, "revenue": Decimal, "clicks": int}],
            "has_data": bool,
        }
    """
    cutoff = _utcnow() - timedelta(days=window_days)
    row = conn.execute(
        """
        select sum(sends), sum(opens), sum(clicks), sum(attributed_revenue)
        from omnisend_sends
        where date >= %s::date
        """,
        (cutoff.date(),),
    ).fetchone()

    if not row or not row[0]:
        return {
            "sends": 0, "opens": 0, "clicks": 0,
            "open_rate": None, "click_rate": None,
            "attributed_revenue": None, "revenue_share": None,
            "top_flows": [], "has_data": False,
        }

    sends, opens, clicks, attributed = row

    top_flows_rows = conn.execute(
        """
        select coalesce(nullif(flow_name,''), nullif(campaign_name,''), 'Unknown') as name,
               sum(attributed_revenue) as rev,
               sum(clicks) as cl
        from omnisend_sends
        where date >= %s::date and (flow_name != '' or campaign_name != '')
        group by 1
        order by rev desc
        limit 5
        """,
        (cutoff.date(),),
    ).fetchall()

    rev_share = None
    if attributed and total_revenue and total_revenue > 0:
        rev_share = Decimal("100") * attributed / total_revenue

    return {
        "sends": sends or 0,
        "opens": opens or 0,
        "clicks": clicks or 0,
        "open_rate": round(100 * opens / sends, 1) if sends else None,
        "click_rate": round(100 * clicks / sends, 1) if sends else None,
        "attributed_revenue": attributed,
        "revenue_share": rev_share,
        "top_flows": [{"name": r[0], "revenue": r[1], "clicks": r[2]} for r in top_flows_rows],
        "has_data": True,
    }


# ── Data quality stats ────────────────────────────────────────────────────────

def data_quality_stats(conn: psycopg.Connection) -> dict:
    """Returns data quality indicators:
    - orders_no_customer_pct: % orders with customer_id IS NULL
    - orphan_sub_rebills: count of subscription_revenue rows where customer_id not in customers
    - last_sync_per_source: dict of {source_key: last_synced_at} from sync_state
    - total_orders: total order count
    - total_subs: total subscription_revenue count
    """
    total_orders = conn.execute("select count(*) from orders").fetchone()[0] or 0
    orders_no_customer = conn.execute(
        "select count(*) from orders where customer_id is null"
    ).fetchone()[0] or 0

    orders_no_customer_pct = (
        Decimal("100") * Decimal(orders_no_customer) / Decimal(total_orders)
        if total_orders else Decimal("0")
    )

    total_subs = conn.execute("select count(*) from subscription_revenue").fetchone()[0] or 0

    orphan_sub_rebills = conn.execute(
        """
        select count(*) from subscription_revenue sr
        where sr.customer_id is not null
          and not exists (select 1 from customers c where c.id = sr.customer_id)
        """
    ).fetchone()[0] or 0

    # Try to get sync state — table may not exist yet
    # Column is `source` (not `source_key`); also pull last_error for quality page.
    last_sync_per_source: dict[str, dict] = {}
    try:
        rows = conn.execute(
            "select source, last_synced_at, last_error, last_error_at from sync_state"
        ).fetchall()
        for src, last_synced_at, last_error, last_error_at in rows:
            last_sync_per_source[src] = {
                "synced_at":    last_synced_at,
                "last_error":   last_error,
                "last_error_at": last_error_at,
            }
    except Exception:
        pass  # sync_state table not present yet (pre-migration env)

    # Derive age strings for the three live ingest sources.
    # ga4_funnel is shown as "not wired" until Phase D credentials are supplied.
    now = _utcnow()
    SOURCES = ["shopify_orders", "meta_ad_spend", "recharge_charges", "ga4_funnel"]
    source_rows = []
    for src in SOURCES:
        entry     = last_sync_per_source.get(src, {})
        synced_at = entry.get("synced_at")
        last_error  = entry.get("last_error")
        error_at    = entry.get("last_error_at")
        if synced_at is None:
            age_minutes = None
            age_str = "Never"
            status = "never"
        else:
            diff = now - synced_at
            age_minutes = int(diff.total_seconds() / 60)
            if age_minutes < 60:
                age_str = f"{age_minutes} min ago"
            elif age_minutes < 1440:
                age_str = f"{age_minutes // 60}h ago"
            else:
                age_str = f"{age_minutes // 1440}d ago"
            if age_minutes > 1440:
                status = "red"
            elif age_minutes > 240:
                status = "amber"
            else:
                status = "ok"
        source_rows.append({
            "source":       src,
            "synced_at":    synced_at,
            "age_str":      age_str,
            "status":       status,
            "last_error":   last_error,
            "last_error_at": error_at,
        })

    return {
        "total_orders": total_orders,
        "orders_no_customer": orders_no_customer,
        "orders_no_customer_pct": orders_no_customer_pct,
        "orphan_sub_rebills": orphan_sub_rebills,
        "total_subs": total_subs,
        "last_sync_per_source": last_sync_per_source,
        "source_rows": source_rows,
    }


def subscription_mrr_recognized_and_cash(conn: psycopg.Connection) -> dict:
    """For the current calendar month: mrr_recognized and cash_collected."""
    now = _utcnow()
    year, month = now.year, now.month
    mrr = mrr_recognized(conn, year, month) or Decimal("0")
    cash = cash_collected_in_month(conn, year, month) or Decimal("0")
    return {"mrr_recognized": mrr, "cash_collected": cash}


def serum_vs_capsules_ltv(conn: psycopg.Connection) -> dict:
    """Compares 12m LTV for serum-only vs serum+capsules subscribers.
    Returns dict with serum_only and serum_capsules sub-dicts."""
    now = _utcnow()
    cutoff_cohort = now - timedelta(days=365)

    settings = {
        r[0]: Decimal(str(r[1]))
        for r in conn.execute("select key, value from cost_settings").fetchall()
    }
    shipping = settings.get("shipping_cost_per_order", Decimal("0"))
    payment_fee_pct = settings.get("payment_fee_pct", Decimal("0"))

    def _compute_ltv(customer_ids: list) -> Optional[Decimal]:
        if not customer_ids:
            return None
        placeholders = ",".join(["%s"] * len(customer_ids))
        order_rows = conn.execute(
            f"""
            select o.total, o.refunded, o.discount_amount, o.line_items
            from orders o
            join customers c on c.id = o.customer_id
            where c.id in ({placeholders})
              and c.first_order_at < %s
              and o.created_at >= c.first_order_at
              and o.created_at < c.first_order_at + interval '12 months'
            """,
            tuple(customer_ids) + (cutoff_cohort,),
        ).fetchall()
        total_gp = Decimal("0")
        for total, refunded, discount_amount, line_items in order_rows:
            discount_amount = discount_amount or Decimal("0")
            line_items = line_items or []
            cogs = Decimal("0")
            for item in (line_items if isinstance(line_items, list) else []):
                sku = item.get("sku") if isinstance(item, dict) else None
                qty = Decimal(str(item.get("quantity", 1))) if isinstance(item, dict) else Decimal("1")
                if sku:
                    cogs_row = conn.execute(
                        "select cogs_per_unit from cost_inputs where sku = %s", (sku,)
                    ).fetchone()
                    if cogs_row:
                        cogs += qty * cogs_row[0]
            gp = (total - refunded - discount_amount) - cogs - shipping - (total * payment_fee_pct)
            total_gp += gp
        return total_gp / Decimal(len(customer_ids)) if customer_ids else None

    # Serum-only: sub orders that ONLY contain HAIR-SERUM-50ML
    serum_only_custs = conn.execute(
        """
        select distinct o.customer_id
        from orders o
        where o.is_subscription_order = true
          and o.customer_id is not null
          and not exists (
            select 1 from jsonb_array_elements(o.line_items) li
            where li->>'sku' != 'HAIR-SERUM-50ML'
          )
          and exists (
            select 1 from jsonb_array_elements(o.line_items) li
            where li->>'sku' = 'HAIR-SERUM-50ML'
          )
        """
    ).fetchall()
    serum_only_ids = [r[0] for r in serum_only_custs]

    # Serum + capsules: sub orders with DSL-CAPS-90 in any order
    serum_caps_custs = conn.execute(
        """
        select distinct o.customer_id
        from orders o
        where o.is_subscription_order = true
          and o.customer_id is not null
          and exists (
            select 1 from jsonb_array_elements(o.line_items) li
            where li->>'sku' = 'DSL-CAPS-90'
          )
        """
    ).fetchall()
    serum_caps_ids = [r[0] for r in serum_caps_custs]

    serum_only_ltv = _compute_ltv(serum_only_ids)
    serum_caps_ltv = _compute_ltv(serum_caps_ids)

    delta = None
    delta_pct = None
    if serum_only_ltv is not None and serum_caps_ltv is not None and serum_only_ltv > 0:
        delta = serum_caps_ltv - serum_only_ltv
        delta_pct = round(float(delta / serum_only_ltv * 100), 1)

    return {
        "serum_only": {"ltv": serum_only_ltv, "count": len(serum_only_ids)},
        "serum_capsules": {"ltv": serum_caps_ltv, "count": len(serum_caps_ids)},
        "delta": delta,
        "delta_pct": delta_pct,
    }
