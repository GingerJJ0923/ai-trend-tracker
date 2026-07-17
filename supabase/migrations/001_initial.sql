create extension if not exists pgcrypto with schema extensions;
create extension if not exists vector with schema extensions;

create table if not exists public.tracks (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete cascade,
  name text not null unique,
  goal text not null,
  active boolean not null default true,
  embedding extensions.vector(512),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.items (
  id uuid primary key default gen_random_uuid(),
  source_key text not null,
  external_id text not null,
  title text not null,
  url text not null,
  product_url text,
  summary text not null default '',
  author text,
  published_at timestamptz not null,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  fingerprint text not null,
  metadata jsonb not null default '{}'::jsonb,
  embedding extensions.vector(512),
  unique (source_key, external_id)
);

create index if not exists items_published_at_idx on public.items (published_at desc);
create index if not exists items_fingerprint_idx on public.items (fingerprint);
create index if not exists items_source_key_idx on public.items (source_key);

create table if not exists public.matches (
  id bigint generated always as identity primary key,
  track_id uuid not null references public.tracks(id) on delete cascade,
  item_id uuid not null references public.items(id) on delete cascade,
  score numeric(5,2) not null,
  semantic_score numeric(8,6) not null default 0,
  tier text not null check (tier in ('high', 'possible', 'irrelevant')),
  reason text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (track_id, item_id)
);

create index if not exists matches_track_score_idx on public.matches (track_id, score desc);
create index if not exists matches_item_idx on public.matches (item_id);

create table if not exists public.analyses (
  id bigint generated always as identity primary key,
  track_id uuid not null references public.tracks(id) on delete cascade,
  item_id uuid not null references public.items(id) on delete cascade,
  content text not null,
  model text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (track_id, item_id)
);

create table if not exists public.feedback (
  id bigint generated always as identity primary key,
  track_id uuid not null references public.tracks(id) on delete cascade,
  item_id uuid not null references public.items(id) on delete cascade,
  value text not null check (value in ('relevant', 'irrelevant', 'duplicate', 'known', 'watch', 'try', 'adopted')),
  note text,
  created_at timestamptz not null default now()
);

create table if not exists public.fetch_runs (
  id bigint generated always as identity primary key,
  source_key text not null,
  status text not null check (status in ('success', 'failed')),
  item_count integer not null default 0,
  error text,
  created_at timestamptz not null default now()
);

create index if not exists fetch_runs_source_time_idx on public.fetch_runs (source_key, created_at desc);

create table if not exists public.digests (
  id bigint generated always as identity primary key,
  generated_at timestamptz not null,
  content text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists digests_generated_at_idx on public.digests (generated_at desc);

alter table public.tracks enable row level security;
alter table public.items enable row level security;
alter table public.matches enable row level security;
alter table public.analyses enable row level security;
alter table public.feedback enable row level security;
alter table public.fetch_runs enable row level security;
alter table public.digests enable row level security;

-- No anon/authenticated policies are created. GitHub Actions uses the service-role
-- key, which bypasses RLS. Add user-scoped policies when a frontend is introduced.

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists tracks_set_updated_at on public.tracks;
create trigger tracks_set_updated_at before update on public.tracks
for each row execute function public.set_updated_at();

drop trigger if exists matches_set_updated_at on public.matches;
create trigger matches_set_updated_at before update on public.matches
for each row execute function public.set_updated_at();

drop trigger if exists analyses_set_updated_at on public.analyses;
create trigger analyses_set_updated_at before update on public.analyses
for each row execute function public.set_updated_at();

create or replace function public.cleanup_trend_tracker()
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  fetch_runs_deleted integer := 0;
  matches_deleted integer := 0;
  embeddings_cleared integer := 0;
  items_deleted integer := 0;
  digests_deleted integer := 0;
begin
  delete from public.fetch_runs where created_at < now() - interval '30 days';
  get diagnostics fetch_runs_deleted = row_count;

  delete from public.matches
  where score < 50 and updated_at < now() - interval '60 days';
  get diagnostics matches_deleted = row_count;

  update public.items i
  set embedding = null
  where i.embedding is not null
    and i.published_at < now() - interval '90 days'
    and not exists (
      select 1 from public.matches m
      where m.item_id = i.id and m.score >= 80
    );
  get diagnostics embeddings_cleared = row_count;

  delete from public.items i
  where i.published_at < now() - interval '180 days'
    and not exists (select 1 from public.matches m where m.item_id = i.id and m.score >= 60)
    and not exists (select 1 from public.analyses a where a.item_id = i.id)
    and not exists (select 1 from public.feedback f where f.item_id = i.id);
  get diagnostics items_deleted = row_count;

  delete from public.digests where generated_at < now() - interval '90 days';
  get diagnostics digests_deleted = row_count;

  return jsonb_build_object(
    'fetch_runs_deleted', fetch_runs_deleted,
    'matches_deleted', matches_deleted,
    'embeddings_cleared', embeddings_cleared,
    'items_deleted', items_deleted,
    'digests_deleted', digests_deleted
  );
end;
$$;

revoke all on function public.cleanup_trend_tracker() from public, anon, authenticated;
grant execute on function public.cleanup_trend_tracker() to service_role;

create or replace function public.trend_tracker_storage_status()
returns jsonb
language sql
security definer
set search_path = public
as $$
  select jsonb_build_object(
    'database_size_bytes', pg_database_size(current_database()),
    'free_plan_limit_bytes', 524288000,
    'usage_percent', round((pg_database_size(current_database())::numeric / 524288000::numeric) * 100, 2),
    'warning', pg_database_size(current_database()) >= 367001600
  );
$$;

revoke all on function public.trend_tracker_storage_status() from public, anon, authenticated;
grant execute on function public.trend_tracker_storage_status() to service_role;
