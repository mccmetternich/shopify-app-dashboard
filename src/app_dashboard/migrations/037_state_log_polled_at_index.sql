-- Migration 037: index on subscription_state_log(polled_at) for retention pruning
--
-- The retention pruner deletes rows older than 7 days:
--   DELETE FROM subscription_state_log WHERE polled_at < now() - interval '7 days'
-- Without this index that is a sequential scan on a table that grows at ~20K rows/day.
-- The index makes the DELETE fast once the table is large.
--
-- Retention policy: 7 days of raw state log rows. The Phase-2 differ extracts
-- transitions into subscription_events; after 7 days the raw rows have no further
-- use whether or not the differ has been built. The pruner runs at end of every
-- sync_subscription_events() call, independently of whether the differ ships.

create index if not exists ix_state_log_polled_at
    on subscription_state_log(polled_at);
