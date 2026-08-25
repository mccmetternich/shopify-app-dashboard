# DATA_FLOW.md — Densologie Scoreboard Pipeline Audit

> Written 2026-08-24. Read-only audit — no code was changed.  
> All citations are `file:line`.

---

## Verdict

**"Opens instantly and self-refreshes with no manual action" — TRUE for Shopify / Meta / Recharge. FALSE for GA4 and Omnisend.**

- The three core sources (Shopify, Meta, Recharge) are background-scheduled, always running, and never block a page load. Pages read from Postgres only.
- GA4 and Omnisend ingest functions exist and are correct, but are **not wired to the APScheduler**. They will run only if someone adds them to `scheduler.py`. Until then, those two tables are populated from seed data only.
- One data quality gap: a refund on a >90-day-old order will not be captured by the incremental Shopify sync (see Q5).

**What's needed to make it fully true:**  
Add `ingest_ga4` and `ingest_omnisend` calls inside `scheduler.py`'s `_sync_all` function (or as separate jobs), and supply `GA4_PROPERTY_ID`, `GOOGLE_APPLICATION_CREDENTIALS`, and `OMNISEND_API_KEY` as Fly secrets.

---

## Q1 — Scheduler Architecture

**Is ingest a scheduled background job or on-request?**

Background job. `APScheduler BackgroundScheduler` is started in the app lifespan at `web.py:222-228`. It runs in a separate thread, independent of any page visits.

**Intervals per source:**

| Source    | Interval | Scheduler entry | Note |
|-----------|----------|-----------------|------|
| Shopify   | 15 min   | `scheduler.py` (job: `shopify_sync`) | `config.py:shopify_poll_interval_minutes=15` |
| Meta      | 15 min   | `scheduler.py` (job: `meta_sync`)    | `config.py:meta_poll_interval_minutes=15` |
| Recharge  | 15 min   | `scheduler.py` (job: `recharge_sync`)| `config.py:recharge_poll_interval_minutes=15` |
| GA4       | —        | **NOT WIRED** | `ingest_ga4.py` exists but no APScheduler job |
| Omnisend  | —        | **NOT WIRED** | `ingest_omnisend.py` exists but no APScheduler job |

**Do pages call APIs live on page load?**  
No. Stats functions (`stats.py`) import only `psycopg` and query Postgres. No API clients are imported at the route level. Opening any page is a pure DB read — instant, no API round-trip.

---

## Q2 — Crash Recovery and Stale Sync

**What happens if the scheduler stops?**

On deploy or restart, the scheduler restarts automatically at app startup (`web.py:222`). The first job fires immediately, then resumes the 15-min cadence. There is no state lost — ingest uses cursor/watermark from the `sync_state` table in Postgres.

**Does the stale-sync banner reflect per-source freshness?**  
Partially. `ops.py:sync_health()` reads from `sync_state` filtered on `SOURCE = "densologie_ingest"` (`pipeline.py:8`) — a **single string constant**, not per-source. All three ingest jobs write to this same key. If Shopify syncs successfully but Recharge fails, the banner shows "last synced" from Shopify and does not surface the Recharge failure separately.

- Stale banner threshold: 120 min (`ops.py:PAGE_STALE_MINUTES`)
- Slack alert threshold: 180 min (`ops.py:ALERT_STALE_MINUTES`)
- "Warn once per stale episode" logic in `ops.py:check_stale_sync()` — won't spam Slack on every check

**Does a failed sync retry on its own?**  
Yes — no retry within the interval, but the next 15-min tick fires unconditionally. There is no manual trigger needed.

**Is there any state where I'd need to manually trigger a sync?**  
No, unless the Fly machine itself crashes (which would require a `fly restart` or deploy). Normal Python exceptions inside a sync job are caught and logged; they do not stop the scheduler.

---

## Q3 — Fly Machine Sleep

**Does Fly scale the machine to zero when idle?**  
No. `fly.toml` has:
```
auto_stop_machines = 'off'
min_machines_running = 1
```
The machine runs 24/7 regardless of traffic. The scheduler never stops due to idle scaling. This configuration explicitly trades a small amount of always-on compute cost for guaranteed background sync — the right call for this use case.

No keep-alive ping is needed. No risk of "fresh only when I'm looking."

---

## Q4 — Incremental vs Full Pull

**Each source uses a different strategy:**

**Shopify (`ingest_shopify.py`):** True cursor-based incremental.
- GraphQL pagination cursor stored in `sync_state` after every page (`ingest_shopify.py` — cursor write on each page boundary).
- On cursor expiry, falls back to `last_order_created_at` timestamp.
- Initial sync: 90-day lookback (`ingest_shopify.py:172`).
- Cost per run: only new orders since last cursor. O(new orders), not O(total orders).

