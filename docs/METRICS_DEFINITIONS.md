# DENSOLOGIE SCOREBOARD — Metrics Definitions

> **GATE 1 APPROVED 2026-08-24.** All flags resolved per architect rulings below.
> All formulas are final for GATE 2 (seed + tests). No UI built yet.

Last updated: 2026-08-24 (GATE 1 rulings applied)

---

## Guiding Principles

- **Null-not-zero**: every aggregate returns `None` when no data exists in the window. Zero is a real measurement; null means "we don't know yet."
- **Attribution caution**: GA4-sourced numbers carry `~` superscript and a caption. Shopify order counts are hard data.
- **Gross-profit discipline**: LTV, payback, and cohort curves use gross profit, not revenue. Revenue is shown separately and clearly labeled.
- **Recognized ≠ cash**: MRR-equivalent and cash-collected are always on separate lines; never combined.
- **No hardcoded costs or prices**: COGS reads from `cost_inputs` table; prices read from Shopify at sync time. If unresolvable, flag with "est." label — never guess.

---

## 1. Subscription Revenue Recognition

### 1.1 Plan types and prices

Prices are NOT hardcoded. They are read from Shopify product/selling-plan data at sync time and stored on `subscription_revenue.cash_collected` and `subscription_revenue.monthly_amount`. The seed mirrors the known model below; live path reads Shopify.

| Plan | Cash collected at purchase (seed values) | MRR-equivalent / mo | Recognition period |
|------|------------------------------------------|---------------------|--------------------|
| Monthly | $129 per billing cycle | $129 | 1 month |
| 3-month prepaid | $327 per term | $109 ($327 ÷ 3) | 3 months |
| 6-month prepaid | $594 per term | $99 ($594 ÷ 6) | 6 months |

If a price cannot be resolved from Shopify, the cell is flagged "price-unknown" — never filled with a guess.

### 1.2 MRR-equivalent recognized

```
mrr_equivalent_per_month(sub) =
  if sub.sub_type == 'monthly':  sub.cash_collected          # = $129
  if sub.sub_type == '3mo':      sub.cash_collected / 3      # = $109
  if sub.sub_type == '6mo':      sub.cash_collected / 6      # = $99
```

Stored as `monthly_amount` on `subscription_revenue`. Each recognized-MRR month is also recorded as a `subscription_events` row with `event_type = 'mrr_recognized'`, enabling the waterfall.

### 1.3 Cash collected (separate field)

`cash_collected` on `subscription_revenue` = the actual Recharge charge amount at billing.

- Monthly: cash_collected = $129 in each renewal month
- 3-mo prepaid: cash_collected = $327 in month 0; $0 in months 1–2
- 6-mo prepaid: cash_collected = $594 in month 0; $0 in months 1–5

Dashboard always shows both lines separately:
```
MRR recognized this month:  $X,XXX   ← sum of monthly_amount for active subs in period
Cash collected this month:   $X,XXX   ← sum of cash_collected where cash_collected > 0
```

### 1.4 Subscriber retention and churn boundary

A subscriber is retained through any active prepaid term. Churn is evaluated **only at the renewal boundary**:

- Monthly: monthly billing date
- 3-mo prepaid: end of month 3
- 6-mo prepaid: end of month 6

**RULING (FLAG-A2 confirmed):** `churned_at` = end of the paid term, NOT the cancellation date. A subscriber who cancels in month 2 of a 3-month prepaid: MRR recognized through month 3 (cash already collected); `churned_at` = end of month 3.

### 1.5 Expansion and contraction

A subscriber who moves from monthly ($129) to 3-month prepaid ($109/mo recognized) is a **contraction** in MRR-equivalent terms ($20/mo reduction). Moving from monthly to a bundle subscription is **expansion**. Adding a capsule subscription is **expansion**.

These events are recorded in `subscription_events` with:
- `event_type` = 'expansion' or 'contraction'
- `mrr_delta` = new_monthly_amount − old_monthly_amount (positive = expansion, negative = contraction)

Seeded: ~10 expansions, ~5 contractions across the 90-day window.

---

## 2. Churn

All churn metrics computed **monthly**. The granularity picker does NOT affect churn — churn section always shows monthly data, labeled "Monthly."

**RULING (FLAG-B1 confirmed):** Denominator = subscribers at month start (not average of start + end).

### 2.1 Logo churn — voluntary

```
logo_churn_voluntary(month) =
  count(churned subs where churn_type='voluntary' AND churned_at IN month)
  / count(active subs at month start)
```

