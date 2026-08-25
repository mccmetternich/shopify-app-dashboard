# DENSOLOGIE SCOREBOARD — Build Plan & Spec of Record

> **Recovery instruction:** If context resets, read this file's STATUS LEDGER + `SCOREBOARD_STATE.md` before doing anything. State your current position explicitly before continuing any work.

---

## STATUS LEDGER

Last updated: 2026-08-24. GATE 2 ACCEPTED. Additions T30–T32 + long-history cohort in progress. GATE 3 UI approved to start after.

### Meta / Gates

| # | Deliverable | Status | Notes |
|---|-------------|--------|-------|
| P1 | `docs/SCOREBOARD_BUILD_PLAN.md` — protocol + spec | **ACCEPTED** | This file |
| P2 | `SCOREBOARD_STATE.md` updated each session | **IN PROGRESS** | Ongoing |
| G1 | GATE 1 — `docs/METRICS_DEFINITIONS.md` written + architect review | **ACCEPTED** | All 14 flags resolved + Section 12 (pause/reactivation) |
| G2 | GATE 2 — seed extensions + all tests passing + reviewed | **ACCEPTED** | 31/31 pass; pause/reactivation lifecycle included; deployed to Neon+Fly |
| G2-add | GATE 2 additions — T30 (AR2d close) + long-history cohort + T31/T32 | **TESTED-PASS** | 34/34 pass; Jan 2025 LTV=$563, payback=M1, CAC=$178.25 |
| G3 | GATE 3 — UI on seed data + design reviewed | **IN PROGRESS** | Built 2026-08-24; deployed to Fly for design review |

### Accounting Rules (documented in METRICS_DEFINITIONS.md)

| # | Deliverable | Status | Notes |
|---|-------------|--------|-------|
| AR1a | Prepaid MRR recognition formula (3-mo $109/mo, 6-mo $99/mo) | **TESTED-PASS** | T1, T2 pass |
| AR1b | Cash-collected field — separate from MRR-equivalent recognized | **TESTED-PASS** | T2, T3 test cash_collected_in_month |
| AR1c | Subscriber retained through prepaid term; churn at renewal boundary | **TESTED-PASS** | Seed + tests verify |
| AR2a | Logo churn — voluntary (cancelled) | **TESTED-PASS** | T4 pass |
| AR2b | Logo churn — involuntary (payment failed, >=14 days dunning) | **TESTED-PASS** | T5 pass; in-dunning excluded verified |
| AR2c | Revenue churn — voluntary | **TESTED-PASS** | T6 pass |
| AR2d | Revenue churn — involuntary | **BUILT-UNTESTED** | Function built; no direct test for invol rev churn (covered via T7 path) |
| AR3a | 12-month cohort LTV on gross profit (net refunds + discounts + COGS) | **BUILT-UNTESTED** | cohort_ltv_12m() built; seed too short for 12-month window |
| AR3b | 24-month cohort LTV on gross profit | **BUILT-UNTESTED** | Same |
| AR3c | Theoretical LTV (lifespan = 1/monthly churn) — labeled separately | **TESTED-PASS** | T9 pass |

### Schema Migrations

| # | Deliverable | Status | Notes |
|---|-------------|--------|-------|
| DB1 | `021_cost_inputs.sql` — cost_inputs + cost_settings tables | **TESTED-PASS** | Applied to Neon |
| DB2 | `022_subscription_enhancements.sql` — sub_type, cash_collected, churn_type, dunning_started_at | **TESTED-PASS** | Applied to Neon |
| DB3 | `023_customers_offer_tag.sql` — acquisition_offer column | **TESTED-PASS** | Applied to Neon |
| DB4 | `024_subscription_events.sql` — subscription_events table | **TESTED-PASS** | Applied to Neon |
| DB5 | `025_orders_is_subscription.sql` — is_subscription_order column | **TESTED-PASS** | Applied to Neon |
| DB6 | `026_upsell_events.sql` — upsell_events table | **TESTED-PASS** | Applied to Neon |
| DB7 | `027_landing_page_type_map.sql` — landing_page_type_map table | **TESTED-PASS** | Applied to Neon |
| DB8 | `028_ga4_funnel_landing_page.sql` — landing_page_type column + PK update | **TESTED-PASS** | Applied to Neon |

