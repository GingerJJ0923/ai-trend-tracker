-- Make Supabase the safe, owner-managed source of truth for beta recipients
-- and Tracks. This migration is additive and preserves existing data.

create or replace function public.prepare_beta_user()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  new.email := lower(btrim(new.email));
  new.display_name := btrim(coalesce(new.display_name, ''));
  new.timezone := btrim(coalesce(new.timezone, 'Asia/Shanghai'));

  if new.email !~ '^[^@[:space:],;]+@[^@[:space:],;]+$' then
    raise exception 'Invalid beta user email address';
  end if;

  if not exists (
    select 1 from pg_timezone_names where name = new.timezone
  ) then
    raise exception 'Invalid beta user timezone: %', new.timezone;
  end if;

  return new;
end;
$$;

drop trigger if exists beta_users_prepare_fields on public.beta_users;
create trigger beta_users_prepare_fields
before insert or update of email, display_name, timezone on public.beta_users
for each row execute function public.prepare_beta_user();

-- Normalize rows created before this trigger existed.
update public.beta_users
set email = email,
    display_name = display_name,
    timezone = timezone;

-- A single ServerChan key belongs to the owner. Prevent accidentally sending
-- several users' private reports to that same WeChat destination.
create unique index if not exists beta_users_single_wechat_owner_idx
  on public.beta_users (wechat_enabled)
  where wechat_enabled = true;

create or replace function public.prepare_track()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  new.name := btrim(new.name);
  new.goal := btrim(new.goal);

  if new.name = '' then
    raise exception 'Track name cannot be empty';
  end if;
  if new.goal = '' then
    raise exception 'Track goal cannot be empty';
  end if;

  -- A changed natural-language goal must not keep stale LLM output or vectors.
  if tg_op = 'UPDATE' then
    if new.goal is distinct from old.goal then
      new.compiled_goal := '';
      new.goal_spec := '{}'::jsonb;
      new.embedding := null;
    end if;
  end if;

  return new;
end;
$$;

drop trigger if exists tracks_prepare_fields on public.tracks;
create trigger tracks_prepare_fields
before insert or update of name, goal on public.tracks
for each row execute function public.prepare_track();

-- RLS still protects the tables. Trigger functions contain no privileged
-- operations and run with the caller's permissions.
