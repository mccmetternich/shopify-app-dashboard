"""One definition per number, written once.

Every figure on the dashboard is somebody's decision about what to count, and a
reader who cannot see that decision has to either trust it blind or go read the
SQL. This is the same idea as Mixpanel's Lexicon — a dict, not a governance
product.

The registry is the single source. Tiles read it for the hover panel, markdown
twins read it so a pasted page carries its own definitions into whatever agent
reads it next. A definition can therefore be wrong, but it cannot be
*inconsistent* — the failure that actually happens in the wild.

`rule` stays close to the SQL rather than paraphrasing it. A paraphrase is what
drifts. `definition` is the one sentence a non-technical reader gets.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class Metric:
    name: str
    """The label the tile shows."""

    slug: str
    """snake_case, used in export.json keys and URL slugs."""

    unit: Literal["currency", "count", "percent", "ratio", "days", "text"]
    """Drives how a delta is formatted."""

    definition: str
    """One sentence of plain English — what a person would say out loud."""

    rule: str
    """The exact counting rule, close enough to the query to be checkable.
    Kept for backwards compatibility with _macros.html defn panel."""

    data_source: str
    """Which table(s) or API feed this number comes from."""

    # Alias so _macros.html `{{ m.source }}` still works
    @property
    def source(self) -> str:
        return self.data_source

    pages: tuple[str, ...]
    """Which dashboard pages show this metric."""

    kind: str = "window"
    """`point` = state as of now. `window` = count over a span.
    Point metrics compare to their own past value; window metrics compare to the
    prior window. Mixing them up is how a comparison lies."""

    better: str | None = None
    """`up`, `down`, or None when neither direction is good news on its own."""

    warn_below: float | None = None
    """For threshold tiles: show warning styling when value falls below this."""

    warn_above: float | None = None
    """For threshold tiles: show warning styling when value rises above this."""

    benchmark: str | None = None
    """Industry or best-practice benchmark, written in plain English for the info popover."""


METRICS: dict[str, Metric] = {

    # ── Overview headline tiles ─────────────────────────────────────────────

    "revenue": Metric(
        name="Net Revenue",
        slug="revenue",
        unit="currency",
        definition="Your total sales minus any refunds, for the selected time period. This is the actual money you collected.",
        rule="sum(total - refunded) from orders where created_at in window",
        data_source="orders",
        pages=("overview",),
        kind="window",
        better="up",
        benchmark=None,
    ),

    "new_customers": Metric(
        name="New Customers",
        slug="new_customers",
        unit="count",
        definition="First-time buyers only — people who have never purchased from you before.",
        rule="count(distinct customer_id) from orders where is_new_customer = true and created_at in window",
        data_source="orders",
        pages=("overview",),
        kind="window",
        better="up",
        benchmark=None,
    ),

    "blended_cac": Metric(
        name="Cost to Acquire a Customer",
        slug="blended_cac",
        unit="currency",
        definition="How much you spent on ads to bring in one new customer. Divide total ad spend by new customers. Lower is better.",
        rule="sum(spend) from ad_spend in window / count of new customers in same window. Null when new_customers = 0.",
        data_source="ad_spend, orders",
        pages=("overview",),
        kind="window",
        better="down",
        benchmark="DTC supplements: $30–$60 is typical. Below $30 is strong.",
    ),

    "mer": Metric(
        name="Marketing Efficiency (MER)",
        slug="mer",
        unit="ratio",
        definition="How much revenue you earn for every dollar spent on ads. A 3x MER means $3 back for every $1 spent. Higher is better.",
        rule="sum(total - refunded) / sum(spend) across the window. Null when spend = 0.",
        data_source="orders, ad_spend",
        pages=("overview",),
        kind="window",
        better="up",
        benchmark="Above 3x is healthy. Between 2x–3x is acceptable. Below 2x means ads may not be profitable.",
    ),

    "subscription_share": Metric(
        name="Subscription Conversion",
        slug="subscription_share",
        unit="percent",
        definition="What percentage of your new customers signed up for a subscription. Higher means more predictable recurring revenue.",
        rule="count of customer_ids in subscription_revenue with converted_at in window / count of new customers in window * 100.",
        data_source="orders, subscription_revenue",
        pages=("overview",),
        kind="window",
        better="up",
        benchmark="30% or higher is strong for a DTC subscription brand.",
    ),

    "aov": Metric(
        name="Average Order Value",
        slug="aov",
        unit="currency",
        definition="The average dollar amount per order. Higher means customers are buying more per visit.",
        rule="sum(total - refunded) / count(*) from orders where created_at in window.",
        data_source="orders",
        pages=("overview",),
        kind="window",
        better="up",
        benchmark="Track this alongside discount usage — heavy discounts inflate order count but reduce AOV.",
    ),

    "days_of_cover": Metric(
        name="Inventory Days Remaining",
        slug="days_of_cover",
        unit="days",
        definition="How many days of stock you have left at your current sales rate. Below 60 days is a warning sign — you may run out before new stock arrives.",
        rule="inventory_levels.units_on_hand / (sum of line_item quantities for serum SKU in last 14 days / 14). Null when fewer than 14 days of orders exist.",
        data_source="inventory_levels, orders",
        pages=("overview",),
        kind="point",
        warn_below=60.0,
        benchmark="Keep at least 60 days of cover as a buffer for shipping delays.",
    ),

    # ── Cohort metrics ──────────────────────────────────────────────────────

    "cohort_revenue_per_customer": Metric(
        name="Cohort Revenue / Customer",
        slug="cohort_revenue_per_customer",
        unit="currency",
        definition="Cumulative net revenue per customer cohort member, N months after their first order.",
        rule="sum(total - refunded) for all orders by customers in cohort / cohort_size, cumulative through month N.",
        data_source="orders, customers",
        pages=("cohorts",),
        kind="point",
        better="up",
    ),

    "subscription_retention": Metric(
        name="Subscription Retention",
        slug="subscription_retention",
        unit="percent",
        definition="Percentage of subscribers from a given start month who are still active N months later.",
        rule="count of subscribers in cohort where churned_at is null or churned_at > cohort_month + N / cohort_size * 100.",
        data_source="subscription_revenue",
        pages=("cohorts",),
        kind="point",
        better="up",
    ),

    # ── Repeat purchase & refund ─────────────────────────────────────────────

    "repeat_purchase_rate": Metric(
        name="Repeat Purchase Rate",
        slug="repeat_purchase_rate",
        unit="percent",
        definition="The percentage of customers who came back and bought again. Higher means stronger loyalty and lower dependence on ads.",
        rule="count of customer_ids with order_count > 1 / total customer_ids in window * 100.",
        data_source="orders",
        pages=("overview",),
        kind="window",
        better="up",
        benchmark="20–30% is typical for DTC. Above 40% is excellent.",
    ),

    "refund_rate": Metric(
        name="Refund Rate",
        slug="refund_rate",
        unit="percent",
        definition="The percentage of orders that were refunded. Lower is healthier — high refunds signal product, quality, or expectation issues.",
        rule="count(*) filter (where refunded > 0) / count(*) from orders where created_at in window * 100.",
        data_source="orders",
        pages=("overview",),
        kind="window",
        better="down",
        benchmark="Below 3% is healthy. Above 5% warrants investigation.",
    ),

    # ── Survey metrics ──────────────────────────────────────────────────────

    "survey_tally": Metric(
        name="Survey: Heard Via",
        slug="survey_tally",
        unit="count",
        definition="Count of post-purchase survey responses grouped by how the customer heard about Densologie.",
        rule="count(*) from usage_events where event_type = 'survey_response' grouped by properties->>'heard_via'.",
        data_source="usage_events",
        pages=("survey",),
        kind="window",
    ),
}


# ── Backwards-compatibility shims used by web.py, _macros.html, markdown_export.py ──

COMPARE_LABEL = {"point": "vs prior period", "window": "vs prior window"}


def signed(value, unit: str = "count") -> str:
    """A change, with its sign always visible.

    `+0` is deliberate rather than blank: 'no change' is information, and an
    empty slot where other tiles have a number reads as broken.
    """
    if unit in ("currency", "usd"):
        return f"{'-' if value < 0 else '+'}${abs(Decimal(str(value))):,.2f}"
    if unit in ("percent", "pct"):
        return f"{value:+.1f} pts"
    if unit == "ratio":
        return f"{value:+.2f}x"
    return f"{value:+,}"


def get(slug: str) -> Metric:
    return METRICS[slug]


def all_metrics() -> list[Metric]:
    return list(METRICS.values())
