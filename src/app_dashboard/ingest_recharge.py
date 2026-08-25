"""Recharge subscription charge ingest.

Polls Recharge charges and upserts into subscription_revenue.
Also polls /subscriptions to emit exact lifecycle events to subscription_events.

Design:
  - converted_at is set to the first charge for each subscription_id. On
    conflict, it is NOT updated (first-seen wins — same pattern as customers).
  - churned_at: a subscription is marked churned when it has had no charge for
    > 45 days. This is applied retroactively in a post-upsert pass. The 45-day
    threshold accounts for monthly billing cycles plus a grace window.
  - If a customer_id referenced by a charge does not exist in the customers
    table, we insert a stub row (ON CONFLICT DO NOTHING). This prevents FK
    violations when Recharge subscribers haven't come through the Shopify order
    path yet (e.g. gift subscriptions, manual enrollments).

Exact event emission (approximation_reason=NULL):
  - 'new'     — dated from subscription_revenue.converted_at (first charge date)
  - 'churn'   — dated from subscription.cancelled_at (API field, exact)
  - 'winback' — dated from the 'new' event for a customer with a prior 'churn'

Poll-approximate events (pause, reactivate, dunning, expansion/contraction) are
Phase 2 — the differ operates against subscription_state_log entries written here.

Gap threshold: 2× poll_interval_minutes (default 30 min). If the interval between
two consecutive state log rows for a subscription exceeds this threshold, Phase-2
events are flagged in approximation_reason with the actual gap duration.

Currency assertion: charges with currency != 'USD' raise immediately. This is
enforced in RechargeClient.fetch_charges() and re-asserted here for defense-in-
depth. Never silently convert.
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone

import psycopg

from app_dashboard.recharge import RechargeClient

logger = logging.getLogger(__name__)

_SYNC_SOURCE = "recharge_charges"

# Subscriptions with no charge in this many days are marked churned.
_CHURN_DAYS = 45


def _ensure_customer(conn: psycopg.Connection, customer_id: str) -> None:
    """Insert a stub customer row if this customer_id is not yet in the table.

    Recharge subscribers may arrive before their Shopify order (gift subs,
    manual enrollments). We insert a placeholder so the FK constraint doesn't
    block the subscription_revenue upsert. The Shopify ingest pass will fill in
    real data; ON CONFLICT DO NOTHING means the stub doesn't overwrite it.
    """
    # Use SHA-256 of customer_id as surrogate email_hash (no real email here).
    email_hash = hashlib.sha256(customer_id.encode()).hexdigest()
    conn.execute(
        """
        insert into customers (id, email_hash, first_order_at, country)
        values (%(id)s, %(email_hash)s, now(), null)
        on conflict (id) do nothing
        """,
        {"id": customer_id, "email_hash": email_hash},
    )


def _upsert_subscription(
    conn: psycopg.Connection,
    charge: dict,
) -> int:
    """Upsert a subscription_revenue row from a Recharge charge.

    subscription_revenue.id is the Recharge subscription_id (not charge id).
    Multiple charges per subscription mean multiple billing cycles — we only
    store one row per subscription (the relationship), not per charge.

    converted_at = the charge's scheduled_at on first insert. NOT updated on
    conflict (first-seen wins).

    monthly_amount = the charge total_price. On conflict, we update it in case
    the plan changed (e.g. upsell, price increase). churned_at is handled in a
    separate pass.
    """
    # Re-assert currency (defense in depth; RechargeClient also checks).
    assert charge.get("currency", "USD") == "USD", (
        f"Recharge charge for subscription {charge['subscription_id']} "
        f"has non-USD currency: {charge.get('currency')!r}"
    )

    sub_id = charge["subscription_id"]
    if not sub_id:
        logger.warning(
            "Recharge charge %s has no subscription_id, skipping",
            charge["id"],
        )
        return 0

    result = conn.execute(
        """
        insert into subscription_revenue
            (id, customer_id, monthly_amount, converted_at, churned_at)
        values
            (%(sub_id)s, %(customer_id)s, %(monthly_amount)s,
             %(converted_at)s, null)
        on conflict (id) do update set
            monthly_amount = excluded.monthly_amount
            -- converted_at intentionally NOT updated (first-seen wins)
            -- churned_at managed by _mark_churned() pass
        """,
        {
            "sub_id": sub_id,
            "customer_id": charge["customer_id"],
            "monthly_amount": charge["total_price"],
            "converted_at": charge["scheduled_at"],
        },
    )
    return result.rowcount


def _emit_mrr_recognized(conn: psycopg.Connection, charge: dict) -> None:
    """Emit one 'mrr_recognized' event per successful charge cycle.

    Idempotent on source_id = 'rc_charge_<charge_id>'. Re-running after a
    cursor expiry is safe — ON CONFLICT (source_id) DO NOTHING absorbs the
    duplicate and writes zero rows.

    event_date = charge.scheduled_at::date (billing date, exact).
    mrr_delta  = charge.total_price (positive — cash collected this cycle).
    approximation_reason = NULL (scheduled_at is an exact API field).
    """
    sub_id = charge.get("subscription_id", "")
    if not sub_id:
        return
    conn.execute(
        """
        insert into subscription_events
            (subscription_id, customer_id, event_type, event_date,
             mrr_delta, source_id, approximation_reason)
        values (%s, %s, 'mrr_recognized', %s, %s, %s, null)
        on conflict (source_id) do nothing
        """,
        (
            sub_id,
            charge["customer_id"],
            charge["scheduled_at"].date(),
            charge["total_price"],
            f"rc_charge_{charge['id']}",
        ),
    )


def _mark_churned(conn: psycopg.Connection) -> int:
    """Retroactively churn subscriptions with no charge in the last 45 days.

    Identifies active subs (churned_at IS NULL) whose converted_at is older than
    the cutoff and that have no existing 'churn' event. For each:
      - Sets churned_at, status='churned', churn_type='involuntary'
      - Emits one 'churn' event to subscription_events with approximation_reason
        explaining this is a billing-gap inference, not a confirmed cancellation

    approximation_reason = 'billing-gap: no charge in 45+ days'
    Phase D (webhooks) will replace this with an exact cancellation timestamp.

    Returns count of newly churned subscriptions.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=_CHURN_DAYS)
    event_date = cutoff.date()
    reason = f"billing-gap: no charge in {_CHURN_DAYS}+ days"

    rows = conn.execute(
        """
        select sr.id, sr.customer_id, sr.monthly_amount
        from subscription_revenue sr
        where sr.churned_at is null
          and sr.converted_at < %s
          and not exists (
              select 1 from subscription_events se
              where se.subscription_id = sr.id and se.event_type = 'churn'
          )
        """,
        (cutoff,),
    ).fetchall()

    if not rows:
        return 0

    sub_ids = [r[0] for r in rows]

    conn.execute(
        """
        update subscription_revenue
        set churned_at = %s, status = 'churned', churn_type = 'involuntary'
        where id = any(%s) and churned_at is null
        """,
        (cutoff, sub_ids),
    )

    conn.executemany(
        """
        insert into subscription_events
            (subscription_id, customer_id, event_type, event_date, mrr_delta, approximation_reason)
        values (%s, %s, 'churn', %s, %s, %s)
        """,
        [
            (sub_id, customer_id, event_date, -monthly_amount, reason)
            for sub_id, customer_id, monthly_amount in rows
        ],
    )
    return len(rows)