### 2.2 Logo churn — involuntary (confirmed churn)

**RULING (FLAG-B2 confirmed):** Involuntary churn = payment failed AND no recovery within 14 days.

```
logo_churn_involuntary(month) =
  count(churned subs where churn_type='involuntary'
        AND churned_at IN month
        AND (churned_at - dunning_started_at) >= 14 days)
  / count(active subs at month start)
```

### 2.3 In-dunning / at-risk (separate, NOT counted as churn yet)

Payment failed but still inside the 14-day recovery window = **at-risk**, not churned. Shown as a distinct amber card:

```
subs_in_dunning =
  count(subscription_events where event_type='dunning_start'
        AND event_date > today - 14 days
        AND no subsequent 'dunning_resolved' event)

at_risk_mrr = sum(monthly_amount for subs_in_dunning)
```

Label: "Failed payments — still in dunning. Recoverable with retry / card update flow."

### 2.4 Revenue churn — voluntary

```
rev_churn_voluntary(month) =
  sum(monthly_amount for voluntary churns in month)
  / sum(monthly_amount for active subs at month start)
```

### 2.5 Revenue churn — involuntary

```
rev_churn_involuntary(month) =
  sum(monthly_amount for involuntary churns in month)
  / sum(monthly_amount for active subs at month start)
```

### 2.6 Skip rate (leading indicator — not churn)

**RULING (FLAG-B3 confirmed):** Skips ≠ churn. Tracked separately as a leading indicator.

```
skip_rate(month) =
  count(subscription_events where event_type='skip' AND event_date IN month)
  / count(active subs at month start)
```

Displayed as its own card with caption: "Heavy skipping predicts churn 30-60 days ahead."

### 2.7 Combined monthly churn (for benchmark comparison)

```
logo_churn_total(month) = logo_churn_voluntary + logo_churn_involuntary
```

Benchmark bands displayed as faint reference areas behind the actual curve (**not modeled curves** — FLAG-F1 ruling: these are research reference ranges, not a fitted shape):
- < 5%: top-quartile (faint green band)
- 5–7%: good (faint yellow band)
- > 10%: problem (faint red band)

---

## 3. Cost Inputs

### 3.1 cost_inputs table

```sql
CREATE TABLE cost_inputs (
  sku         text primary key,
  label       text not null,
  cogs_per_unit numeric(10,2) not null default 0,
  updated_at  timestamptz default now()
);

CREATE TABLE cost_settings (
  key         text primary key,
  value       numeric(10,4) not null,
  label       text not null,
  updated_at  timestamptz default now()
);
```

`cost_settings` keys:
- `shipping_cost_per_order` — flat cost per order shipped (e.g., 6.50)
- `payment_fee_pct` — Shopify Payments / Stripe transaction fee (e.g., 0.029 = 2.9%)
- `return_processing_cost` — flat cost per returned order

Seeded dummy values (labeled "est. — costs not finalized"):

| SKU | COGS (dummy) |
|-----|-------------|
| HAIR-SERUM-50ML | $3.50 |
| DSL-CAPS-90 | $5.00 |
| DSL-BUNDLE | $8.50 (sum of above) |
| DSL-3MO-SUPPLY | $10.50 |

cost_settings defaults:
- shipping_cost_per_order = 6.50
- payment_fee_pct = 0.029
- return_processing_cost = 5.00

### 3.2 Gross profit formula

```
gross_profit(order) =
  (order.total - order.refunded - order.discount_amount)
  - sum(line_item.quantity × cost_inputs[line_item.sku].cogs_per_unit)
  - cost_settings['shipping_cost_per_order']
  - (order.total × cost_settings['payment_fee_pct'])
```

If `cost_inputs[sku]` is missing for any line item: `gross_profit` returns `None` and the cell is labeled "est. — costs not finalized." Every GP/LTV number that depends on COGS carries an "est." flag until cost_inputs is fully populated.

---

## 4. LTV

### 4.1 12-month cohort LTV (realized, gross profit)

```
cohort_ltv_12m(cohort) =
  sum(gross_profit(order) for orders by cohort members in months M0..M11)
  / cohort_size
```

- Only shown for cohorts with ≥ 12 months of history.
- Partial cohorts (< 12 months) show "M0–M{n}" label; excluded from 12m LTV headline.
- If any gross_profit is None (missing COGS): returns None, labeled "est."

### 4.2 24-month cohort LTV (realized, gross profit)

