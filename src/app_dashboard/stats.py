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
        subscription_share = Decimal("100") * Decimal(subs_in_window) / Decimal(new_customers)
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