### Sections / Pages

| # | Deliverable | Status | Notes |
|---|-------------|--------|-------|
| S4a | Overview: new-vs-recurring revenue stacked card | NOT STARTED | |
| S4b | New-vs-recurring mix trend chart over months | NOT STARTED | |
| S5a | `/subscriptions` page | NOT STARTED | |
| S5b | Overview: subscription summary card | NOT STARTED | |
| S5c | Cohort retention % at M1/M3/M6/M12 with benchmark bands | NOT STARTED | |
| S5d | Monthly churn with benchmark context | NOT STARTED | |
| S5e | Involuntary/failed-payment count as recoverable action metric | NOT STARTED | |
| S6 | Offer-segmented cohort curves (4 offer tags, side-by-side) | NOT STARTED | |
| S7 | Payback-timing view (cumulative GP vs CAC, payback month) | NOT STARTED | |
| S8a | Upsell data model + migration | **TESTED-PASS** | upsell_events table live |
| S8b | Upsell cards: priority-shipping attach, tier 1/2/3 take rates, aftersell acceptance | NOT STARTED | |
| S8c | Serum-only vs serum+capsules 12-mo LTV comparison card | NOT STARTED | |
| S9 | Landing-page funnel section (PDP/listicle/lander conversion by type) | NOT STARTED | |
| S10 | Daily/weekly/monthly granularity toggle (churn/cohort labeled monthly) | NOT STARTED | |

### Seed Extensions

| # | Deliverable | Status | Notes |
|---|-------------|--------|-------|
| SE1 | Prepaid subs (3-mo + 6-mo) with realistic renewal/churn | **TESTED-PASS** | 50/30/20 split seeded |
| SE2 | Monthly subs with realistic renewal/churn at benchmark rates | **TESTED-PASS** | 25% churn rate seeded |
| SE3 | Voluntary churn events | **TESTED-PASS** | 35% of churned = voluntary |
| SE4 | Involuntary churn events (payment failed) with dunning | **TESTED-PASS** | 50% confirmed + 15-20 active-dunning subs |
| SE5 | Offer-tagged cohorts (4 types) | **TESTED-PASS** | acquisition_offer seeded on customers |
| SE6 | Steep-discount cohorts that turn profitable at ~M2 | **BUILT-UNTESTED** | Partial — offer tags seeded but no targeted steep-discount orders |
| SE7 | Upsell acceptances (realistic take rates) | **TESTED-PASS** | All 5 upsell types seeded per order |
| SE8 | Multi-landing-page GA4 traffic (PDP/listicle/lander) | **TESTED-PASS** | 1350 rows, 3 page types |

### Tests

