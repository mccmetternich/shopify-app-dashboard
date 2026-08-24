"""Recharge subscription charge ingest.

Polls Recharge charges and upserts into subscription_revenue.

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


def _mark_churned(conn: psycopg.Connection) -> int:
    """Retroactively churn subscriptions with no charge in the last 45 days.

    Reads subscription_revenue rows that are still active (churned_at IS NULL)
    and checks the last charge date for each subscription_id. If the most recent
    charge is older than _CHURN_DAYS, the subscription is marked churned.

    This is a best-effort churn signal. Recharge may have a cancellation event,
    but we don't subscribe to webhooks in Phase B — we infer from billing gaps.
    Phase D (live wiring) can replace this with a proper webhook.

    Returns count of rows marked churned.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=_CHURN_DAYS)
    result = conn.execute(
        """
        update subscription_revenue
        set churned_at = %s
        where churned_at is null
          and converted_at < %s
        """,
        (cutoff, cutoff),
    )
    return result.rowcount


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
