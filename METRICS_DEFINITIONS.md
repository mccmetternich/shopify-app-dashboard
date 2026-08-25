# METRICS DEFINITIONS

One entry per metric on any live page. Source: `metrics.py` (registry) + `stats.py` (formulas).

Drift guard: `tests/test_metrics_definitions.py` walks the `METRICS` registry and fails if any slug
is missing from this file. Do not remove entries — mark them `DEPRECATED` instead.

---

## revenue — Net Revenue

**Plain-English meaning:** Your total sales minus any refunds, for the selected time period. This is
the actual money you collected — not what you invoiced, not what settled.

**Formula:**
```sql
SELECT COALESCE(SUM(total - refunded), NULL)
FROM orders
WHERE created_at >= now() - interval '{window_days} days'
```

**Source tables/fields:** `orders.total`, `orders.refunded`, `orders.created_at`

**Window semantics:** Rolling window ending at query time. `window_days` = 7, 30, 90, or a
custom range computed from `start_date`/`end_date`.

**Edge cases:**
- Empty `orders` table → `NULL` (not 0). A 0 would mean "data exists and sums to zero", which is
  a different — and false — claim.
- Partial refunds: `refunded` can be < `total`. The formula deducts whatever was refunded, not the
  full order.
- No refunds: `refunded` defaults to 0.00, so `total - refunded = total` when no refund occurred.

---

## new_customers — New Customers

**Plain-English meaning:** First-time buyers only — people who have never purchased from you before
within your Shopify history.

**Formula:**
```sql
SELECT COUNT(DISTINCT customer_id)
FROM orders
WHERE is_new_customer = TRUE
  AND created_at >= now() - interval '{window_days} days'
```

**Source tables/fields:** `orders.customer_id`, `orders.is_new_customer`, `orders.created_at`

**Window semantics:** Rolling window. `DISTINCT` guards against duplicate order rows.

**Edge cases:**
- Empty table → 0 (not NULL). Zero new customers is a valid observable state.
- `is_new_customer` is set at Shopify ingest: `true` when the order's `order_number` equals the
  customer's first order or when Shopify's `first_order_id` matches. Guest checkouts without an
  account are tagged at ingest as new when no `customer_id` exists.

---

## blended_cac — Cost to Acquire a Customer

**Plain-English meaning:** How much you spent on ads to bring in one new customer. Divide total ad
spend by new customers in the same period.

**Formula:**
```
blended_cac = SUM(ad_spend.spend WHERE date in window) / COUNT new_customers in window
```

```sql
-- spend
SELECT COALESCE(SUM(spend), NULL) FROM ad_spend WHERE date >= now()::date - {window_days}
-- new_customers: see above
```

**Source tables/fields:** `ad_spend.spend`, `ad_spend.date`, `orders.is_new_customer`

**Window semantics:** Both numerator and denominator use the same rolling window.

