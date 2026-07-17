-- Upgrade an existing 001 deployment for provider-neutral 512-dimensional
-- embeddings and private digest storage. Existing embeddings are cleared and
-- regenerated automatically on the next digest run.

alter table public.tracks
  alter column embedding type extensions.vector(512)
  using null::extensions.vector(512);

alter table public.items
  alter column embedding type extensions.vector(512)
  using null::extensions.vector(512);

create table if not exists public.digests (
  id bigint generated always as identity primary key,
  generated_at timestamptz not null,
  content text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists digests_generated_at_idx on public.digests (generated_at desc);
alter table public.digests enable row level security;

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
