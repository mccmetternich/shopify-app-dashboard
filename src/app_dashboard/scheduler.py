"""Background scheduler — Phase B ingest jobs.

Ingest jobs are NO-OPS (log a warning, don't crash) if the corresponding token
is empty, so the dashboard can run in demo mode without credentials.

Job cadence (all configurable via settings.*_poll_interval_minutes):
  - shopify_sync:         every 15 min — orders + customers from Shopify Admin API
  - meta_sync:            every 15 min — ad spend from Meta Marketing API
  - recharge_sync:        every 15 min — subscription charges from Recharge
  - subscription_snapshot: daily at midnight store time — subscription state snapshot
  - stale_check:          every 60 min — Slack alert if ingest is stale
  - weekly_digest:        cron         — Slack weekly summary
"""

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from app_dashboard.digest import send_weekly_digest
from app_dashboard.ops import check_stale_sync
from app_dashboard.pipeline import SOURCE_SHOPIFY, SOURCE_META, SOURCE_RECHARGE

logger = logging.getLogger(__name__)

WEEKLY_DIGEST_JOB_ID = "weekly_digest"


# ── Error recording ────────────────────────────────────────────────────────────

def _record_sync_error(conn_factory, source: str, exc: Exception) -> None:
    """Write the last error message to sync_state for the given source.

    Called in each ingest job's except block so the quality page and stale
    banner can surface 'last failed at X with message Y' rather than just
    showing the last-success age going amber/red with no context.
    """
    try:
        conn = conn_factory()
        try:
            conn.execute(
                """
                insert into sync_state (source, last_error, last_error_at)
                values (%s, %s, now())
                on conflict (source) do update set
                    last_error    = excluded.last_error,
                    last_error_at = excluded.last_error_at
                """,
                (source, str(exc)[:500]),  # cap at 500 chars
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        # Never let error-recording crash the scheduler
        logger.exception("failed to record sync error for source %s", source)


def _clear_sync_error(conn_factory, source: str) -> None:
    """Clear last_error after a successful sync so the quality page goes green."""
    try:
        conn = conn_factory()
        try:
            conn.execute(
                """
                update sync_state set last_error = null, last_error_at = null
                where source = %s and last_error is not null
                """,
                (source,),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception("failed to clear sync error for source %s", source)


# ── Operational jobs ──────────────────────────────────────────────────────────

def run_stale_check_job(conn_factory, settings) -> None:
    conn = conn_factory()
    try:
        check_stale_sync(conn, settings)
    except Exception:
        logger.exception("stale-sync check failed")
    finally:
        conn.close()


def run_digest_job(conn_factory, settings) -> None:
    conn = conn_factory()
    try:
        if send_weekly_digest(conn, settings):
            logger.info("posted weekly digest")
    except Exception:
        logger.exception("weekly digest failed")
    finally:
        conn.close()


def run_snapshot_job(conn_factory, settings) -> None:
    """Write today's subscription state snapshot. Always runs — no token needed."""
    from app_dashboard.snapshot import take_subscription_snapshot
    conn = conn_factory()
    try:
        take_subscription_snapshot(conn)
        logger.info("subscription_snapshot: complete")
    except Exception as exc:
        logger.exception("subscription_snapshot failed")
        # Snapshot errors don't need a sync_state record (no API involved)
    finally:
        conn.close()


# ── Ingest jobs ───────────────────────────────────────────────────────────────

def run_shopify_sync_job(conn_factory, settings) -> None:
    """Sync Shopify orders. NO-OP if shopify_admin_token is unset."""
    if not settings.shopify_admin_token:
        logger.warning(
            "shopify_sync: SHOPIFY_ADMIN_TOKEN is not set — skipping. "
            "Set SHOPIFY_ADMIN_TOKEN + SHOPIFY_SHOP_DOMAIN to enable live ingest."
        )
        return
    from app_dashboard.shopify_admin import ShopifyAdminClient
    from app_dashboard.ingest_shopify import sync_orders
    conn = conn_factory()
    try:
        with ShopifyAdminClient(
            shop_domain=settings.shopify_shop_domain,
            access_token=settings.shopify_admin_token,
        ) as client:
            n = sync_orders(conn, client)
            logger.info("shopify_sync: %d orders upserted", n)
        _clear_sync_error(conn_factory, SOURCE_SHOPIFY)
    except Exception as exc:
        logger.exception("shopify_sync failed")
        _record_sync_error(conn_factory, SOURCE_SHOPIFY, exc)
    finally:
        conn.close()


def run_meta_sync_job(conn_factory, settings) -> None:
    """Sync Meta ad spend. NO-OP if meta_access_token is unset."""
    if not settings.meta_access_token:
        logger.warning(
            "meta_sync: META_ACCESS_TOKEN is not set — skipping. "
            "Set META_ACCESS_TOKEN + META_ACCOUNT_ID to enable live ingest."
        )
        return
    from app_dashboard.meta_insights import MetaInsightsClient
    from app_dashboard.ingest_meta import sync_ad_spend
    conn = conn_factory()
    try:
        with MetaInsightsClient(
            account_id=settings.meta_account_id,
            access_token=settings.meta_access_token,
        ) as client:
            n = sync_ad_spend(conn, client,
                              lookback_days=settings.meta_poll_interval_minutes)
            logger.info("meta_sync: %d ad_spend rows upserted", n)
        _clear_sync_error(conn_factory, SOURCE_META)
    except Exception as exc:
        logger.exception("meta_sync failed")
        _record_sync_error(conn_factory, SOURCE_META, exc)
    finally:
        conn.close()


def run_recharge_sync_job(conn_factory, settings) -> None:
    """Sync Recharge subscription charges + lifecycle events. NO-OP if token unset."""
    if not settings.recharge_api_token:
        logger.warning(
            "recharge_sync: RECHARGE_API_TOKEN is not set — skipping. "
            "Set RECHARGE_API_TOKEN to enable live subscription ingest."
        )
        return
    from app_dashboard.recharge import RechargeClient
    from app_dashboard.ingest_recharge import sync_subscription_revenue, sync_subscription_events
    conn = conn_factory()
    try:
        with RechargeClient(api_token=settings.recharge_api_token) as client:
            n = sync_subscription_revenue(conn, client)
            logger.info("recharge_sync: %d subscription rows upserted", n)
            # Event emission runs after charges so subscription_revenue is populated.
            e = sync_subscription_events(
                conn, client,
                poll_interval_minutes=settings.recharge_poll_interval_minutes,
            )
            logger.info("recharge_sync: %d lifecycle events written", e)
        _clear_sync_error(conn_factory, SOURCE_RECHARGE)
    except Exception as exc:
        logger.exception("recharge_sync failed")
        _record_sync_error(conn_factory, SOURCE_RECHARGE, exc)
    finally:
        conn.close()


# ── Scheduler start ───────────────────────────────────────────────────────────

def start_scheduler(conn_factory, settings) -> BackgroundScheduler:
    """Start all background jobs. Ingest jobs are NO-OPS when tokens are unset."""
    scheduler = BackgroundScheduler()

    # --- Operational jobs (always run) -------------------------------------
    scheduler.add_job(
        lambda: run_stale_check_job(conn_factory, settings),
        "interval",
        minutes=60,
        next_run_time=datetime.now(),
    )

    scheduler.add_job(
        lambda: run_digest_job(conn_factory, settings),
        "cron",
        day_of_week=settings.digest_day_of_week,
        hour=settings.digest_hour,
        minute=0,
        timezone=settings.digest_timezone,
        id=WEEKLY_DIGEST_JOB_ID,
    )

    # --- Subscription snapshot (always run — no API token needed) ----------
    # Runs at 00:05 store time so it captures the full previous day.
    # next_run_time=datetime.now() also takes an immediate snapshot on startup
    # so the first row is written the moment the process comes up.
    scheduler.add_job(
        lambda: run_snapshot_job(conn_factory, settings),
        "cron",
        hour=0,
        minute=5,
        timezone=settings.store_timezone,
        next_run_time=datetime.now(),
        id="subscription_snapshot",
    )

    # --- Ingest jobs (NO-OP when token is empty) ---------------------------
    scheduler.add_job(
        lambda: run_shopify_sync_job(conn_factory, settings),
        "interval",
        minutes=settings.shopify_poll_interval_minutes,
        id="shopify_sync",
    )

    scheduler.add_job(
        lambda: run_meta_sync_job(conn_factory, settings),
        "interval",
        minutes=settings.meta_poll_interval_minutes,
        id="meta_sync",
    )

    scheduler.add_job(
        lambda: run_recharge_sync_job(conn_factory, settings),
        "interval",
        minutes=settings.recharge_poll_interval_minutes,
        id="recharge_sync",
    )

    scheduler.start()
    return scheduler
