-- Migration 031: is_reconstructed flag on subscription_snapshots
--
-- Every row written by the daily scheduler job is a real measurement taken at
-- the moment the job ran. Any row produced by a backfill script is a derived
-- estimate — possibly from a source that can't fully represent the state (e.g.
-- subscription_revenue can't encode pause history; event log may be empty).
--
-- This column makes that distinction explicit and query-able. The UI can caveat
-- reconstructed rows ("estimated"). Aggregations can exclude them. The flag
-- cannot be set back to false once a row is marked reconstructed.
--
-- Default false: the daily scheduler never sets it, so new rows are always
-- measured by default. The backfill script sets it to true explicitly.

alter table subscription_snapshots
    add column if not exists is_reconstructed boolean not null default false;

comment on column subscription_snapshots.is_reconstructed is
    'true = row was produced by a backfill script, not by the live daily job. '
    'Treat as an estimate; the source table may not have full state history. '
    'false = measured at job-run time (authoritative).';