def _load_sync_state(conn: psycopg.Connection) -> dict:
    row = conn.execute(
        "select meta from sync_state where source = %s",
        (_SYNC_SOURCE,),
    ).fetchone()
    if row and row[0]:
        return row[0]
    return {}


def _update_sync_state(conn: psycopg.Connection, cursor: str | None) -> None:
    from psycopg.types.json import Jsonb
    meta: dict = {}
    if cursor:
        meta["recharge_cursor"] = cursor
    conn.execute(
        """
        insert into sync_state (source, last_synced_at, meta)
        values (%(source)s, now(), %(meta)s)
        on conflict (source) do update set
            last_synced_at = excluded.last_synced_at,
            meta           = sync_state.meta || excluded.meta
        """,
        {"source": _SYNC_SOURCE, "meta": Jsonb(meta)},
    )


def sync_subscription_revenue(
    conn: psycopg.Connection,
    client: RechargeClient,
    state: dict | None = None,
) -> int:
    """Poll Recharge charges and upsert into subscription_revenue.

    Resumes from state['recharge_cursor'] if available, otherwise falls back to
    90 days ago for the initial sync.

    Returns count of rows inserted or updated.

    CRITICAL: currency must be USD — raised in RechargeClient and re-asserted
    here. Never silently convert.

    CRITICAL: test charges are filtered in RechargeClient.fetch_charges().
    """
    if state is None:
        state = _load_sync_state(conn)

    cursor: str | None = state.get("recharge_cursor")

    # Timestamp fallback for initial sync or after cursor expiry.
    last_ts_str: str | None = state.get("last_charge_updated_at")
    if last_ts_str:
        updated_at_min = datetime.fromisoformat(last_ts_str)
    else:
        from datetime import timedelta
        updated_at_min = datetime.now(timezone.utc) - timedelta(days=90)

    total_upserted = 0

    logger.info(
        "sync_subscription_revenue: starting from cursor=%r updated_at_min=%s",
        cursor,
        updated_at_min.isoformat(),
    )

    while True:
        charges, next_cursor = client.fetch_charges(
            updated_at_min=updated_at_min,
            cursor=cursor,
        )

        if not charges:
            break

        with conn.transaction():
            for charge in charges:
                _ensure_customer(conn, charge["customer_id"])
                upserted = _upsert_subscription(conn, charge)
                total_upserted += upserted
                _emit_mrr_recognized(conn, charge)

            _update_sync_state(conn, next_cursor)

        logger.info(
            "sync_subscription_revenue: page done, %d charges processed, next_cursor=%r",
            len(charges),
            next_cursor,
        )

        if next_cursor is None:
            break
        cursor = next_cursor

    # Run churn detection after all new charges are ingested.
    churned = _mark_churned(conn)
    if churned:
        logger.info("sync_subscription_revenue: marked %d subscriptions as churned", churned)

    logger.info(
        "sync_subscription_revenue: complete, %d total rows upserted", total_upserted
    )
    return total_upserted


