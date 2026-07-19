begin;

create table if not exists app.referral_sources (
  id bigint generated always as identity primary key,
  source_code text unique,
  title text not null,
  created_by_admin_user_id bigint references app.admin_users(id) on delete set null,
  created_by_username text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint referral_sources_title_check check (btrim(title) <> ''),
  constraint referral_sources_source_code_check check (
    source_code is null or source_code ~ '^[A-Za-z0-9_-]{1,64}$'
  )
);

alter table app.telegram_users
  add column if not exists referral_source_id bigint,
  add column if not exists referral_source_captured_at timestamptz;

alter table app.telegram_users
  drop constraint if exists telegram_users_referral_source_id_fkey,
  add constraint telegram_users_referral_source_id_fkey
    foreign key (referral_source_id)
    references app.referral_sources(id)
    on delete set null;

create index if not exists telegram_users_referral_source_idx
  on app.telegram_users (referral_source_id)
  where referral_source_id is not null;

alter table app.referral_sources enable row level security;

commit;
