"""Is the pipeline actually running?

Every number on the scoreboard is only as fresh as the last successful ingest,
and a stalled scheduler looks exactly like a quiet week: the charts keep
rendering, they just stop changing. The staleness rule lives here once.
"""

import logging
from datetime import datetime, timezone

import httpx

from app_dashboard.pipeline import SOURCE

logger = logging.getLogger(__name__)

# Staleness thresholds in minutes.
PAGE_STALE_MINUTES = 120
ALERT_STALE_MINUTES = 180


def sync_health(conn, poll_interval_minutes: int = 60) -> dict:
    row = conn.execute(
        "select last_synced_at from sync_state where source = %s", (SOURCE,)
    ).fetchone()
    last = row[0] if row else None
    age = None
    if last is not None:
        age = (datetime.now(timezone.utc) - last).total_seconds() / 60

    return {
        "last_synced_at": last,
        "age_minutes": None if age is None else round(age),
        # A sync that has never run is stale.
        "stale": age is None or age > PAGE_STALE_MINUTES,
        "page_threshold_minutes": PAGE_STALE_MINUTES,
    }


def build_stale_message(age_minutes: int | None, base_url: str,
                        dashboard_name: str = "Densologie Scoreboard") -> dict:
    when = "never" if age_minutes is None else f"{age_minutes} minutes ago"
    return {"text": (
        f":rotating_light: {dashboard_name} ingest is stale. Last run: {when}. "
        f"Numbers on {base_url} are frozen until it recovers."
    )}


def check_stale_sync(conn, settings, http_post=httpx.post) -> bool:
    """Warn once per stale episode, not once per poll."""
    row = conn.execute(
        "select last_synced_at from sync_state where source = %s",
        (SOURCE,),
    ).fetchone()
    last = row[0] if row else None

    age = None
    if last is not None:
        age = (datetime.now(timezone.utc) - last).total_seconds() / 60

    if age is not None and age <= ALERT_STALE_MINUTES:
        return False

    if not settings.slack_webhook_url:
        logger.warning("ingest is stale but SLACK_WEBHOOK_URL is unset")
        return False

    from app_dashboard.slack import post_alert

    payload = build_stale_message(None if age is None else round(age),
                                  settings.public_base_url,
                                  settings.dashboard_name)
    return post_alert(settings.slack_webhook_url, payload, http_post=http_post)