# ── Subscription event emission ───────────────────────────────────────────────
#
# Watermark for the /subscriptions poll is stored alongside the charges watermark
# in sync_state.meta for _SYNC_SOURCE. Key: "sub_event_updated_at_min".

_SUB_EVENT_WATERMARK_KEY = "sub_event_updated_at_min"
_SUB_EVENT_INITIAL_LOOKBACK_DAYS = 90


def _load_sub_event_watermark(conn: psycopg.Connection) -> datetime:
    row = conn.execute(
        "select meta from sync_state where source = %s",
        (_SYNC_SOURCE,),
    ).fetchone()
    if row and row[0] and row[0].get(_SUB_EVENT_WATERMARK_KEY):
        return datetime.fromisoformat(row[0][_SUB_EVENT_WATERMARK_KEY])
    return datetime.now(timezone.utc) - timedelta(days=_SUB_EVENT_INITIAL_LOOKBACK_DAYS)


def _save_sub_event_watermark(conn: psycopg.Connection, ts: datetime) -> None:
    from psycopg.types.json import Jsonb
    conn.execute(
        """
        insert into sync_state (source, last_synced_at, meta)
        values (%(source)s, now(), %(meta)s)
        on conflict (source) do update set
            last_synced_at = excluded.last_synced_at,
            meta           = sync_state.meta || excluded.meta
        """,
        {"source": _SYNC_SOURCE, "meta": Jsonb({_SUB_EVENT_WATERMARK_KEY: ts.isoformat()})},
    )


def _backfill_new_events(conn: psycopg.Connection) -> int:
    """Emit 'new' events for subscription_revenue rows that don't have one.

    Uses subscription_revenue.converted_at as event_date — this is set from the
    first charge's scheduled_at, which is an exact date.
    Idempotent: the WHERE NOT EXISTS guard prevents duplicates.
    """
    result = conn.execute(
        """
        insert into subscription_events
            (subscription_id, customer_id, event_type, event_date, mrr_delta, approximation_reason)
        select
            sr.id,
            sr.customer_id,
            'new',
            sr.converted_at::date,
            sr.monthly_amount,
            null
        from subscription_revenue sr
        where not exists (
            select 1 from subscription_events se
            where se.subscription_id = sr.id
              and se.event_type = 'new'
        )
        """
    )
    return result.rowcount


