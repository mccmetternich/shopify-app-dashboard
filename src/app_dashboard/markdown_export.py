"""Every page, as a markdown document an agent can read.

Same shape Shopify uses on shopify.dev: YAML frontmatter naming the page and
where it came from, then prose, then the data itself as JSON. The prose exists
so a model reading this cold does not have to guess what a number means; the
JSON exists so it does not have to parse a table.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal

from app_dashboard import annotations as anno
from app_dashboard import stats
from app_dashboard.faq import FAQ
from app_dashboard.metrics import METRICS
from app_dashboard.ops import sync_health
from app_dashboard.ranges import (
    MONEY_MONTHS,
    choice,
)

# Path on the site -> (slug used for the .md URL, title, one-line description).
PAGES = {
    "overview": ("index", "Overview",
                 "Revenue, customers, and ad efficiency for {app}."),
    "cohorts": ("cohorts", "Cohorts",
                "Customer LTV cohorts and subscription retention for {app}."),
    "survey": ("survey", "Survey",
               "Post-purchase survey: how customers heard about {app}."),
    "faq": ("faq", "Why the numbers don't match",
            "How MRR differs from collected revenue, and how subscription share is counted."),
}


def _definitions(*keys: str) -> str:
    return "\n".join([
        "## What these numbers mean\n",
        *[f"- **{METRICS[k].name}** &mdash; {METRICS[k].definition} "
          f"Counted as: {METRICS[k].rule}. Source: {METRICS[k].source}."
          for k in keys if k in METRICS],
        "",
    ])


def json_default(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, datetime):
        return o.isoformat()
    if hasattr(o, "isoformat"):
        return o.isoformat()
    raise TypeError(type(o))


def _json(value) -> str:
    return "```json\n" + json.dumps(value, indent=2, default=json_default) + "\n```"


def _frontmatter(page: str, base_url: str, now: datetime,
                 app_name: str = "Densologie",
                 dashboard_name: str = "Densologie Scoreboard") -> str:
    slug, title, description = PAGES[page]
    description = description.format(app=app_name)
    html_url = f"{base_url}/" if slug == "index" else f"{base_url}/{slug}"
    return (
        "---\n"
        f"title: '{dashboard_name}: {title}'\n"
        "description: >-\n"
        f"  {description}\n"
        "source_url:\n"
        f"  html: '{html_url}'\n"
        f"  md: '{base_url}/{slug}.md'\n"
        f"generated_at: '{now.isoformat(timespec='seconds')}'\n"
        "---\n"
    )


READING_NOTES = """## How to read this

- Every number on this scoreboard carries its own definition.
- Money is net of refunds unless noted otherwise.
- Subscription share counts new-customer orders that started a subscription in the same window.
"""


def _overview(conn, settings, query: dict) -> str:
    months = choice(query.get("months"), MONEY_MONTHS, 12)
    s = stats.overview_stats(conn, window_days=30)
    # Point metric computed separately
    from app_dashboard.stats import days_of_cover
    doc = days_of_cover(conn, settings.serum_sku)
    s["days_of_cover"] = doc
    health = sync_health(conn)
    notes = anno.recent(conn)

    return "\n".join([
        "# Overview\n",
        _definitions("revenue", "new_customers", "blended_cac", "mer",
                     "subscription_share", "aov", "days_of_cover"),
        "\n## Headline numbers (last 30 days)\n",
        _json(s),
        "\n## Annotations\n",
        _json(notes),
        "\n## Pipeline health\n",
        _json(health),
        f"\n## Subscription MRR by month, last {months} months\n",
        _json(stats.mrr_trend(conn, months)),
        "\n## Revenue by month\n",
        _json(stats.revenue_by_month(conn, months)),
    ])


def _cohorts(conn, settings, query: dict) -> str:
    return "\n".join([
        "# Cohorts\n",
        _definitions("cohort_revenue_per_customer", "subscription_retention"),
        "\n## Revenue per customer cohort\n",
        "Each row is the calendar month of a customer's first order. Each cell is cumulative "
        "net revenue per cohort member through month N.\n",
        _json(stats.customer_cohorts(conn)),
        "\n## Subscription retention cohort\n",
        "Each row is the month a subscription started; each cell is the percentage still "
        "subscribed N months later. M0 is always 100%.\n",
        _json(stats.subscription_retention(conn)),
    ])


def _survey(conn, settings, query: dict) -> str:
    return "\n".join([
        "# Survey\n",
        _definitions("survey_tally"),
        "\n## Post-purchase: heard via (last 90 days)\n",
        _json(stats.survey_tally(conn, window_days=90)),
    ])


def _faq(conn, settings, query: dict) -> str:
    parts = ["# Why the numbers don't match\n"]
    for question, paragraphs in FAQ:
        parts.append(f"## {question}\n")
        parts.extend(f"{paragraph}\n" for paragraph in paragraphs)
    return "\n".join(parts)


def render_page(conn, page: str, settings, query: dict | None = None,
                now: datetime | None = None) -> str:
    """Build one page's markdown."""
    query = query or {}
    now = now or datetime.now(timezone.utc)
    base_url = settings.public_base_url.rstrip("/")

    body = {
        "overview": _overview,
        "cohorts": _cohorts,
        "survey": _survey,
        "faq": _faq,
    }[page](conn, settings, query)

    return "\n".join([_frontmatter(page, base_url, now, settings.app_name,
                                   settings.dashboard_name),
                      body, "", READING_NOTES])


def customer_markdown(conn, settings, shop_domain: str, detail: dict,
                      now: datetime | None = None) -> str:
    """Stub — customer detail page is not part of Densologie Scoreboard."""
    return f"# {shop_domain}\n\nCustomer detail not available.\n"
