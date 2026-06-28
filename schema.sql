-- Trenchers leaderboard schema (run this in the Supabase SQL editor)

create table if not exists runs (
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
create index if not exists idx_runs_season_scars on runs (season, scars desc);
create index if not exists idx_runs_handle       on runs (season, handle);

-- The backend connects with the SERVICE ROLE key, which bypasses RLS.
-- We still enable RLS so that the public/anon key cannot read or write directly.
alter table runs enable row level security;

-- (Intentionally NO public policies. Only the service-role backend touches this table.)
-- If you later want public read access to the leaderboard directly from the client,
-- add a SELECT policy here — but never expose the service role key in the game.
