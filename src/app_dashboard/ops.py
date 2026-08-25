"""Is the pipeline actually running?

Every number on the scoreboard is only as fresh as the last successful ingest,
and a stalled scheduler looks exactly like a quiet week: the charts keep
rendering, they just stop changing. The staleness rule lives here once.
"""

import logging
from datetime import datetime, timezone

import httpx

from app_dashboard.pipeline import SYNC_SOURCES

logger = logging.getLogger(__name__)

# Staleness thresholds in minutes.
PAGE_STALE_MINUTES = 120
ALERT_STALE_MINUTES = 180


def sync_health(conn, poll_interval_minutes: int = 60) -> dict:
    """Return per-source sync health plus an overall (worst-case) summary.

    The stale banner on the overview uses the top-level keys:
        last_synced_at, age_minutes, stale, page_threshold_minutes.

    The quality page additionally reads `sources` for the per-source table.
    """
    now = datetime.now(timezone.utc)
    sources: dict[str, dict] = {}
    oldest_last: datetime | None = None

    for src in SYNC_SOURCES:
        row = conn.execute(
            """
            select last_synced_at, last_error, last_error_at
            from sync_state
            where source = %s
            """,
            (src,),
        ).fetchone()
        last        = row[0] if row else None
        last_error  = row[1] if row else None
        error_at    = row[2] if row else None
        age: float | None = None
        if last is not None:
            age = (now - last).total_seconds() / 60
            if oldest_last is None or last < oldest_last:
                oldest_last = last

        sources[src] = {
            "last_synced_at": last,
            "age_minutes":    None if age is None else round(age),
            "stale":          age is None or age > PAGE_STALE_MINUTES,
            "last_error":     last_error,
            "last_error_at":  error_at,
        }

    # Overall: stale if ANY source is stale; age = worst (oldest) source.
    overall_stale = any(s["stale"] for s in sources.values())
    overall_age   = max(
        (s["age_minutes"] for s in sources.values() if s["age_minutes"] is not None),
        default=None,
    )

    return {
        # Top-level keys consumed by overview template and stale-check job
        "last_synced_at":         oldest_last,
        "age_minutes":            overall_age,
        "stale":                  overall_stale,
        "page_threshold_minutes": PAGE_STALE_MINUTES,
        # Per-source detail consumed by quality page
        "sources":                sources,
    }


def build_stale_message(age_minutes: int | None, base_url: str,
                        dashboard_name: str = "Densologie Scoreboard") -> dict:
    when = "never" if age_minutes is None else f"{age_minutes} minutes ago"
    return {"text": (
        f":rotating_light: {dashboard_name} ingest is stale. Last run: {when}. "
        f"Numbers on {base_url} are frozen until it recovers."
    )}


def check_stale_sync(conn, settings, http_post=httpx.post) -> bool:
    """Warn once per stale episode, not once per poll.

    Reads from all SYNC_SOURCES; alerts if any source exceeds ALERT_STALE_MINUTES.
    """
    now = datetime.now(timezone.utc)
    worst_age: float | None = None

    for src in SYNC_SOURCES:
        row = conn.execute(
            "select last_synced_at from sync_state where source = %s", (src,)
        ).fetchone()
        last = row[0] if row else None
        age = (now - last).total_seconds() / 60 if last else None
        if age is None or (worst_age is not None and age > worst_age) or worst_age is None:
            worst_age = age

    if worst_age is not None and worst_age <= ALERT_STALE_MINUTES:
        return False

    if not settings.slack_webhook_url:
        logger.warning("ingest is stale but SLACK_WEBHOOK_URL is unset")
        return False

    from app_dashboard.slack import post_alert

    payload = build_stale_message(
        None if worst_age is None else round(worst_age),
        settings.public_base_url,
        settings.dashboard_name,
    )
    return post_alert(settings.slack_webhook_url, payload, http_post=http_post)
