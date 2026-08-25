-- Migration 035: reclassify billing-gap-inferred churns from involuntary to voluntary
--
-- Background: _mark_churned() infers churn from a 45-day billing gap with no charge.
-- It has no payment-failure evidence — it never reads dunning_started_at, it has
-- no webhook confirmation, and it cannot distinguish a billing failure from a silent
-- cancellation. When it wrote churn_type='involuntary' (prior to migration 035), those
-- rows landed in the involuntary bucket in the waterfall, which requires:
--
--   sr.churn_type = 'involuntary'
--   AND sr.dunning_started_at IS NOT NULL
--   AND (sr.churned_at - sr.dunning_started_at) >= interval '14 days'
--
-- For seed rows lh_sub_00034 and lh_sub_00039, dunning_started_at happened to be set
-- (seed data was crafted to test the dunning tile), making them satisfy the involuntary
-- bucket despite the evidence coming from gap inference, not confirmed payment failure.
--
-- Rule adopted: gap-inference → churn_type='voluntary'. The approximation_reason on
-- the subscription_event records the mechanism. Phase D (webhooks) will set
-- churn_type='involuntary' with real dunning evidence when it arrives.
--
-- This migration is idempotent: the WHERE clause targets only gap-inferred rows
-- identified by their approximation_reason on the corresponding churn event.
-- Running it on a DB that has already been patched via SSH (the original fix) is safe.

update subscription_revenue sr
set churn_type = 'voluntary'
where sr.churn_type = 'involuntary'
  and exists (
      select 1
      from subscription_events se
      where se.subscription_id = sr.id
        and se.event_type      = 'churn'
        and se.approximation_reason like 'billing-gap%'
  );
