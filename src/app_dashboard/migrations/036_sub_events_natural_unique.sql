-- Migration 036: natural uniqueness index on subscription_events for source_id=NULL events
--
-- Events with a source_id are already guarded by ix_sub_events_source_id (partial
-- unique index on non-null source_id values). Events without a source_id — gap-inferred
-- churns, backfilled new/churn/winback events — have no uniqueness guard, which means
-- a concurrent race (e.g. two scheduler instances during a rolling deploy) can write
-- duplicate rows.
--
-- The natural key for these events is (subscription_id, event_type, event_date):
--   - A subscription can only acquire a given event_type on a given date once.
--   - 'new': one per subscription (converted_at date).
--   - 'churn': one per subscription (churned_at date). Winbacks get a new subscription_id.
--   - 'winback': one per subscription (the new-sub event date).
--   - Gap-inferred churns from _mark_churned(): cutoff date, same for both racing writers.
--
-- Scoped to source_id IS NULL so it doesn't conflict with the source_id index and
-- doesn't require touching rows that already have a source_id uniqueness guard.
--
-- Used as the ON CONFLICT target in _mark_churned(), _backfill_new_events(),
-- _backfill_churn_events(), and _backfill_winback_events().

create unique index if not exists ix_sub_events_natural_key
    on subscription_events(subscription_id, event_type, event_date)
    where source_id is null;
