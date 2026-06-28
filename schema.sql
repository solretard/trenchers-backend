-- Trenchers leaderboard schema (run this in your Supabase SQL editor).
-- Table is namespaced as "trenchers_runs" so it can safely share an existing
-- Supabase project without clashing with your other tables.

create table if not exists trenchers_runs (
    id          bigint generated always as identity primary key,
    handle      text    not null,
    faction     text    not null,
    scars       integer not null default 0,
    depth       integer not null default 1,
    kills       integer not null default 0,
    wallet      text,
    season      text    not null default '0',
    created_at  bigint  not null
);

-- Fast leaderboard + per-handle lookups
create index if not exists idx_trenchers_runs_season_scars on trenchers_runs (season, scars desc);
create index if not exists idx_trenchers_runs_handle       on trenchers_runs (season, handle);

-- The backend connects with the SERVICE ROLE key, which bypasses RLS.
-- We still enable RLS so the public/anon key cannot read or write directly.
alter table trenchers_runs enable row level security;

-- (Intentionally NO public policies. Only the service-role backend touches this table.)