**Edge cases:**
- `new_customers = 0` → `NULL` (cannot divide by zero; returning 0 would misrepresent "we spent
  money but acquired nobody").
- `total_spend = NULL` (no ad spend data) → `NULL`.
- `total_spend = 0` → `NULL` (spend exists but is $0; CAC of $0 is misleading without context).

---

## mer — Marketing Efficiency (MER)

**Plain-English meaning:** How much revenue you earn for every dollar spent on ads. A 3x MER means
$3 back for every $1 spent. Higher is better.

**Formula:**
```
MER = SUM(total - refunded) / SUM(spend)
```

Both sides use the same window.

**Source tables/fields:** `orders.total`, `orders.refunded`, `ad_spend.spend`

**Window semantics:** Rolling window. Spend uses `ad_spend.date` (date-only); revenue uses
`orders.created_at` (timestamp). A one-day mismatch at the boundary is expected and acceptable.

**Edge cases:**
- `spend = 0` or `spend = NULL` → `NULL`. MER of infinity is not returned.
- `revenue = NULL` (empty orders) → `NULL`.
- MER can exceed 10x on seed data because spend is seeded lower than revenue.

---

## subscription_share — Subscription Conversion

**Plain-English meaning:** What percentage of your new customers signed up for a subscription.
Higher means more predictable recurring revenue.

**Formula:**
```
subscription_share = (
    COUNT DISTINCT customer_id FROM subscription_revenue WHERE converted_at IN window
    /
    COUNT new customers in window
) * 100
```

Capped at 100%.

**Source tables/fields:** `subscription_revenue.customer_id`, `subscription_revenue.converted_at`,
`orders.is_new_customer`

**Window semantics:** `converted_at` is when the subscription was first created (Recharge
`created_at`). A customer can appear in both windows if their subscription started in a different
window than their first order — this is rare but will produce values slightly above or below
intuitive expectations.

**Edge cases:**
- `new_customers = 0` → `NULL`.
- Subscription data not yet synced (Recharge token missing) → `subs_in_window = 0`, so
  `subscription_share = 0%` — not NULL, because new customers exist and zero subs is meaningful.
- On demo seed data with misaligned `converted_at` dates, may show 0% even when both tables have
  rows. See Item 3 in the test gap closure for verification.

---

## aov — Average Order Value

**Plain-English meaning:** The average dollar amount per order. Higher means customers are buying
more per visit.

**Formula:**
```
AOV = SUM(total - refunded) / COUNT(*) FROM orders WHERE created_at IN window
```

**Source tables/fields:** `orders.total`, `orders.refunded`, `orders.created_at`

**Window semantics:** Rolling window. All orders in the window, including repeat purchases.

**Edge cases:**
- `order_count = 0` → `NULL`.
- Heavy refunds reduce both numerator and denominator partially. A full-refund order still counts
  in `COUNT(*)` but contributes $0 to the sum.

---

## days_of_cover — Inventory Days Remaining

**Plain-English meaning:** How many days of stock you have left at your current sales rate. Below
60 days is a warning — stock may run out before new inventory arrives.

**Formula:**
```
days_of_cover = inventory_levels.units_on_hand
                / (SUM(line_item quantities for serum SKU in last 14 days) / 14)
```

**Source tables/fields:** `inventory_levels.units_on_hand`, `inventory_levels.sku`,
`orders.line_items` (JSONB array), `settings.serum_sku`

**Window semantics:** Fixed 14-day lookback for the daily run rate denominator (not the page
window). This is a point-in-time metric, not a rolling window metric.

**Edge cases:**
- Fewer than 14 days of orders → `NULL`. Not enough history for a reliable run rate.
- `inventory_levels` row missing for the serum SKU → `NULL`.
- `units_on_hand = 0` → 0 days (triggers red warning immediately).
- `units_sold_14d = 0` (no serum orders in 14 days) → `NULL` (division by zero avoided).

---

## repeat_purchase_rate — Repeat Purchase Rate

**Plain-English meaning:** The percentage of customers who came back and bought again. Higher means
stronger loyalty and lower dependence on ads.

**Formula:**
```
repeat_purchase_rate = (
    COUNT DISTINCT customer_id WHERE order_count > 1 in window
    /
    COUNT DISTINCT customer_id in window
) * 100
```

Computed in `all_stats()` / `all_prior_stats()` via `repeat_rate` key.

**Source tables/fields:** `orders.customer_id`, `orders.created_at`

**Window semantics:** Rolling window. A customer counts as "repeat" if they placed more than one
order within the window, not across all time.

**Edge cases:**
- `total_customers = 0` → `NULL`.
- A customer with two orders in the window counts once as repeat. A customer with one order counts
  once as non-repeat.

---

## refund_rate — Refund Rate

**Plain-English meaning:** The percentage of orders that were refunded. Lower is healthier.

**Formula:**
```
refund_rate = COUNT(*) FILTER (WHERE refunded > 0) / COUNT(*) * 100
FROM orders WHERE created_at IN window
```

**Source tables/fields:** `orders.refunded`, `orders.created_at`

**Window semantics:** Rolling window.

**Edge cases:**
- `order_count = 0` → `NULL`.
- Partial refunds (refunded < total) still increment the refunded-order count.
- A refund processed outside the window on an order inside the window: counted if `refunded > 0`
  at query time. Refund timestamps are not tracked separately.

---

## cohort_revenue_per_customer — Cohort Revenue / Customer

**Plain-English meaning:** Cumulative net revenue per customer cohort member, N months after their
first order.

**Formula:**
```
LTV[N] = SUM(total - refunded for all orders by cohort members through month N) / cohort_size
```

**Source tables/fields:** `orders.total`, `orders.refunded`, `orders.customer_id`,
`customers.first_order_at`, `customers.id`

**Window semantics:** Not a rolling window — fixed cohort based on `first_order_at` month.
Cumulative through each month offset (M0, M1, M2, …, M11).

**Edge cases:**
- Cohorts with fewer than 12 months of history are still shown with available months.
- `cohort_size = 0` → division avoided; row skipped.
- A customer who returns orders partially reduces the cohort LTV.

---

## subscription_retention — Subscription Retention

**Plain-English meaning:** Percentage of subscribers from a given start month who are still active
N months later. Higher and greener is better.

**Formula:**
```
retention[N] = COUNT(subscribers in cohort still active at month N) / cohort_size * 100
```
"Active at month N" = `churned_at IS NULL OR churned_at > cohort_month + N months`.

**Source tables/fields:** `subscription_revenue.customer_id`, `subscription_revenue.converted_at`,
`subscription_revenue.churned_at`

**Window semantics:** Cohort defined by `converted_at` month. Monthly retention grid.

**Edge cases:**
- If Recharge token is not set, `subscription_revenue` is empty → empty retention grid (no rows
  shown, not 0% across the board).
- `cohort_size = 0` → division avoided; row skipped.

---

## survey_tally — Survey: Heard Via

**Plain-English meaning:** Count of post-purchase survey responses grouped by how the customer
heard about Densologie.

**Formula:**
```sql
SELECT properties->>'heard_via', COUNT(*)
FROM usage_events
WHERE event_type = 'survey_response'
  AND created_at >= now() - interval '{window_days} days'
GROUP BY 1
ORDER BY 2 DESC
```

**Source tables/fields:** `usage_events.event_type`, `usage_events.properties` (JSONB),
`usage_events.created_at`

**Window semantics:** Rolling window. Only `survey_response` events.

**Edge cases:**
- Missing `heard_via` key in JSONB → grouped as `"unknown"`.
- Empty table → empty list (not an error).

---

## Metrics NOT in the METRICS registry (page-level only)

These appear on live pages but are computed ad-hoc without a registry entry.

### three_revenue_streams (Overview, Upsell)
New customer / subscription recurring / non-sub repeat revenue split. Mutually exclusive.
- New customer: `is_new_customer = true`
- Subscription recurring: `is_subscription_order = true AND is_new_customer = false`
- Non-sub repeat: `is_new_customer = false AND is_subscription_order = false`
- Total = sum of all three. `NULL` only when all three are NULL.

### gross_profit_summary (Overview — Profit breakdown)
Waterfall: `gross_revenue − refunds = net_revenue − cogs − shipping − payment_fees = gross_profit − ad_spend = profit_after_ads`.
- `cogs`: estimated from `cost_inputs.cogs_per_unit × line_item quantities`; `cogs_estimated=true` when any SKU is absent.
- `shipping`: `cost_settings['shipping_cost_per_order'] × order_count`.
- `payment_fees`: `cost_settings['payment_fee_pct'] × gross_revenue`.
- Returns `NULL` when no orders in window.

### daily_revenue_and_spend (Overview — hero chart)
Daily time series. `generate_series` fills gaps so the chart always returns exactly `window_days` rows.
- Revenue: `SUM(total - refunded)` per day from `orders`.
- Spend: `SUM(spend)` per day from `ad_spend`.
- Days with no data show `0`, not `NULL` (by design for chart rendering).

### subscription_movement_summary (Subscriptions)
Monthly event counts from `subscription_events` for event types: `new`, `expansion`, `contraction`, `churn`, `winback`.
Each row: `{count, mrr}` where `mrr = SUM(mrr_delta)` for that event type in the calendar month.
Returns `NULL` when no events exist for the period.

### payback_timing (Subscriptions)
Per-cohort cumulative gross profit vs CAC. `payback_month` = first month where `cumulative_gp >= cac`.
- `cac` = `blended_cac` for the cohort's first-order month.
- `gp_by_month` = cumulative LTV at each month offset.
- Returns empty list when no cohort data.

### upsell_take_rates (Upsell)
Per offer-type: `accepted / total_offered * 100`. From `upsell_events` table.
Returns `NULL` take_rate when `total_offered = 0`.

### serum_vs_capsules_ltv (Upsell)
12-month LTV split: customers who subscribed to serum-only vs serum+capsules.
Returns `{serum_only: {ltv, count}, serum_capsules: {ltv, count}, delta_pct}`.
`delta_pct` = `(serum_capsules.ltv - serum_only.ltv) / serum_only.ltv * 100`. `NULL` when either cohort empty.

### offer_segmented_cohorts (Cohorts)
Cohort LTV grid per `offer_tag`: `full-price`, `coupon-only`, `steep-intro-discount`, `reactivation`.
Same grid structure as `cohort_ltv_12m`. Empty grid when `offer_tag` column is unpopulated (requires ingest-side tagging).
