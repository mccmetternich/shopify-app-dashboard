"""Background scheduler — Phase B ingest jobs.

Ingest jobs are NO-OPS (log a warning, don't crash) if the corresponding token
is empty, so the dashboard can run in demo mode without credentials.

Job cadence (all configurable via settings.*_poll_interval_minutes):
  - shopify_sync:   every 15 min — orders + customers from Shopify Admin API
  - meta_sync:      every 15 min — ad spend from Meta Marketing API
  - recharge_sync:  every 15 min — subscription charges from Recharge
  - stale_check:    every 60 min — Slack alert if ingest is stale
  - weekly_digest:  cron        — Slack weekly summary
"""

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from app_dashboard.digest import send_weekly_digest
from app_dashboard.ops import check_stale_sync

logger = logging.getLogger(__name__)

WEEKLY_DIGEST_JOB_ID = "weekly_digest"


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
    except Exception:
        logger.exception("shopify_sync failed")
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
    except Exception:
        logger.exception("meta_sync failed")
    finally:
        conn.close()


def run_recharge_sync_job(conn_factory, settings) -> None:
    """Sync Recharge subscription charges. NO-OP if recharge_api_token is unset."""
    if not settings.recharge_api_token:
        logger.warning(
            "recharge_sync: RECHARGE_API_TOKEN is not set — skipping. "
            "Set RECHARGE_API_TOKEN to enable live subscription ingest."
        )
        return
    from app_dashboard.recharge import RechargeClient
    from app_dashboard.ingest_recharge import sync_subscription_revenue
    conn = conn_factory()
    try:
        with RechargeClient(api_token=settings.recharge_api_token) as client:
            n = sync_subscription_revenue(conn, client)
            logger.info("recharge_sync: %d subscription rows upserted", n)
    except Exception:
        logger.exception("recharge_sync failed")
    finally:
        conn.close()


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