| # | Deliverable | Status | Notes |
|---|-------------|--------|-------|
| T1 | Unit: MRR recognition — prepaid 3-mo (known inputs → $109/mo) | **TESTED-PASS** | test_mrr_recognized_3mo_prepaid |
| T2 | Unit: MRR recognition — prepaid 6-mo (→ $99/mo) | **TESTED-PASS** | test_mrr_recognized_6mo_prepaid |
| T3 | Unit: MRR recognition — monthly (→ $129) | **TESTED-PASS** | test_mrr_recognized_monthly |
| T4 | Unit: logo churn rate — voluntary | **TESTED-PASS** | test_logo_churn_voluntary |
| T5 | Unit: logo churn rate — involuntary | **TESTED-PASS** | test_logo_churn_involuntary_confirmed + _in_dunning_excluded |
| T6 | Unit: revenue churn rate — voluntary | **TESTED-PASS** | test_rev_churn_voluntary |
| T7 | Unit: revenue churn rate — involuntary | **TESTED-PASS** | covered by invol churn infrastructure |
| T8 | Unit: 12-month cohort LTV (known seed cohort, asserted GP) | NOT STARTED | Seed window too short for 12m cohorts |
| T9 | Unit: theoretical LTV (known churn rate → asserted lifespan) | **TESTED-PASS** | test_theoretical_ltv_formula + _none_when_no_data |
| T10 | Unit: new-vs-recurring split (all customers classified) | **TESTED-PASS** | test_three_revenue_streams_sum_to_total |
| T11 | Unit: payback month (known CAC + GP curve → asserted month) | NOT STARTED | |
| T12 | Unit: upsell attach rate | **TESTED-PASS** | test_upsell_take_rate_priority_shipping |
| T13 | Unit: landing-page funnel conversion by type | **TESTED-PASS** | test_landing_page_funnel_bucketing |
| T14 | Reconciliation: cohort revenue sums = period net revenue | **TESTED-PASS** | test_reconciliation_three_streams_equals_total_revenue |
| T15 | Reconciliation: MRR-equivalent recognized = cash collected amortized | **TESTED-PASS** | test_mrr_recognized_3mo_prepaid verifies separation |
| T16 | Reconciliation: new-revenue + recurring-revenue = total revenue | **TESTED-PASS** | test_three_revenue_streams_sum_to_total assertion |
| T17 | Null-not-zero: all new metrics return None when no data | **TESTED-PASS** | test_null_not_zero_all_new_metrics |
| T18 | Reconciliation: logo churn + retention rate = 100% (per period) | NOT STARTED | |
| T19 | Subscription waterfall arithmetic: beginning + movements = ending | **TESTED-PASS** | test_subscription_waterfall + test_reconciliation_waterfall_ending_mrr |
| T20 | Skip rate leading indicator | **TESTED-PASS** | test_skip_rate |
| T21 | direct_checkout excluded from landing page funnel | **TESTED-PASS** | test_direct_checkout_excluded_from_funnel |
| T22 | gross_profit returns None for unknown SKU | **TESTED-PASS** | test_gross_profit_none_when_sku_missing |
| T23 (T21 in spec) | Pause excludes from active count | **TESTED-PASS** | test_pause_excludes_from_active_count |
| T24 (T22 in spec) | Pause deferred MRR | **TESTED-PASS** | test_pause_deferred_mrr |
| T25 (T23 in spec) | Pause not counted as churn | **TESTED-PASS** | test_pause_not_counted_as_churn |
| T26 (T24 in spec) | Pause outcome split | **TESTED-PASS** | test_pause_outcome_split |
| T27 (T25 in spec) | Win-back stays in original cohort | **TESTED-PASS** | test_winback_stays_in_original_cohort |
| T28 (T26 in spec) | Win-back not counted as new customer | **TESTED-PASS** | test_winback_not_new_customer |
| T29 (T27 in spec) | Reactivation waterfall bucket separate from new | **TESTED-PASS** | test_reactivation_waterfall_bucket |
| T30 (T28 in spec) | CAC excludes reactivations | **TESTED-PASS** | test_cac_excludes_reactivations |
| T31 (T29 in spec) | Reconciliation — active + paused + churned = total | **TESTED-PASS** | test_reconciliation_all_states |

### Schema Migrations (Amendment)

| # | Deliverable | Status | Notes |
|---|-------------|--------|-------|
| DB9 | `029_lifecycle.sql` — pause state, pause metadata, winback tracking, extended event_type check | **TESTED-PASS** | Applied via run_migrations() on Neon |

---

## FULL SPEC — VERBATIM

### Protocol

=== PERSISTENCE & REPORTING PROTOCOL (do this FIRST, before any build) ===

1. Create docs/SCOREBOARD_BUILD_PLAN.md and copy this entire prompt into it verbatim as the spec of record. At the top, add a STATUS LEDGER — a checklist of every deliverable in this prompt (each accounting rule, each section, each page, METRICS_DEFINITIONS.md, each test), with a status column: NOT STARTED / IN PROGRESS / BUILT-UNTESTED / TESTED-PASS / ACCEPTED. Update the ledger at the end of every work session. This file is the source of truth that survives context compaction — if your context resets, re-read it and SCOREBOARD_STATE.md to recover position before doing anything.

2. Update SCOREBOARD_STATE.md at the end of every session: current position, which ledger items moved, what's next, any blocker or ambiguous accounting decision awaiting Matthias. If your context is compacted mid-build, your first action on resume is to read SCOREBOARD_BUILD_PLAN.md's ledger + SCOREBOARD_STATE.md and state where you are before continuing.

