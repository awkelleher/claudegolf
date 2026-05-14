-- Adds a jsonb column to shots for the full per-shot Measurement blob
-- returned by https://golf-player-activities.trackmangolf.com/api/reports/getactivityreport.
-- The first-class columns (carry_m, smash_factor, ...) stay for fast queries;
-- everything else becomes available via raw_measurement without future migrations.
--
-- Paste into Supabase -> SQL Editor -> Run. Safe to re-run.

alter table public.shots
    add column if not exists raw_measurement jsonb;

create index if not exists shots_raw_measurement_gin on public.shots using gin (raw_measurement);