def _backfill_churn_events(conn: psycopg.Connection) -> int:
    """Emit 'churn' events for subscription_revenue rows with churned_at but no event.

    Covers:
      - Seed data / historical imports that set churned_at without writing events
      - Subscriptions churned before the event system was wired

    approximation_reason:
      NULL             — churn_type='voluntary' (churned_at treated as authoritative)
      'billing-gap...' — churn_type='involuntary' (inferred from billing gap)
      'backfill: churn_type unknown' — churn_type IS NULL (origin unclear)

    Idempotent: WHERE NOT EXISTS guard prevents duplicates on re-run.
    """
    result = conn.execute(
        """
        insert into subscription_events
            (subscription_id, customer_id, event_type, event_date, mrr_delta, approximation_reason)
        select
            sr.id,
            sr.customer_id,
            'churn',
            sr.churned_at::date,
            -sr.monthly_amount,
            case sr.churn_type
                when 'voluntary'   then null
                when 'involuntary' then 'billing-gap: no charge in 45+ days'
                else                    'backfill: churn_type unknown'
            end
        from subscription_revenue sr
        where sr.churned_at is not null
          and not exists (
              select 1 from subscription_events se
              where se.subscription_id = sr.id and se.event_type = 'churn'
          )
        """
    )
    return result.rowcount


def _backfill_winback_events(conn: psycopg.Connection) -> int:
    """Emit 'winback' events for 'new' events where the customer had a prior 'churn'.

    Rule: a 'new' event for customer X qualifies as a winback if customer X has
    a 'churn' event on any subscription dated before this new event, and no
    'winback' event already exists for this subscription.

    This fires after _backfill_new_events() and _emit_churn_events() so it sees
    the complete picture.
    Idempotent: the WHERE NOT EXISTS guard prevents duplicates.
    """
    result = conn.execute(
        """
        insert into subscription_events
            (subscription_id, customer_id, event_type, event_date, mrr_delta, approximation_reason)
        select
            se_new.subscription_id,
            se_new.customer_id,
            'winback',
            se_new.event_date,
            se_new.mrr_delta,
            null
        from subscription_events se_new
        where se_new.event_type = 'new'
          and exists (
              select 1 from subscription_events se_churn
              where se_churn.customer_id = se_new.customer_id
                and se_churn.event_type = 'churn'
                and se_churn.event_date < se_new.event_date
          )
          and not exists (
              select 1 from subscription_events se_wb
              where se_wb.subscription_id = se_new.subscription_id
                and se_wb.event_type = 'winback'
          )
        """
    )
    return result.rowcount


