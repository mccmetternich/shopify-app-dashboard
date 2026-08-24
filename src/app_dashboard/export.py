"""The whole scoreboard as one JSON file.

Phase C: overview (Densologie metrics), cohorts (customer LTV + subscription
retention), and survey (heard-via tally).
"""

import json
from datetime import datetime, timezone

from app_dashboard import annotations as anno
from app_dashboard import stats
from app_dashboard.faq import FAQ
from app_dashboard.markdown_export import json_default
from app_dashboard.metrics import METRICS
from app_dashboard.ops import sync_health

LIMITS = {
    "money_months": 24,
    "activity_months": 24,
    "retention_offsets": 24,
}


def _definitions() -> dict:
    return {
        key: {"name": m.name, "definition": m.definition, "rule": m.rule,
              "source": m.source, "kind": m.kind, "unit": m.unit,
              "better": m.better}
        for key, m in METRICS.items()
    }


def _overview(conn, settings) -> dict:
    from app_dashboard.stats import days_of_cover as _doc
    current = stats.overview_stats(conn, window_days=30)
    prior = stats.overview_stats(conn, window_days=60)  # rough prior; web.py computes exact prior
    current["days_of_cover"] = _doc(conn, settings.serum_sku)
    prior["days_of_cover"] = None
    comparison = stats.overview_comparison(current, prior)
    return {
        "summary": current,
        "comparison": comparison,
        "mrr_trend": stats.mrr_trend(conn, months=LIMITS["money_months"]),
        "mrr_movements": stats.mrr_movements(conn, months=LIMITS["money_months"]),
        "revenue_by_month": stats.revenue_by_month(conn, months=LIMITS["money_months"]),
        "monthly_activity": stats.monthly_activity(conn, months=LIMITS["activity_months"]),
        "recent_events": stats.recent_events(conn),
    }


def _cohorts(conn) -> dict:
    return {
        "customer_cohorts": stats.customer_cohorts(conn),
        "subscription_retention": stats.subscription_retention(conn),
        "paying_retention": stats.retention_cohorts(conn, max_offset=LIMITS["retention_offsets"]),
    }


def _survey(conn) -> dict:
    return {
        "tally_90d": stats.survey_tally(conn, window_days=90),
        "tally_all": stats.survey_tally(conn, window_days=0),
    }


def full_export(conn, settings, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    return {
        "meta": {
            "generated_at": now.isoformat(timespec="seconds"),
            "source": settings.public_base_url.rstrip("/"),
            "about": (
                f"Densologie Scoreboard data export — {settings.dashboard_name}. "
                "Includes overview metrics, customer cohorts, subscription retention, "
                "and post-purchase survey tally."
            ),
            "windows": LIMITS,
        },
        "definitions": _definitions(),
        "sync_health": sync_health(conn),
        "annotations": anno.recent(conn),
        "overview": _overview(conn, settings),
        "cohorts": _cohorts(conn),
        "survey": _survey(conn),
        "faq": [{"question": q, "answer": paragraphs} for q, paragraphs in FAQ],
    }


def filename(now: datetime | None = None, slug: str = "densologie") -> str:
    now = now or datetime.now(timezone.utc)
    return f"{slug}-{now:%Y-%m-%d}.json"


def render(conn, settings, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return json.dumps(full_export(conn, settings, now), indent=2,
                      default=json_default)
