"""The Monday digest.

One Slack message a week. Six lead numbers from the Densologie schema.
"""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from app_dashboard.slack import escape

logger = logging.getLogger(__name__)

DIGEST_SOURCE = "weekly_digest"
MIN_DAYS_BETWEEN_DIGESTS = 3


def collect_digest(conn, settings, now=None) -> dict:
    """Collect the six lead numbers for the weekly digest.

    Returns:
        revenue_7d: Decimal or None
        new_customers: int
        blended_cac: Decimal or None
        mer: Decimal or None
        subscription_share: Decimal or None (0-100)
        days_of_cover: int or None
    """
    from app_dashboard.stats import days_of_cover as _days_of_cover

    now = now or datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    def scalar(sql, params=()):
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None

    revenue_7d = scalar(
        "select coalesce(sum(total - refunded), null) from orders where created_at >= %s",
        (week_ago,),
    )

    new_customers = scalar(
        "select count(distinct customer_id) from orders "
        "where is_new_customer = true and created_at >= %s",
        (week_ago,),
    ) or 0

    total_spend = scalar(
        "select coalesce(sum(spend), null) from ad_spend where date >= %s::date",
        (week_ago,),
    )

    blended_cac = (
        total_spend / Decimal(new_customers)
        if total_spend and new_customers else None
    )

    mer = (
        revenue_7d / total_spend
        if revenue_7d and total_spend and total_spend > 0 else None
    )

    subs_in_window = scalar(
        "select count(distinct customer_id) from subscription_revenue "
        "where converted_at >= %s",
        (week_ago,),
    ) or 0

    subscription_share = (
        Decimal("100") * Decimal(subs_in_window) / Decimal(new_customers)
        if new_customers else None
    )

    doc = _days_of_cover(conn, settings.serum_sku)

    return {
        "revenue_7d": revenue_7d,
        "new_customers": new_customers,
        "blended_cac": blended_cac,
        "mer": mer,
        "subscription_share": subscription_share,
        "days_of_cover": doc,
    }


def _money(value) -> str:
    if value is None:
        return "—"
    return f"${Decimal(str(value)):,.0f}"


def _pct(value) -> str:
    if value is None:
        return "—"
    return f"{float(value):.0f}%"


def _ratio(value) -> str:
    if value is None:
        return "—"
    return f"{float(value):.2f}x"


def _days(value) -> str:
    if value is None:
        return "n/a"
    return f"{value}d"


def render_digest(data: dict, app_name: str = "Densologie",
                  summary_line: str | None = None) -> str:
    """Format the weekly digest as a Slack message."""
    from app_dashboard.stats import generate_summary, overview_comparison

    doc = data["days_of_cover"]
    cover_flag = " :rotating_light:" if doc is not None and doc < 60 else ""

    # Build a quick summary line from the digest data if not provided
    if summary_line is None:
        stats_dict = {
            "revenue": data.get("revenue_7d"),
            "new_customers": data.get("new_customers", 0),
            "blended_cac": data.get("blended_cac"),
            "mer": data.get("mer"),
        }
        summary_line = generate_summary(stats_dict, {}, 7)

    lines = [
        f"*{app_name} Scoreboard — last 7 days*",
        summary_line,
        (
            f"Revenue {_money(data['revenue_7d'])}"
            f"  ·  New customers {data['new_customers']}"
            f"  ·  CAC {_money(data['blended_cac'])}"
        ),
        (
            f"MER {_ratio(data['mer'])}"
            f"  ·  Sub share {_pct(data['subscription_share'])}"
            f"  ·  Cover {_days(doc)}{cover_flag}"
        ),
    ]
    return "\n".join(lines)


def should_send(last_sent, now=None) -> bool:
    if last_sent is None:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - last_sent) >= timedelta(days=MIN_DAYS_BETWEEN_DIGESTS)


def send_weekly_digest(conn, settings, http_post=httpx.post, now=None) -> bool:
    from app_dashboard.slack import post_alert

    row = conn.execute(
        "select last_synced_at from sync_state where source = %s", (DIGEST_SOURCE,)
    ).fetchone()
    if not should_send(row[0] if row else None, now):
        logger.info("weekly digest already sent recently; skipping")
        return False
    if not settings.slack_webhook_url:
        logger.info("SLACK_WEBHOOK_URL unset; skipping weekly digest")
        return False

    text = render_digest(collect_digest(conn, settings, now), settings.app_name)
    if not post_alert(settings.slack_webhook_url, {"text": text}, http_post=http_post):
        return False

    conn.execute(
        """insert into sync_state (source, last_synced_at) values (%s, now())
           on conflict (source) do update set last_synced_at = now()""",
        (DIGEST_SOURCE,),
    )
    conn.commit()
    return True
