"""Meta Marketing API ad-level ingest.

Required secrets (Phase D):
  META_ACCESS_TOKEN  — long-lived system user token with ads_read permission
  META_ACCOUNT_ID    — ad account ID (act_XXXXXXXXX)

Fetches campaign/adset/ad breakdown at daily level for the last 7 days
(Meta retroactively adjusts spend up to 28 days).

Fields: spend, impressions, clicks, actions[purchase], action_values[purchase],
        creative.thumbnail_url, permalink_url
"""
from __future__ import annotations
import logging
from datetime import date, timedelta
import psycopg

logger = logging.getLogger(__name__)
META_API_BASE = "https://graph.facebook.com/v19.0"


def ingest_meta_ads(conn: psycopg.Connection, settings) -> int:
    token = getattr(settings, "meta_access_token", None)
    account_id = getattr(settings, "meta_account_id", None)
    if not token or not account_id:
        logger.debug("META_ACCESS_TOKEN/ACCOUNT_ID not set — skipping ad-level ingest")
        return 0
    try:
        import httpx
    except ImportError:
        return 0

    end = date.today()
    start = end - timedelta(days=7)
    params = {
        "access_token": token,
        "level": "ad",
        "time_range": f'{{"since":"{start}","until":"{end}"}}',
        "time_increment": 1,
        "fields": "campaign_id,campaign_name,adset_id,adset_name,ad_id,ad_name,"
                  "spend,impressions,clicks,actions,action_values,date_start",
        "limit": 500,
    }
    try:
        r = httpx.get(f"{META_API_BASE}/{account_id}/insights", params=params, timeout=30)
        r.raise_for_status()
    except Exception as e:
        logger.error("Meta ads ingest failed: %s", e)
        return 0

    written = 0
    for row in r.json().get("data", []):
        day = date.fromisoformat(row["date_start"])
        purchases = next((int(a["value"]) for a in row.get("actions", []) if a["action_type"] == "purchase"), 0)
        purchase_value = next((float(a["value"]) for a in row.get("action_values", []) if a["action_type"] == "purchase"), 0)
        conn.execute(
            """
            insert into meta_ad_stats
            (date, campaign_id, campaign_name, adset_id, adset_name, ad_id, ad_name,
             spend, impressions, clicks, purchases, purchase_value)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict (date, campaign_id, adset_id, ad_id)
            do update set spend=excluded.spend, impressions=excluded.impressions,
                clicks=excluded.clicks, purchases=excluded.purchases,
                purchase_value=excluded.purchase_value
            """,
            (day, row["campaign_id"], row["campaign_name"],
             row.get("adset_id", ""), row.get("adset_name", ""),
             row.get("ad_id", ""), row.get("ad_name", ""),
             float(row["spend"]), int(row["impressions"]), int(row["clicks"]),
             purchases, purchase_value),
        )
        written += 1
    conn.commit()
    logger.info("Meta ads: wrote %d rows", written)
    return written
