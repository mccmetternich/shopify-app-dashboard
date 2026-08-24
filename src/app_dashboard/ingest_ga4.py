"""GA4 Data API ingest — polls session/funnel metrics into ga4_funnel table.

Required secrets (Phase D):
  GA4_PROPERTY_ID              — numeric property ID from GA4 Admin > Property Settings
  GOOGLE_APPLICATION_CREDENTIALS — path to service account JSON file, OR
  GOOGLE_SERVICE_ACCOUNT_JSON  — JSON content as env var (for Fly secrets)

The service account needs "Viewer" role on the GA4 property.
Metrics fetched: sessions, addToCarts, checkouts, transactions
Dimensions: date, sessionSource, sessionMedium
Lookback: 3 days on every run (GA4 data can arrive late).
"""
from __future__ import annotations
import logging
from datetime import date, timedelta

import psycopg

logger = logging.getLogger(__name__)


def ingest_ga4(conn: psycopg.Connection, settings) -> int:
    """Poll GA4 and upsert funnel data. Returns rows written.
    Stub — requires GA4_PROPERTY_ID and GOOGLE_APPLICATION_CREDENTIALS.
    """
    prop_id = getattr(settings, "ga4_property_id", None)
    if not prop_id:
        logger.debug("GA4_PROPERTY_ID not set — skipping funnel ingest")
        return 0

    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import (
            DateRange, Dimension, Metric, RunReportRequest,
        )
    except ImportError:
        logger.warning("google-analytics-data not installed")
        return 0

    client = BetaAnalyticsDataClient()
    end = date.today()
    start = end - timedelta(days=3)

    request = RunReportRequest(
        property=f"properties/{prop_id}",
        date_ranges=[DateRange(start_date=str(start), end_date=str(end))],
        dimensions=[
            Dimension(name="date"),
            Dimension(name="sessionSource"),
            Dimension(name="sessionMedium"),
        ],
        metrics=[
            Metric(name="sessions"),
            Metric(name="addToCarts"),
            Metric(name="checkouts"),
            Metric(name="transactions"),
        ],
    )

    response = client.run_report(request)
    written = 0
    for row in response.rows:
        d = row.dimension_values
        m = row.metric_values
        day = date.fromisoformat(d[0].value.replace(":", "-"))
        source = d[1].value if d[1].value not in ("(none)", "(direct)") else ""
        medium = d[2].value if d[2].value != "(none)" else ""
        conn.execute(
            """
            insert into ga4_funnel (date, utm_source, utm_medium, sessions, add_to_carts, begin_checkouts, purchases)
            values (%s, %s, %s, %s, %s, %s, %s)
            on conflict (date, utm_source, utm_medium)
            do update set
                sessions = excluded.sessions,
                add_to_carts = excluded.add_to_carts,
                begin_checkouts = excluded.begin_checkouts,
                purchases = excluded.purchases
            """,
            (day, source, medium, int(m[0].value), int(m[1].value), int(m[2].value), int(m[3].value)),
        )
        written += 1
    conn.commit()
    logger.info("GA4 funnel: wrote %d rows", written)
    return written
