-- Promote the fields that BOTH TrackMan and TopTracer measure from
-- raw_measurement jsonb up to first-class columns on `shots`. This makes
-- them queryable, indexable, and dashboard-friendly without digging into
-- nested JSON every time.
--
-- After applying, run `brogey backfill-flight` to populate from existing
-- raw_measurement payloads (no API/CSV re-read needed).
--
-- Paste into Supabase -> SQL Editor -> Run. Safe to re-run.

alter table public.shots add column if not exists total_m            numeric(6,2);
alter table public.shots add column if not exists launch_angle_deg   numeric(5,2);
alter table public.shots add column if not exists max_height_m       numeric(6,2);
alter table public.shots add column if not exists landing_angle_deg  numeric(5,2);
alter table public.shots add column if not exists hang_time_s        numeric(5,2);
alter table public.shots add column if not exists curve_m            numeric(6,2);
