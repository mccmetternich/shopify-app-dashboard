"""Meta ad spend ingest — fetches daily campaign spend and upserts into ad_spend.

Date timezone note:
  ad_spend.date stores the campaign date IN THE STORE'S LOCAL TIMEZONE
  (default: America/Los_Angeles, configurable via settings.store_timezone).
  Meta returns dates in the account's timezone (set in Meta Business Manager,
  which should match the store's timezone). We do not re-convert the date —
  we store what Meta reports as the date for that day's spend.

  This means a campaign that ran 2026-08-22 23:00–23:59 PT is recorded as
  2026-08-22 in ad_spend, not 2026-08-23 UTC. This is the standard for DTC
  reporting: always report in the store timezone so numbers match the Business
  Manager dashboard.

  Consequence: when joining ad_spend to orders (which are UTC), convert
  orders.created_at to store timezone before grouping by date.
"""

import logging
from datetime import date, timedelta

import psycopg

from app_dashboard.meta_insights import MetaInsightsClient

logger = logging.getLogger(__name__)

_SYNC_SOURCE = "meta_ad_spend"


def sync_ad_spend(
    conn: psycopg.Connection,
    client: MetaInsightsClient,
    lookback_days: int = 7,
) -> int:
    """Fetch the last `lookback_days` of ad spend and upsert into ad_spend.

    Uses a lookback window rather than a cursor because Meta retroactively
    adjusts spend figures up to 28 days after the fact (audience deduplication,
    invalid traffic credits). A 7-day lookback reprocesses recent rows to pick
    up any adjustments without fetching the full history on every poll.

    ON CONFLICT(date, campaign_id) DO UPDATE spend: replaces the stored figure
    with the freshest Meta value. impressions and clicks are also updated.

    Returns the count of rows inserted or updated.

    Raises RuntimeError on any API error (never silently returns $0 spend).
    """
    today = date.today()
    date_start = today - timedelta(days=lookback_days - 1)
    date_end = today

    logger.info(
        "sync_ad_spend: fetching %s → %s (%d days)",
        date_start.isoformat(),
        date_end.isoformat(),
        lookback_days,
    )

    rows = client.fetch_daily_spend(date_start=date_start, date_end=date_end)

    if not rows:
        logger.info("sync_ad_spend: no rows returned from Meta")
        _mark_synced(conn)
        return 0

    upserted = 0
    with conn.transaction():
        for row in rows:
            # platform defaults to 'meta' since this is the Meta client.
            # If we add a Google or TikTok client later, they get their own
            # ingest_google.py / ingest_tiktok.py with their own platform values.
            result = conn.execute(
                """
                insert into ad_spend
                    (date, campaign_id, campaign_name, platform, spend,
                     impressions, clicks)
                values
                    (%(date)s, %(campaign_id)s, %(campaign_name)s, 'meta',
                     %(spend)s, %(impressions)s, %(clicks)s)
                on conflict (date, campaign_id) do update set
                    spend        = excluded.spend,
                    campaign_name = excluded.campaign_name,
                    impressions  = excluded.impressions,
                    clicks       = excluded.clicks
                """,
                {
                    "date": row["date"],
                    "campaign_id": row["campaign_id"],
                    "campaign_name": row["campaign_name"],
                    "spend": row["spend"],
                    # Meta Insights basic endpoint does not return impressions/
                    # clicks unless added to fields. They are optional here.
                    "impressions": row.get("impressions"),
                    "clicks": row.get("clicks"),
                },
            )
            upserted += result.rowcount

        _mark_synced(conn)

    logger.info("sync_ad_spend: %d rows upserted", upserted)
    return upserted


def _mark_synced(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        insert into sync_state (source, last_synced_at)
        values (%s, now())
        on conflict (source) do update set last_synced_at = excluded.last_synced_at
        """,
        (_SYNC_SOURCE,),
    )
