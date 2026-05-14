-- Brogey schema. Paste this whole file into:
--   Supabase dashboard -> SQL editor -> New query -> Run
-- Safe to run on a fresh project. Idempotent: re-running drops & recreates.

-- =============================================================
-- Drop in reverse dependency order (for clean re-runs in dev)
-- =============================================================
drop table if exists public.insights cascade;
drop table if exists public.shots cascade;
drop table if exists public.sessions cascade;
drop type if exists public.data_source;

-- =============================================================
-- Types
-- =============================================================
create type public.data_source as enum ('trackman', 'toptracer', '18birdies', 'garmin', 'manual');

-- =============================================================
-- sessions: one row per range/sim/course visit
-- =============================================================
create table public.sessions (
    id              uuid primary key default gen_random_uuid(),
    session_date    date not null,
    source          public.data_source not null default 'trackman',
    external_id     text,                  -- e.g. TrackMan's session UUID, lets us de-dupe re-imports
    location        text,
    notes           text,
    pdf_filename    text,                  -- original file we ingested from, for traceability
    created_at      timestamptz not null default now(),
    unique (source, external_id)           -- prevents duplicate re-ingests
);
create index sessions_date_idx on public.sessions (session_date desc);

-- =============================================================
-- shots: one row per ball struck
-- Columns mirror the existing trackman_shots.csv schema.
-- =============================================================
create table public.shots (
    id                  bigserial primary key,
    session_id          uuid not null references public.sessions(id) on delete cascade,
    shot_num            int not null,
    club                text not null,                  -- 'Dr', '5W', '5i', '7i', '8i', 'PW', 'SW', etc.
    club_speed_mps      numeric(5,2),
    attack_angle_deg    numeric(5,2),
    ball_speed_mps      numeric(5,2),
    spin_rate_rpm       int,
    carry_m             numeric(6,2),
    side_m              numeric(6,2),
    smash_factor        numeric(4,3),
    created_at          timestamptz not null default now(),
    unique (session_id, club, shot_num)  -- shot_num restarts per club within a session
);
create index shots_session_idx on public.shots (session_id);
create index shots_club_idx on public.shots (club);

-- =============================================================
-- insights: Brogey's generated commentary per session (or rolling)
-- Stored as JSON so the schema stays flexible while we iterate on
-- what Brogey produces.
-- =============================================================
create table public.insights (
    id              bigserial primary key,
    session_id      uuid references public.sessions(id) on delete cascade,  -- nullable: rolling insights aren't tied to one session
    scope           text not null default 'session',                          -- 'session' | 'rolling-30d' | 'all-time' etc.
    headline        text,
    body            jsonb not null,        -- { takeaways: [...], practice_plan: [...], next_session: "...", stats: {...} }
    model           text,                  -- which Claude model generated this
    created_at      timestamptz not null default now()
);
create index insights_session_idx on public.insights (session_id);
create index insights_created_idx on public.insights (created_at desc);

-- =============================================================
-- Row Level Security
-- Single-user app: anon key gets read-only access (for dashboards
-- on your phone / home laptop). All writes go through the service
-- role key, which bypasses RLS automatically.
-- =============================================================
alter table public.sessions  enable row level security;
alter table public.shots     enable row level security;
alter table public.insights  enable row level security;

create policy anon_read_sessions on public.sessions for select to anon using (true);
create policy anon_read_shots    on public.shots    for select to anon using (true);
create policy anon_read_insights on public.insights for select to anon using (true);
