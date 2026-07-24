-- Invite-only design-partner beta.
-- Collection remains shared, while goals, ranking, digests and feedback become
-- user-scoped. No public database policies are added in this phase.

create table if not exists public.beta_users (
  id uuid primary key default gen_random_uuid(),
  email text not null unique,
  display_name text not null default '',
  timezone text not null default 'Asia/Shanghai',
  wechat_enabled boolean not null default false,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.beta_users
  add column if not exists wechat_enabled boolean not null default false;

alter table public.tracks
  add column if not exists beta_user_id uuid references public.beta_users(id) on delete cascade,
  add column if not exists compiled_goal text not null default '',
  add column if not exists goal_spec jsonb not null default '{}'::jsonb;

-- The old global unique constraint prevents two beta users from using the same
-- friendly Track name. Keep uniqueness within each user instead.
alter table public.tracks drop constraint if exists tracks_name_key;
create unique index if not exists tracks_beta_user_name_key
  on public.tracks (beta_user_id, name)
  where beta_user_id is not null;
create unique index if not exists tracks_legacy_name_key
  on public.tracks (name)
  where beta_user_id is null;
create index if not exists tracks_beta_user_active_idx
  on public.tracks (beta_user_id, active);

alter table public.digests
  add column if not exists beta_user_id uuid references public.beta_users(id) on delete cascade;
create index if not exists digests_beta_user_generated_idx
  on public.digests (beta_user_id, generated_at desc);

alter table public.feedback
  add column if not exists beta_user_id uuid references public.beta_users(id) on delete cascade,
  add column if not exists updated_at timestamptz not null default now();

alter table public.feedback drop constraint if exists feedback_value_check;
alter table public.feedback add constraint feedback_value_check check (
  value in (
    'helpful', 'irrelevant', 'deep_dive',
    'relevant', 'duplicate', 'known', 'watch', 'try', 'adopted'
  )
);

create unique index if not exists feedback_beta_user_item_key
  on public.feedback (beta_user_id, track_id, item_id);
create index if not exists feedback_beta_user_time_idx
  on public.feedback (beta_user_id, created_at desc);

create table if not exists public.digest_items (
  id bigint generated always as identity primary key,
  digest_id bigint not null references public.digests(id) on delete cascade,
  track_id uuid not null references public.tracks(id) on delete cascade,
  item_id uuid not null references public.items(id) on delete cascade,
  section text not null check (section in ('highlight', 'related')),
  position integer not null,
  created_at timestamptz not null default now(),
  unique (digest_id, track_id, item_id)
);

create table if not exists public.analytics_events (
  id bigint generated always as identity primary key,
  beta_user_id uuid references public.beta_users(id) on delete cascade,
  event_name text not null check (
    event_name in ('feedback_helpful', 'feedback_irrelevant', 'feedback_deep_dive')
  ),
  track_id uuid references public.tracks(id) on delete cascade,
  item_id uuid references public.items(id) on delete cascade,
  properties jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists analytics_events_user_time_idx
  on public.analytics_events (beta_user_id, created_at desc);
create index if not exists digest_items_item_idx
  on public.digest_items (item_id);

alter table public.beta_users enable row level security;
alter table public.digest_items enable row level security;
alter table public.analytics_events enable row level security;

-- The GitHub Action and Edge Function use a server-side key. Invitees receive
-- email only and never receive database credentials in this phase.
revoke all on table public.beta_users from anon, authenticated;
revoke all on table public.digest_items from anon, authenticated;
revoke all on table public.analytics_events from anon, authenticated;
revoke all on sequence public.digest_items_id_seq from anon, authenticated;
revoke all on sequence public.analytics_events_id_seq from anon, authenticated;
grant all on table public.beta_users to service_role;
grant all on table public.digest_items to service_role;
grant all on table public.analytics_events to service_role;
grant select on table public.tracks to service_role;
grant select on table public.items to service_role;
grant select, insert, update on table public.feedback to service_role;
grant usage, select on sequence public.digest_items_id_seq to service_role;
grant usage, select on sequence public.analytics_events_id_seq to service_role;
grant usage, select on sequence public.feedback_id_seq to service_role;

drop trigger if exists beta_users_set_updated_at on public.beta_users;
create trigger beta_users_set_updated_at before update on public.beta_users
for each row execute function public.set_updated_at();

drop trigger if exists feedback_set_updated_at on public.feedback;
create trigger feedback_set_updated_at before update on public.feedback
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
  events_deleted integer := 0;
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

  delete from public.analytics_events where created_at < now() - interval '180 days';
  get diagnostics events_deleted = row_count;

  return jsonb_build_object(
    'fetch_runs_deleted', fetch_runs_deleted,
    'matches_deleted', matches_deleted,
    'embeddings_cleared', embeddings_cleared,
    'items_deleted', items_deleted,
    'digests_deleted', digests_deleted,
    'events_deleted', events_deleted
  );
end;
$$;

revoke all on function public.cleanup_trend_tracker() from public, anon, authenticated;
grant execute on function public.cleanup_trend_tracker() to service_role;