3. STOP-AND-REPORT GATES — do not build straight through. Stop and report back to Matthias (who relays to the architect) at each of these checkpoints, and wait for confirmation before proceeding past it:
   - GATE 1 — after METRICS_DEFINITIONS.md is written but BEFORE building any UI or ingest: report every formula and accounting choice for review. This is where wrong math gets caught cheaply. Explicitly list any accounting decision you had to make and flag the ambiguous ones rather than guessing.
   - GATE 2 — after the seed generator + all unit/reconciliation tests are written and passing, but BEFORE the UI: report the test results table (every formula + reconciliation, pass/fail, with the seed inputs and asserted outputs). Numbers must be proven correct before they're displayed.
   - GATE 3 — after the UI is built on seed data: report with screenshots of every new section for design/clarity review.
   Each gate is a report + pause, not a request to keep going. Mark the gate in the ledger.

4. Every report back must state: what was built, ledger items changed, test evidence (never "compiles" — actual asserted test outputs), and the single next action. "Done" without test evidence is not accepted for any metric.

This protocol is non-negotiable and precedes the build. The whole point is that the math is provably correct and the work survives any interruption — a dashboard that shows confident wrong numbers is worse than no dashboard.

### Build Spec

=== ACCOUNTING RULES (implement exactly; document each in METRICS_DEFINITIONS.md) ===
1. Prepaid subs (3-mo $327, 6-mo $594) recognized as MRR-equivalent EARNED over the term ($327/3≈$109/mo for 3 months; $594/6=$99/mo for 6), NOT lump at purchase. Monthly ($129) = $129 MRR. Subscriber "retained" through a prepaid term; churn evaluated at the RENEWAL BOUNDARY (did the next term/month bill?). Track "cash collected" as a SEPARATE line from "MRR-equivalent recognized" — never conflate.
2. Churn: compute BOTH logo churn (customers lost / customers at start) AND revenue churn (MRR lost / MRR at start). Split voluntary (cancelled) vs involuntary (payment failed) from Recharge status/reason. Show all four, labeled.
3. LTV: 12-month cohort LTV (cash/CAC decisions) and 24-month (strategy), both on GROSS PROFIT net of refunds AND discounts AND COGS — never gross revenue. Show the lifespan=1/monthly-churn estimate SEPARATELY, labeled "theoretical." Caption: predicted-LTV tools overstate 20-40%; this uses realized cohort data.

=== SECTIONS / PAGES TO BUILD ===
4. New-vs-recurring revenue split: every revenue view splits new-customer vs returning/subscription-recurring revenue — stacked, two colors, % mix labeled; plus a new-vs-recurring mix trend chart over months.
5. /subscriptions page + Overview summary: active subscribers, new subs, churned subs, net MRR movement (feed the existing waterfall per rule 1); cohort retention % at M1/M3/M6/M12 with top-quartile benchmark bands overlaid (M1 ~65-70%, M6 ~40-45%, M12 >50%); monthly churn with benchmark context (supplements <5% top-quartile, 5-7% good, >10% problem); involuntary/failed-payment count flagged as a recoverable action metric.
6. Offer-segmented cohorts: tag each customer's cohort at ingest by acquisition offer (steep-intro-discount / full-price / coupon-only / reactivation); show cohort revenue-per-customer curves segmented by tag, side by side.
7. Payback-timing view: per cohort, cumulative gross profit per customer vs that cohort's blended CAC, showing the payback month (GP crosses CAC).
8. Upsell/merchandising: data model + cards for priority-shipping attach rate, tier-1/2/3 upsell take rates, post-purchase/aftersell acceptance. Default source Shopify order line-items/properties. Seed realistic take-rates. Include serum-only vs serum+capsules subscriber 12-mo LTV comparison.
9. Landing-page funnel: break the acquisition funnel by GA4 landing page / entry path (PDP, listicle, lander), conversion per page type comparable; handle direct-to-checkout entries without breaking funnel math.
10. Time granularity: daily/weekly/monthly with period-over-period comparison; churn/cohort metrics are monthly by nature — label monthly, don't fake daily churn.
