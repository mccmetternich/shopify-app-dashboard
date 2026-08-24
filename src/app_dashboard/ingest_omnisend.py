"""Omnisend API ingest — polls flow and campaign metrics into omnisend_sends.

Required secret (Phase D):
  OMNISEND_API_KEY — from Omnisend > Settings > API keys

Endpoints used:
  GET /v3/reports/flows    — flow performance metrics
  GET /v3/reports/campaigns — campaign performance metrics
Lookback: 7 days on every run.
"""
from __future__ import annotations
import logging
from datetime import date, timedelta

import psycopg

logger = logging.getLogger(__name__)

OMNISEND_BASE = "https://api.omnisend.com/v3"


def ingest_omnisend(conn: psycopg.Connection, settings) -> int:
    """Poll Omnisend and upsert send metrics. Returns rows written.
    Stub — requires OMNISEND_API_KEY.
    """
    api_key = getattr(settings, "omnisend_api_key", None)
    if not api_key:
        logger.debug("OMNISEND_API_KEY not set — skipping Omnisend ingest")
        return 0

    try:
        import httpx
    except ImportError:
        logger.warning("httpx not installed")
        return 0

    end = date.today()
    start = end - timedelta(days=7)
    headers = {"X-API-KEY": api_key}
    written = 0

    # Flows
    try:
        r = httpx.get(
            f"{OMNISEND_BASE}/reports/flows",
            headers=headers,
            params={"startDate": str(start), "endDate": str(end)},
            timeout=30,
        )
        r.raise_for_status()
        for flow in r.json().get("data", []):
            _upsert_omnisend_row(conn, end, flow_name=flow.get("name"), data=flow)
            written += 1
    except Exception as e:
        logger.warning("Omnisend flows ingest failed: %s", e)

    # Campaigns
    try:
        r = httpx.get(
            f"{OMNISEND_BASE}/reports/campaigns",
            headers=headers,
            params={"startDate": str(start), "endDate": str(end)},
            timeout=30,
        )
        r.raise_for_status()
        for camp in r.json().get("data", []):
            _upsert_omnisend_row(conn, end, campaign_name=camp.get("name"), data=camp)
            written += 1
    except Exception as e:
        logger.warning("Omnisend campaigns ingest failed: %s", e)

    conn.commit()
    logger.info("Omnisend: wrote %d rows", written)
    return written


def _upsert_omnisend_row(conn, day, data, flow_name=None, campaign_name=None):
    stats = data.get("statistics", data)
    conn.execute(
        """
        insert into omnisend_sends (date, flow_name, campaign_name, channel, sends, opens, clicks, attributed_revenue)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (date, flow_name, campaign_name, channel)
        do update set
            sends = excluded.sends,
            opens = excluded.opens,
            clicks = excluded.clicks,
            attributed_revenue = excluded.attributed_revenue
        """,
        (
            day, flow_name or "", campaign_name or "", "email",
            stats.get("sent", 0),
            stats.get("opened", 0),
            stats.get("clicked", 0),
            stats.get("revenue", 0),
        ),
    )