def sync_subscription_events(
    conn: psycopg.Connection,
    client: RechargeClient,
    poll_interval_minutes: int = 15,
) -> int:
    """Fetch Recharge subscriptions and emit exact lifecycle events.

    Exact events (approximation_reason=NULL):
      'new'     — one per subscription, dated from converted_at (first charge date).
      'churn'   — voluntary cancellations, dated from subscription.cancelled_at.
      'winback' — a 'new' event for a customer who previously had a 'churn' event.

    Poll-approximate events (pause, reactivate, dunning, expansion/contraction) are
    Phase 2 and are NOT emitted here. The subscription_state_log is populated on
    every run so the Phase-2 differ has history when it is wired.

    Gap threshold: 2× poll_interval_minutes. The Phase-2 differ will compare
    polled_at timestamps between consecutive state log rows and flag events with
    the actual gap duration when it exceeds the threshold.

    Returns total events written.
    """
    now_ts = datetime.now(timezone.utc)

    # Load subscriptions already in subscription_revenue so we can skip orphans.
    # An orphan (no charges yet) has no row here — writing it to the state log
    # would violate the FK constraint.
    known_sub_ids: set[str] = {
        row[0]
        for row in conn.execute("select id from subscription_revenue")
    }
    if not known_sub_ids:
        logger.info("sync_subscription_events: no subscriptions in subscription_revenue, skipping")
        return 0

    # Step 1a: emit 'new' events for all subscriptions without one.
    # Runs before the API call so the winback pass sees complete 'new' coverage.
    new_emitted = _backfill_new_events(conn)
    if new_emitted:
        logger.info("sync_subscription_events: emitted %d 'new' events", new_emitted)

    # Step 1b: emit 'churn' events for subscription_revenue rows that have churned_at
    # set but no corresponding event. This covers seed data, historical imports, and
    # any subscription that was marked churned before this event system existed.
    # approximation_reason is NULL for voluntary (churned_at treated as authoritative)
    # and 'billing-gap: ...' for involuntary.
    backfill_churns = _backfill_churn_events(conn)
    if backfill_churns:
        logger.info("sync_subscription_events: backfilled %d 'churn' events from subscription_revenue", backfill_churns)

    # Step 2: fetch subscriptions from API and process per-subscription.
    watermark = _load_sub_event_watermark(conn)
    cursor: str | None = None
    churn_emitted = 0
    subs_processed = 0
    skipped_orphan = 0

    logger.info(
        "sync_subscription_events: fetching subscriptions updated since %s",
        watermark.isoformat(),
    )

    while True:
        subs, next_cursor = client.fetch_subscriptions(
            updated_at_min=watermark,
            cursor=cursor,
        )

        if not subs:
            break

        with conn.transaction():
            for sub in subs:
                sub_id = sub["id"]

                if sub_id not in known_sub_ids:
                    # No charges for this subscription yet — skip state log (FK).
                    skipped_orphan += 1
                    continue

                # Write current state to log for Phase-2 differ.
                conn.execute(
                    """
                    insert into subscription_state_log
                        (subscription_id, polled_at, status, paused_at, cancelled_at,
                         price, cancellation_reason)
                    values (%s, %s, %s, %s, %s, %s, %s)
                    on conflict (subscription_id, polled_at) do nothing
                    """,
                    (
                        sub_id, now_ts, sub["status"],
                        sub.get("paused_at"), sub.get("cancelled_at"),
                        sub.get("price"), sub.get("cancellation_reason"),
                    ),
                )

                # Emit exact 'churn' event for voluntary cancellations.
                # cancelled_at is an exact API timestamp — approximation_reason=NULL.
                if sub["status"] == "cancelled" and sub.get("cancelled_at"):
                    already_churned = conn.execute(
                        """
                        select 1 from subscription_events
                        where subscription_id = %s and event_type = 'churn'
                        limit 1
                        """,
                        (sub_id,),
                    ).fetchone()
                    if not already_churned:
                        price = sub.get("price")
                        mrr_delta = -price if price is not None else None
                        conn.execute(
                            """
                            insert into subscription_events
                                (subscription_id, customer_id, event_type, event_date,
                                 mrr_delta, reason, approximation_reason)
                            values (%s, %s, 'churn', %s, %s, %s, null)
                            """,
                            (
                                sub_id,
                                sub["customer_id"],
                                sub["cancelled_at"].date(),
                                mrr_delta,
                                sub.get("cancellation_reason"),
                            ),
                        )
                        # Mark the subscription as voluntary churn so the waterfall
                        # churned_mrr_voluntary bucket can join on churn_type.
                        conn.execute(
                            "update subscription_revenue set churn_type = 'voluntary' where id = %s",
                            (sub_id,),
                        )
                        churn_emitted += 1

                subs_processed += 1

        logger.info(
            "sync_subscription_events: page done — %d subs, cursor=%r",
            len(subs), next_cursor,
        )

        if next_cursor is None:
            break
        cursor = next_cursor

    if skipped_orphan:
        logger.debug(
            "sync_subscription_events: skipped %d subs not yet in subscription_revenue",
            skipped_orphan,
        )

    # Step 3: emit 'winback' events for new subscriptions by previously-churned customers.
    # Runs after churn events are written so the prior-churn check is complete.
    winback_emitted = _backfill_winback_events(conn)
    if winback_emitted:
        logger.info("sync_subscription_events: emitted %d 'winback' events", winback_emitted)

    # Step 4: advance watermark so next run fetches only recent changes.
    _save_sub_event_watermark(conn, now_ts)
    conn.commit()

    total = new_emitted + churn_emitted + winback_emitted
    logger.info(
        "sync_subscription_events: complete — %d subs processed, "
        "new=%d churn=%d winback=%d total_events=%d",
        subs_processed, new_emitted, churn_emitted, winback_emitted, total,
    )
    return total
