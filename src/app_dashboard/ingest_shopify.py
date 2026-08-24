"""Shopify order ingest — polls the Admin API and upserts into orders + customers.

C1 traps (see shopify_admin.py header for full list):
  - Refunds are fetched from totalRefundedSet on the order node itself. The
    main fetch_orders() call includes refund data; a reconciliation pass can
    call fetch_refunds() to update rows where refunds arrived post-ingest.
  - is_new_customer is set once at ingest time from customer.numberOfOrders
    and is NEVER updated on conflict (DO UPDATE excludes that column).
  - All timestamps are UTC-asserted in shopify_admin._parse_utc().
  - sync_state is updated after every successful page so a crash mid-batch
    resumes from the last good cursor, not from scratch.
"""

import json
import logging
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

from app_dashboard.shopify_admin import ShopifyAdminClient

logger = logging.getLogger(__name__)

# sync_state source key for Shopify orders.
_SYNC_SOURCE = "shopify_orders"


def _upsert_customer(conn: psycopg.Connection, customer: dict) -> None:
    """Insert a customer row if it does not already exist.

    ON CONFLICT DO NOTHING: first-seen wins for first_order_at. This matches
    the semantics of the customers table design (first_order_at is set once).
    """
    if not customer.get("id"):
        return

    # We don't have email from the GraphQL response here, so we hash the GID
    # as a stable surrogate. If we later fetch the email, a separate enrichment
    # pass can update email_hash.
    import hashlib
    email_hash = hashlib.sha256(customer["id"].encode()).hexdigest()

    conn.execute(
        """
        insert into customers (id, email_hash, first_order_at, country)
        values (%(id)s, %(email_hash)s, %(first_order_at)s, %(country)s)
        on conflict (id) do nothing
        """,
        {
            "id": customer["id"],
            "email_hash": email_hash,
            "first_order_at": customer.get("first_order_at", datetime.now(timezone.utc)),
            "country": customer.get("country"),
        },
    )


def _upsert_order(conn: psycopg.Connection, order: dict) -> int:
    """Upsert one order. Returns rowcount (1 = inserted or updated, 0 = no-op).

    ON CONFLICT DO UPDATE for total and refunded — orders can be edited or
    refunded after creation. is_new_customer is EXCLUDED from the update set:
    once set at ingest time, it must never be overwritten (C1 trap).
    """
    source_utm = order["source_utm"]
    line_items = order["line_items"]

    return conn.execute(
        """
        insert into orders
            (id, customer_id, created_at, total, refunded, currency,
             is_new_customer, line_items, source_utm)
        values
            (%(id)s, %(customer_id)s, %(created_at)s, %(total)s, %(refunded)s,
             %(currency)s, %(is_new_customer)s, %(line_items)s, %(source_utm)s)
        on conflict (id) do update set
            total     = excluded.total,
            refunded  = excluded.refunded
            -- is_new_customer intentionally NOT updated (C1 trap: first-seen wins)
        """,
        {
            "id": order["id"],
            "customer_id": order["customer_id"],
            "created_at": order["created_at"],
            "total": order["total"],
            "refunded": order["refunded"],
            "currency": order["currency"],
            "is_new_customer": order["is_new_customer"],
            "line_items": Jsonb(line_items),
            "source_utm": Jsonb(source_utm) if source_utm else None,
        },
    ).rowcount


def _update_sync_state(
    conn: psycopg.Connection,
    cursor: str | None,
    last_created_at: datetime | None,
) -> None:
    """Record successful sync progress in sync_state.

    Stores both the GraphQL pagination cursor and the timestamp of the last
    order seen. On restart, the scheduler uses the cursor first (exact resume),
    falling back to the timestamp if the cursor has expired (Shopify cursors
    expire after ~1 hour of inactivity).
    """
    meta = {}
    if cursor is not None:
        meta["shopify_cursor"] = cursor
    if last_created_at is not None:
        meta["last_order_created_at"] = last_created_at.isoformat()

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


def _load_sync_state(conn: psycopg.Connection) -> dict:
    """Load persisted sync state, or return empty dict if not yet synced."""
    row = conn.execute(
        "select meta from sync_state where source = %s",
        (_SYNC_SOURCE,),
    ).fetchone()
    if row and row[0]:
        return row[0]
    return {}


def sync_orders(
    conn: psycopg.Connection,
    client: ShopifyAdminClient,
    state: dict | None = None,
) -> int:
    """Poll Shopify for orders and upsert into orders + customers tables.

    Resumes from state['shopify_cursor'] (exact position) or
    state['last_order_created_at'] (timestamp fallback). If neither is set,
    fetches from 90 days ago.

    Returns the count of orders inserted or updated.

    CRITICAL: refunds are included in the main fetch_orders() response via
    totalRefundedSet. This covers refunds that existed at query time. For a
    full reconciliation pass (e.g. nightly), call client.fetch_refunds() on
    recently-created orders and run _upsert_order() again.

    CRITICAL: is_new_customer is derived from customer.numberOfOrders at
    ingest time in shopify_admin._normalize_order(). It is not recomputed here.

    CRITICAL: All timestamps are UTC-asserted in shopify_admin._parse_utc().
    """
    if state is None:
        state = _load_sync_state(conn)

    # Determine where to start. Cursor takes priority; it resumes exactly where
    # the last page left off. Timestamp is the fallback for post-expiry restarts.
    cursor: str | None = state.get("shopify_cursor")
    last_created_at_str: str | None = state.get("last_order_created_at")

    if last_created_at_str:
        created_at_min = datetime.fromisoformat(last_created_at_str)
    else:
        # Default: 90 days back for initial sync.
        from datetime import timedelta
        created_at_min = datetime.now(timezone.utc) - timedelta(days=90)

    total_upserted = 0
    last_order_ts: datetime | None = None

    logger.info(
        "sync_orders: starting from cursor=%r created_at_min=%s",
        cursor,
        created_at_min.isoformat(),
    )

    while True:
        orders, next_cursor = client.fetch_orders(
            created_at_min=created_at_min,
            cursor=cursor,
        )

        if not orders:
            break

        with conn.transaction():
            for order in orders:
                customer_raw = order.get("_customer_raw") or {}
                if customer_raw.get("id") and order.get("customer_id"):
                    # Use order's created_at as first_order_at for the customer
                    # row. ON CONFLICT DO NOTHING means first-seen wins.
                    _upsert_customer(conn, {
                        "id": order["customer_id"],
                        "first_order_at": order["created_at"],
                        "country": None,  # not available from order query
                    })
                upserted = _upsert_order(conn, order)
                total_upserted += upserted

                if last_order_ts is None or order["created_at"] > last_order_ts:
                    last_order_ts = order["created_at"]

            # Persist progress after each page so a crash mid-batch resumes
            # from the last successful page.
            _update_sync_state(conn, next_cursor, last_order_ts)

        logger.info(
            "sync_orders: page done, %d orders processed, next_cursor=%r",
            len(orders),
            next_cursor,
        )

        if next_cursor is None:
            break
        cursor = next_cursor

    logger.info("sync_orders: complete, %d total rows upserted", total_upserted)
    return total_upserted