**Meta (`ingest_meta.py`):** 7-day rolling lookback window.
- No cursor. Pulls the last 7 days on every run (`ingest_meta.py:1-17`).
- Rationale: Meta retroactively adjusts spend data up to 28 days; a cursor would miss edits to past rows.
- `ON CONFLICT(date, campaign_id) DO UPDATE` — always writes freshest value.
- Cost per run: 7 days × number of active campaigns. Bounded and cheap.

**Recharge (`ingest_recharge.py`):** Cursor-based incremental.
- Same pattern as Shopify: cursor stored in `sync_state`, timestamp fallback on expiry.
- Initial sync: 90-day lookback.
- `_mark_churned()` runs at end of each sync as a retroactive pass: subscriptions with no charge in the last 45 days are marked `churned_at`. This is billing-gap inference, not a webhook — best-effort but covers the common case.

**GA4 (`ingest_ga4.py`):** 3-day rolling lookback.
- `start = end - timedelta(days=3)` (`ingest_ga4.py:42-43`).
- GA4 data can arrive late; 3-day window covers most cases.
- `ON CONFLICT DO UPDATE` — upserts.

**Omnisend (`ingest_omnisend.py`):** 7-day rolling lookback.
- `start = end - timedelta(days=7)` (`ingest_omnisend.py:37-38`).
- Separate calls for flows and campaigns. `ON CONFLICT DO UPDATE`.

**Summary:** No source does a full re-pull. All are bounded incremental strategies. Safe as data grows.

---

## Q5 — Late-Arriving Changes

**Refund on a 3-month-old order:**  
**Gap.** Shopify ingest is cursor-based on order creation date. An order created 3 months ago is not re-fetched. `totalRefundedSet` is read on each order node at query time, so the refunded amount is accurate when the order is first ingested — but a refund applied later to an old order will not be picked up until a full re-sync is triggered manually.

Workaround: periodically run a backfill script against orders with `updated_at > last_backfill_at` using Shopify's `updated_at` filter. This is a Phase B item, not currently implemented.

**Subscription status change in Recharge:**  
Mostly covered. Recharge ingest uses `updated_at` as the watermark, so changed subscriptions re-surface in the next sync. `_mark_churned()` catches gaps where the subscription went quiet without a formal cancellation event.

**Meta spend adjustment on a 10-day-old entry:**  
**Partially covered.** The 7-day rolling window (`ingest_meta.py:1-17`) catches adjustments to the last 7 days. Meta documents adjustments up to 28 days. An adjustment to an 11-day-old entry would be missed until the lookback window is widened to 28 days. Flag if spend reconciliation matters at that precision.

**Recharge subscription plan change (monthly_amount):**  
Covered. `_upsert_subscription` uses `ON CONFLICT DO UPDATE monthly_amount = excluded.monthly_amount` — plan changes are captured on the next sync.

---

## Q6 — End-to-End Trace: Blended CAC (Last 7 Days)

**1. Raw API pull:**  
Meta Ads API → `ingest_meta.py` → writes to `meta_ad_stats(date, campaign_id, spend, ...)` via `ON CONFLICT DO UPDATE`.

**2. Stored row:**  
`ad_spend(date, campaign_id, spend)` is the table queried by stats (populated from seed or via a rollup — see note below). `meta_ad_stats` is the raw campaign-level table.

> **Note:** `overview_stats` at `stats.py:93-99` queries the `ad_spend` table, not `meta_ad_stats`. These are two distinct tables — `meta_ad_stats` is raw campaign-level detail, `ad_spend` is the aggregated daily total. In a live environment, a rollup query or view will need to aggregate `meta_ad_stats → ad_spend`, or the ingest layer should write to `ad_spend` directly. In the current seed, both tables are populated independently.

**3. Date filtering:**  
In the DB query. `stats.py:93-99`:
```sql
select coalesce(sum(spend), null)
from ad_spend
where date >= %s::date
```
`window_start = now - timedelta(days=7)` computed in Python at `stats.py:60`, passed as a parameter. The DB does the filtering — only matching rows are transferred. Not O(total rows).

**4. Aggregation:**  
Python: `total_spend / Decimal(new_customers)` at `stats.py:103-104`. Division happens in Python after two separate single-scalar DB queries (one for spend, one for new customer count).

**5. Displayed number:**  
Returned in `overview_stats()` dict as `blended_cac`. Template renders via `{{ stats.blended_cac | currency }}`.

**Path:** Meta API → `meta_ad_stats` → (rollup needed) → `ad_spend` → `stats.py:overview_stats()` → `web.py:overview()` → `overview.html`.

---

