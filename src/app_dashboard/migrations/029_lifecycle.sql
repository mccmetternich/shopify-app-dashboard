-- Migration 029: Pause and Reactivation lifecycle
-- Adds 'paused' as a third state for subscription_revenue, plus win-back tracking.

-- 1. Add 'paused' to subscription_revenue.status
--    (The existing check is on churn_type, not status — subscription_revenue had no status
--    column before this migration. We add one here to carry the three-state signal.)
ALTER TABLE subscription_revenue
  ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active'
  CHECK (status IN ('active','paused','churned'));

-- 2. Add pause metadata columns
ALTER TABLE subscription_revenue
  ADD COLUMN IF NOT EXISTS paused_at timestamptz,
  ADD COLUMN IF NOT EXISTS paused_outcome text
    CHECK (paused_outcome IN ('reactivated','cancelled'));

-- 3. Backfill status for existing churned rows
UPDATE subscription_revenue
  SET status = 'churned'
  WHERE churned_at IS NOT NULL AND status = 'active';

-- 4. Add winback tracking to customers
ALTER TABLE customers
  ADD COLUMN IF NOT EXISTS winback_count int NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS last_winback_at timestamptz;

-- 5. Extend subscription_events event_type to include pause/reactivate/winback
ALTER TABLE subscription_events
  DROP CONSTRAINT IF EXISTS subscription_events_event_type_check;
ALTER TABLE subscription_events
  ADD CONSTRAINT subscription_events_event_type_check
  CHECK (event_type IN (
    'new','churn','expansion','contraction',
    'skip','dunning_start','dunning_resolved',
    'mrr_recognized','pause','reactivate','winback'
  ));
