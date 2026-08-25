-- Migration 030: subscription_snapshots + sync_state error tracking
--
-- subscription_snapshots: one row per calendar day capturing the end-of-day
-- subscription state. Written by the daily snapshot job at midnight store
-- time. Without this table, churn/retention history must be reconstructed
-- from current subscription_revenue state — which silently breaks whenever
-- a subscriber changes status or a win-back arrives.
--
-- sync_state error columns: allow the stale banner to surface the last
-- failure message per source rather than just silently going amber/red.

create table if not exists subscription_snapshots (
    snapshot_date   date primary key,
    active_count    int  not null default 0,
    paused_count    int  not null default 0,
    churned_count   int  not null default 0,
    mrr_recognized  numeric(12,2),           -- sum of monthly_amount for active subs
    new_subs        int  not null default 0, -- converted_at = snapshot_date
    churned_subs    int  not null default 0, -- churned_at   = snapshot_date
    reactivations   int  not null default 0, -- winback events on snapshot_date
    created_at      timestamptz default now()
);

comment on table subscription_snapshots is
    'Daily point-in-time subscription counts. Append-only by calendar date; '
    'upsert-safe so a re-run on the same day overwrites rather than duplicates. '
    'Used for cohort retention and churn history. Missing dates = day the job was not running.';

-- Per-source error tracking on sync_state
alter table sync_state add column if not exists last_error      text;
alter table sync_state add column if not exists last_error_at   timestamptz;
