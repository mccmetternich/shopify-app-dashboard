-- Migration 032: allow NULL for reactivations in subscription_snapshots
--
-- reactivations counts winback events from subscription_events.
-- When the events table has no data (pre-event-emission wiring), writing 0
-- is indistinguishable from "zero reactivations occurred" — it looks measured
-- and isn't. NULL means "not tracked", which is honest.
--
-- The daily scheduler will write NULL until event emission is wired.
-- The backfill script will write NULL or a real count depending on what it finds.
-- Query consumers must already handle NULL from reactivation_stats(); treat this
-- the same way.

alter table subscription_snapshots
    alter column reactivations drop not null;
