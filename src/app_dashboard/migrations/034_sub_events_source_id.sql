-- Migration 034: source_id on subscription_events for per-charge idempotency
--
-- mrr_recognized events are emitted once per Recharge charge cycle. The charge ID
-- is the natural deduplication key. Without it, re-running the ingest after a
-- cursor expiry would double-count billing events.
--
-- source_id is NULL for all events that predate this column and for any event
-- not tied to a source record (e.g. events derived from state diffing).
-- Non-null values are unique — the partial-style unique index below enforces this
-- while allowing multiple NULLs (NULLs are always distinct in PostgreSQL unique
-- indexes).
--
-- Convention: prefix source_id with the system abbreviation:
--   rc_charge_<id>  — Recharge charge (mrr_recognized events)

alter table subscription_events
    add column if not exists source_id text;

-- Unique on non-null values; NULLs do not conflict.
-- Used as ON CONFLICT (source_id) DO NOTHING target in ingest_recharge.py.
create unique index if not exists ix_sub_events_source_id
    on subscription_events(source_id)
    where source_id is not null;