Same formula, M0..M23. Only shown for cohorts with ≥ 24 months of history.

### 4.3 Theoretical LTV (shown separately, labeled)

```
theoretical_ltv =
  avg_monthly_gross_profit_per_active_sub / monthly_logo_churn_rate_total
```

Where:
- `avg_monthly_gross_profit_per_active_sub` = total GP from subscription orders in last 90 days / active subscribers / 3
- `monthly_logo_churn_rate_total` = last full month's total logo churn rate

Displayed in its own labeled card:
> "Theoretical estimate (lifespan = 1 / monthly churn). Note: predicted-LTV tools overstate realized LTV by 20–40%. Use cohort curves for CAC and cash decisions."

---

## 5. Three Revenue Streams

**RULING (FLAG-D1 corrected):** THREE streams, not two. Subscription-recurring and non-subscription repeat are kept separate so the subscription signal is visible.

### 5.1 Definitions

```
new_customer_revenue(period) =
  sum(order.total - order.refunded
      for orders where is_new_customer = true AND created_at IN period)

subscription_recurring_revenue(period) =
  sum(order.total - order.refunded
      for orders where is_subscription_order = true AND created_at IN period)

non_sub_repeat_revenue(period) =
  sum(order.total - order.refunded
      for orders where is_new_customer = false
                    AND is_subscription_order = false
                    AND created_at IN period)
```

`is_subscription_order` = boolean column on `orders`, set true by Recharge ingest when the order originates from a subscription charge.

### 5.2 Reconciliation identity

```
new_customer_revenue + subscription_recurring_revenue + non_sub_repeat_revenue
= total_net_revenue
```

Reconciliation test T16 asserts this to the cent.

### 5.3 Display

Stacked bar chart, three colors, % mix labels on each segment. Summary row:
```
New customer:          $X,XXX  (N%)
Sub recurring:         $X,XXX  (N%)
Non-sub repeat:        $X,XXX  (N%)
─────────────────────────────────
Total net revenue:     $X,XXX
```

---

## 6. Offer-Segmented Cohorts

### 6.1 Acquisition offer tags (4 types)

Applied to each customer at first-order ingest. Evaluated in priority order:

1. **reactivation**: `utm_source = 'reactivation'` OR (customer had a prior order AND gap > 90 days before this order)
2. **steep-intro-discount**: `discount_pct >= steep_discount_threshold` on first order (and not reactivation)
3. **coupon-only**: `0 < discount_pct < steep_discount_threshold` on first order
4. **full-price**: no discount on first order

```
discount_pct = order.discount_amount / order.total × 100
steep_discount_threshold = config.steep_discount_threshold  # default 30%
```

**RULING (FLAG-E1):** Steep threshold = 30% (not 20%). Stored in config as a tunable value.
**RULING (FLAG-E2):** Reactivation = utm_source='reactivation' OR >90-day lapse gap. Both confirmed.

Stored on `customers.acquisition_offer` column (set at first-order ingest, never updated).

### 6.2 Cohort revenue-per-customer by offer tag

Same as existing cohort grid but computed separately per offer tag. Display: 4 grids in a 2×2 layout, or a tabbed view. Each shows cumulative net revenue per cohort member through month N.

---

## 7. Payback Timing

### 7.1 Blended CAC per cohort month

```
blended_cac(cohort_month) =
  sum(ad_spend.spend where date IN cohort_month) / new_customers(cohort_month)
```

**RULING (FLAG-F2):** Same-month cohort CAC confirmed.

### 7.2 Cumulative gross profit per customer

```
cum_gp_per_customer(cohort, through_month_N) =
  sum(gross_profit(order) for orders by cohort members in months M0..MN) / cohort_size
```

If gross_profit has any None values (missing COGS): returns None, labeled "est."

### 7.3 Payback month

```
payback_month(cohort) = first N where cum_gp_per_customer(cohort, N) >= blended_cac(cohort)
```

Returns None if payback not yet observed in available data.

Chart: one line per cohort (cumulative GP/customer vs month), horizontal dashed line at cohort's CAC. Intersection = payback.

---

## 8. Upsell / Merchandising

### 8.1 Upsell adapter pattern

`UpsellAdapter` abstract class with `get_upsell_events(conn, period) -> list[UpsellEvent]`. Default implementation reads Shopify order line items and properties. Swappable for any upsell app's native table.

