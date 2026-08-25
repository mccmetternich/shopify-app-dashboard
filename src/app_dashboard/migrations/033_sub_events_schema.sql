-- Migration 033: approximation_reason on subscription_events + subscription_state_log
--
-- approximation_reason
-- --------------------
-- NULL  = event date and MRR delta are exact (came from a precise API field
--         such as cancelled_at or subscription.created_at).
-- text  = approximate; the value describes the mechanism and precision.
--         Examples:
--           'poll-detected: ~15min window'    — normal pause/reactivate inference
--           'poll-detected: 4h outage gap'    — pause detected after scheduler downtime
--           'billing-boundary: charge date'   — expansion dated at next charge
--
-- subscription_state_log
-- ----------------------
-- One row per (subscription_id, polled_at). Populated by sync_subscription_events()
-- on every scheduler run. The differ compares consecutive rows per subscription to
-- emit poll-approximate events (Phase 2 — pause, reactivate, dunning, expansion).
--
-- Gap flagging: if polled_at[n] - polled_at[n-1] > 2× poll interval, the event
-- is written but approximation_reason includes the actual gap duration.

alter table subscription_events
    add column if not exists approximation_reason text;

comment on column subscription_events.approximation_reason is
    'NULL = event date/MRR delta is exact from a precise API field. '
    'Non-null = approximate; value names the mechanism and precision window. '
    'Examples: ''poll-detected: ~15min window'', ''poll-detected: 4h outage gap'', '
    '''billing-boundary: charge date''.';

create table if not exists subscription_state_log (
    subscription_id     text        not null references subscription_revenue(id) on delete cascade,
    polled_at           timestamptz not null,
    status              text        not null check (status in ('active','paused','cancelled')),
    paused_at           timestamptz,
    cancelled_at        timestamptz,
    price               numeric(10,2),
    cancellation_reason text,
    primary key (subscription_id, polled_at)
);

comment on table subscription_state_log is
    'Snapshot of each Recharge subscription''s state at each poll. '
    'The Phase-2 differ compares consecutive rows per subscription_id to emit '
    'pause/reactivate/dunning/expansion events. '
    'Retain at least 2 rows per subscription for diffing; older rows may be pruned.';

-- Index for the differ: load previous state efficiently
create index if not exists ix_state_log_sub_recent
    on subscription_state_log(subscription_id, polled_at desc);