## Q7 — Timezone

**Single source of truth:** `config.py:store_timezone = "America/Los_Angeles"`.

**Where it's applied:**
- Meta ingest: dates stored in store timezone, explicitly documented at `ingest_meta.py:1-17`.
- Shopify: orders stored with UTC `created_at` as received from Shopify API.
- Stats window: `_utcnow()` at `stats.py:45` returns UTC. `window_start = now - timedelta(days=window_days)` is UTC-based.

**Timezone consistency note:** There is a minor inconsistency between sources. `ad_spend.date` is stored in store timezone (Meta dates), while `orders.created_at` is UTC. For a 7-day window, the mismatch is <24 hours and is cosmetically acceptable. For precise day-boundary attribution (e.g., "orders on July 4 vs spend on July 4"), this could cause off-by-one. A future cleanup pass should normalize all date columns to store timezone at ingest time.

---

## Q8 — API Key Security

**Are keys read from environment only?**  
Yes. All credentials are read via `pydantic_settings` from environment variables (`config.py`). No keys appear in the codebase.

**Loud failure on missing/unauthorized credentials:**

| Behavior | Trigger | Where |
|----------|---------|-------|
| `ValueError` at startup | Partial credential pair set (e.g., `SHOPIFY_DOMAIN` without `SHOPIFY_TOKEN`) | `config.py:_credential_pairs_complete` |
| `logger.warning` + silent skip | Token unset, no partial pair | `scheduler.py` ingest jobs — early return if `not settings.shopify_token` etc. |
| `RuntimeError` raised | Meta API returns an error response | `ingest_meta.py` — raises, never returns $0 |
| `logger.warning` + continue | Omnisend flow/campaign call fails | `ingest_omnisend.py:54,70` — per-call exception handler |

The partial-pair check (`config.py`) prevents the most dangerous case (half-credentials silently passing). Fully-absent optional keys skip silently with a debug log — appropriate for phase-gated features like GA4/Omnisend.

---

## Q9 — Resource Footprint

**At 100 orders/month (~3.3/day):**

| Source | API calls/sync | Rows written/sync | Notes |
|--------|---------------|-------------------|-------|
| Shopify | 1–2 pages | ~0–1 new orders per 15-min run | Effectively idle most intervals |
| Meta | 1 call | ~7 × campaigns (5–20 rows) | Rolling 7-day window, small |
| Recharge | 1–2 pages | ~0–5 charge rows | Idle most intervals |
| GA4 | 1 call | ~3 rows (3-day window) | When wired |
| Omnisend | 2 calls | ~7–20 rows | When wired |

**At 1,000 orders/month (~33/day):**

| Source | API calls/sync | Rows written/sync | Notes |
|--------|---------------|-------------------|-------|
| Shopify | 2–4 pages | ~8 new orders per 15-min run | Still O(new orders), not O(total) |
| Meta | 1 call | Same as above — campaigns don't scale with orders | |
| Recharge | 2–4 pages | ~8 charge rows | |

**DB size growth (both scales):**
- `orders`: 1 row per order. Linear. 1,000 orders/month × 12 months = 12K rows/year — trivial.
- `meta_ad_stats`: ~10 rows/day × campaigns. Independent of order volume.
- `subscription_events`: ~2–5 events per sub lifecycle. Linear with subscriber count.

**O(n) concerns on page load:**

The `overview_stats()` function runs 4 scalar SQL queries against `orders` and `ad_spend` filtered by `created_at >= window_start`. If `orders.created_at` and `ad_spend.date` lack indexes, these become full table scans as data grows.

**Recommended:** Add indexes (if not already present):
```sql
create index if not exists orders_created_at_idx on orders(created_at);
create index if not exists ad_spend_date_idx on ad_spend(date);
```

No precomputation is used today — all stats are computed on each page load. At 10K+ orders this is still fast with indexes. At 100K+ orders (unlikely for this brand in the near term), a materialized daily summary table would be warranted. Flag for future planning, not an immediate problem.

---

## Open Items for Future Sessions

| Priority | Item |
|----------|------|
| High | Wire `ingest_ga4` and `ingest_omnisend` into `scheduler.py` + supply secrets |
| Medium | `meta_ad_stats → ad_spend` rollup clarification: confirm ingest writes to both, or add rollup |
| Medium | Shopify late refunds: add `updated_at` backfill pass for orders older than current window |
| Medium | Stale banner: expand `sync_health()` to track last sync per source (not a single key) |
| Low | Normalize all date columns to store timezone at ingest |
| Low | Add `orders.created_at` and `ad_spend.date` indexes if absent |
| Low | Widen Meta lookback from 7 to 28 days if spend reconciliation precision matters |