Config keys (in `config.py`):
```python
upsell_skus = {
    "priority_shipping": "SHIP-PRIORITY",   # placeholder — replace when app chosen
    "upsell_t1": "DSL-CAPS-90",             # placeholder
    "upsell_t2": "DSL-BUNDLE-3MO",          # placeholder
    "upsell_t3": "DSL-SUPPLY-6MO",          # placeholder
    "aftersell": "DSL-AFTERSELL",           # placeholder
}
```

**RULING (FLAG-G1):** Real SKUs/keys provided when upsell app is chosen. Until then: placeholders. Seed uses these placeholder SKUs.

### 8.2 Metrics

```
priority_shipping_attach_rate(period) =
  count(upsell_events where upsell_type='priority_shipping' AND accepted=true AND period)
  / count(orders in period) × 100

upsell_t1_take_rate(period) =
  count(upsell_events where upsell_type='upsell_t1' AND accepted=true AND period)
  / count(orders in period) × 100

# t2, t3 same pattern

aftersell_acceptance_rate(period) =
  count(upsell_events where upsell_type='aftersell' AND accepted=true AND period)
  / count(orders in period) × 100
```

Seeded realistic take rates: priority_shipping 25%, upsell_t1 15%, upsell_t2 8%, upsell_t3 3%, aftersell 12%.

### 8.3 Serum-only vs serum+capsules LTV comparison

```
serum_only_ltv_12m =
  cohort_ltv_12m for customers whose subscription orders contain only HAIR-SERUM-50ML SKU

serum_capsules_ltv_12m =
  cohort_ltv_12m for customers who have at least one DSL-CAPS-90 in any subscription order
```

---

## 9. Landing-Page Funnel

### 9.1 Landing-page type map (config table, not hardcoded)

```sql
CREATE TABLE landing_page_type_map (
  url_prefix   text primary key,
  page_type    text not null  -- 'pdp' | 'listicle' | 'lander' | 'direct_checkout'
);
```

Default seed:
| url_prefix | page_type |
|------------|-----------|
| /products/ | pdp |
| /blogs/ | listicle |
| /pages/ | lander |
| / | lander |
| /checkout | direct_checkout |

**RULING (FLAG-H1):** These prefixes are used as proposed. Matching is prefix-first, longest match wins. Unmatched paths → `page_type = 'other'` (catch-all). Config table is editable by Matthias without code changes.

### 9.2 Direct-to-checkout handling

Sessions where landing page resolves to `direct_checkout` are excluded from page-type funnel math. Counted separately as `direct_checkout_sessions` and shown as a footnote.

### 9.3 Funnel metrics per landing page type

```
sessions(type, period)         = sum(ga4_funnel.sessions where landing_page_type=type AND period)
atc_rate(type, period)         = sum(add_to_carts) / sum(sessions) × 100  ~
checkout_rate(type, period)    = sum(begin_checkouts) / sum(sessions) × 100  ~
purchase_rate(type, period)    = sum(purchases) / sum(sessions) × 100  ~
```

All carry `~` (GA4 attribution).

---

## 10. Time Granularity

| Granularity | Applies to | Label |
|------------|-----------|-------|
| Daily | Revenue, new customers, ad spend, GA4 funnel | Day-level bars |
| Weekly | All above + sparklines | Week starting Monday |
| Monthly | All above + churn + cohorts | Calendar month |

Churn, cohort retention, LTV, and subscription waterfall: **always monthly**. The granularity picker does not affect them. Labeled "Monthly" with no toggle.

---

## 11. MRR Waterfall

Monthly waterfall shown on /subscriptions page:

```
Beginning MRR
+ New MRR (from new subscribers)
+ Expansion MRR (upgrades, add-ons)
- Contraction MRR (downgrades, removals)
- Churned MRR (voluntary)
- Churned MRR (involuntary, confirmed ≥14 days)
= Ending MRR
```

All values use `monthly_amount` (recognized), not cash_collected. Expansion/contraction delta sourced from `subscription_events.mrr_delta`.

---

## 12. Subscription Lifecycle — Pause and Reactivation

### 12.1 Pause state definition and MRR treatment

Pause is a DISTINCT third state alongside 'active' and 'churned'. Stored as `status='paused'` on `subscription_revenue`, with `paused_at` timestamptz.

- While paused: subscriber is EXCLUDED from active-subscriber count.
- While paused: `monthly_amount` is EXCLUDED from recognized MRR (`mrr_recognized` filters `status != 'paused'`).
- Paused MRR is tracked separately as "deferred MRR" via `paused_subscribers()`.
- Pause is NOT a churn event and must NOT appear in any churn numerator.
- Skips (already handled, distinct event_type='skip') remain active — skips ≠ pause.

