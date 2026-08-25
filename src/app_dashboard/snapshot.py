"""Daily subscription-state snapshot.

Writes one row to subscription_snapshots per calendar day, capturing counts
and MRR at that moment. This is the only durable record of what the
subscription population looked like on any given day — without it, churn and
retention history must be reconstructed from current state, which breaks
whenever a subscriber changes status (pause, win-back, re-cancel).

The scheduler calls `take_subscription_snapshot()` once daily, just after
midnight in the store timezone. If the process was down for a day the row is
simply missing — there is no back-fill for missed days (accept the gap rather
than fabricate data).

Usage:
    from app_dashboard.snapshot import take_subscription_snapshot
    take_subscription_snapshot(conn)
"""
from __future__ import annotations

import logging
from datetime import date

import psycopg

logger = logging.getLogger(__name__)


def take_subscription_snapshot(conn: psycopg.Connection) -> None:
    """Write today's subscription state to subscription_snapshots.

    Upsert-safe: re-running on the same day overwrites the earlier row so
    the last intraday run is the authoritative snapshot.
    """
    today = date.today()

    # Count active / paused / churned using the status column added in 029
    agg = conn.execute(
        """
        select
            count(*) filter (where status = 'active')  as active_count,
            count(*) filter (where status = 'paused')  as paused_count,
            count(*) filter (where status = 'churned') as churned_count,
            sum(monthly_amount) filter (where status = 'active') as mrr_recognized
        from subscription_revenue
        """
    ).fetchone()
    active_count   = agg[0] or 0
    paused_count   = agg[1] or 0
    churned_count  = agg[2] or 0
    mrr_recognized = agg[3]  # None is fine — represents no active subs

    # New subscriptions converted today
    new_subs = conn.execute(
        "select count(*) from subscription_revenue where converted_at::date = %s",
        (today,),
    ).fetchone()[0] or 0

    # Subscriptions that churned today
    churned_today = conn.execute(
        "select count(*) from subscription_revenue where churned_at::date = %s",
        (today,),
    ).fetchone()[0] or 0

    # Win-back / reactivation events today
    reactivations = conn.execute(
        """
        select count(*) from subscription_events
        where event_type = 'winback' and event_date = %s
        """,
        (today,),
    ).fetchone()[0] or 0

    conn.execute(
        """
        insert into subscription_snapshots
            (snapshot_date, active_count, paused_count, churned_count,
             mrr_recognized, new_subs, churned_subs, reactivations)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (snapshot_date) do update set
            active_count   = excluded.active_count,
            paused_count   = excluded.paused_count,
            churned_count  = excluded.churned_count,
            mrr_recognized = excluded.mrr_recognized,
            new_subs       = excluded.new_subs,
            churned_subs   = excluded.churned_subs,
            reactivations  = excluded.reactivations
        """,
        (today, active_count, paused_count, churned_count,
         mrr_recognized, new_subs, churned_today, reactivations),
    )
    conn.commit()
    logger.info(
        "subscription_snapshot: %s — active=%d paused=%d churned=%d new=%d "
        "churned_today=%d reactivations=%d mrr=%s",
        today, active_count, paused_count, churned_count,
        new_subs, churned_today, reactivations, mrr_recognized,
    )