### 12.2 Pause rate formula

```
pause_rate(month) =
  count(subscription_events where event_type='pause' AND event_date IN month)
  / count(active subs at month start)
```

Returns None if no active subs at month start.

### 12.3 Pause outcome split formula

```
pause_outcome_split() =
  of all subs where paused_at IS NOT NULL:
    reactivated_pct = count(paused_outcome='reactivated') / total × 100
    cancelled_pct   = count(paused_outcome='cancelled') / total × 100
    still_paused_pct = count(paused_outcome IS NULL) / total × 100
```

Returns None if no subs have ever been paused.

### 12.4 Win-back / reactivation event definition

A win-back occurs when a previously churned customer creates a new active subscription. Recorded as `event_type='winback'` in `subscription_events`. Distinct from `event_type='new'` (first-ever subscription). The win-back event carries a positive `mrr_delta` equal to the new sub's `monthly_amount`.

### 12.5 Cohort immutability rule

A subscriber's cohort is determined by their ORIGINAL first-order date and never changes. All subsequent orders, including win-back orders, roll up to the original cohort. Win-back orders must not increment new_customer count or new_customer_revenue.

### 12.6 Waterfall reactivation bucket (separate from 'new')

The extended waterfall (`subscription_waterfall_v2`) adds a `reactivation_mrr` bucket between `new_mrr` and `expansion_mrr`:

```
Beginning MRR
+ New MRR           (event_type='new')
+ Reactivation MRR  (event_type='winback') ← separate from New
+ Expansion MRR     (event_type='expansion')
- Contraction MRR   (event_type='contraction')
- Churned MRR (voluntary)
- Churned MRR (involuntary, confirmed ≥14 days)
= Ending MRR
```

### 12.7 CAC exclusion rule (reactivations excluded from blended_cac denominator)

`blended_cac_excl_reactivations(window_days)` computes:

```
CAC = sum(ad_spend in window) / count(new customers where winback_count=0)
```

Reactivated subscribers (winback_count > 0) are excluded from the denominator. This gives true new-acquisition CAC, unmixed with win-back efficiency.

### 12.8 Cost per reactivation formula

```
cost_per_reactivation =
  sum(ad_spend tagged for reactivation campaigns in period)
  / count(winback events in period)
```

Tracked separately from blended CAC. Not yet implemented in stats.py (planned for Gate 3).

### 12.9 Gap tracking (avg_gap_days, reactivation_rate_by_cohort)

```
avg_gap_days =
  average of (winback event_date - original sub churned_at) in days

reactivation_rate_by_cohort(cohort) =
  count(customers with winback_count > 0 in cohort) / cohort_size × 100
```

`reactivation_stats(conn, window_days)` returns count, recovered_mrr, and avg_gap_days for winbacks in the window. `reactivation_rate_by_cohort(conn)` returns per-cohort rates for all cohorts with at least one win-back.

---

## Open Questions — ALL RESOLVED

| Flag | Question | Ruling |
|------|----------|--------|
| A1 | Monthly sub price | Read from Shopify; seed uses $129/$327/$594 |
| A2 | Mid-term cancel churn_at | = end of paid term ✓ |
| B1 | Churn denominator | Start-of-month ✓ |
| B2 | Involuntary churn timing | ≥14 days since dunning_start; in-dunning shown separately as at-risk ✓ |
| B3 | Skips ≠ churn | Confirmed; tracked separately ✓ |
| C1 | COGS | cost_inputs table; dummy COGS seeded; "est." label until real COGS entered ✓ |
| D1 | "Recurring" definition | THREE streams: new / sub-recurring / non-sub-repeat ✓ |
| E1 | Steep discount threshold | ≥30%, stored in config as tunable ✓ |
| E2 | Reactivation detection | utm='reactivation' OR >90-day lapse ✓ |
| E3 | Expansion/contraction | YES — build the waterfall buckets; seed some events ✓ |
| F1 | M12 >50% benchmark | Reference bands only, not a modeled curve shape ✓ |
| F2 | CAC denominator | Same-month cohort ✓ |
| G1 | Upsell SKU keys | Config dict with placeholders; real keys when app chosen ✓ |
| H1 | Landing-page URL patterns | Config table; catch-all 'other' bucket; tunable ✓ |
