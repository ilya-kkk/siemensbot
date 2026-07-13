begin;

create schema if not exists app;

revoke all on schema app from public;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on schema app from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on schema app from authenticated;
  end if;
end $$;

create table if not exists app.telegram_users (
  id bigint generated always as identity primary key,
  chat_id bigint unique,
  telegram_user_id bigint,
  username text,
  username_normalized text,
  first_name text,
  last_name text,
  status text not null default 'active',
  funnel_stage text not null default 'started',
  stage_updated_at timestamptz not null default now(),
  started_at timestamptz,
  dialogue_started_at timestamptz,
  offer_shown_at timestamptz,
  lead_at timestamptz,
  analysis_ai_request_id bigint,
  analysis_output jsonb not null default '{}'::jsonb,
  niche text,
  revenue_estimate text,
  average_check text,
  sales_volume text,
  main_problem text,
  lead_temperature text,
  summary text,
  confidence numeric(5, 4),
  analyzed_at timestamptz,
  offer_token text unique,
  offer_legacy_tokens text[] not null default '{}'::text[],
  offer_first_clicked_at timestamptz,
  offer_last_clicked_at timestamptz,
  offer_click_count integer not null default 0,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint telegram_users_status_check check (status in ('active', 'blocked', 'invalid')),
  constraint telegram_users_funnel_stage_check check (funnel_stage in ('started', 'dialogue', 'lead')),
  constraint telegram_users_lead_temperature_check check (
    lead_temperature is null or lead_temperature in ('cold', 'warm', 'hot', 'unknown')
  ),
  constraint telegram_users_offer_click_count_check check (offer_click_count >= 0)
);

create table if not exists app.admin_users (
  id bigint generated always as identity primary key,
  username text not null unique,
  username_normalized text not null unique,
  chat_id bigint unique,
  role text not null,
  is_active boolean not null default true,
  first_seen_at timestamptz,
  last_seen_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint admin_users_role_check check (role in ('tech', 'business'))
);

create table if not exists app.messages (
  id bigint generated always as identity primary key,
  telegram_user_id bigint not null references app.telegram_users(id) on delete cascade,
  ai_request_id bigint,
  direction text not null,
  message_type text not null default 'text',
  text text,
  telegram_message_id bigint,
  raw_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  constraint messages_direction_check check (direction in ('incoming', 'outgoing', 'system')),
  constraint messages_message_type_check check (message_type in ('text', 'button', 'service', 'error'))
);

create table if not exists app.ai_requests (
  id bigint generated always as identity primary key,
  telegram_user_id bigint not null references app.telegram_users(id) on delete cascade,
  source_message_id bigint not null references app.messages(id) on delete cascade,
  provider text not null default 'openrouter',
  model text not null,
  purpose text not null,
  status text not null default 'success',
  request_payload jsonb not null default '{}'::jsonb,
  response_payload jsonb not null default '{}'::jsonb,
  user_snapshot jsonb not null default '{}'::jsonb,
  prompt_tokens integer,
  completion_tokens integer,
  total_tokens integer,
  usage_cost numeric(12, 6),
  error_message text,
  created_at timestamptz not null default now(),
  constraint ai_requests_purpose_check check (purpose in ('chat', 'analysis')),
  constraint ai_requests_status_check check (status in ('success', 'failed'))
);

alter table app.messages
  drop constraint if exists messages_ai_request_id_fkey,
  add constraint messages_ai_request_id_fkey
  foreign key (ai_request_id) references app.ai_requests(id) on delete set null;

alter table app.telegram_users
  drop constraint if exists telegram_users_analysis_ai_request_id_fkey,
  add constraint telegram_users_analysis_ai_request_id_fkey
  foreign key (analysis_ai_request_id) references app.ai_requests(id) on delete set null;

create table if not exists app.alerts (
  id bigint generated always as identity primary key,
  severity text not null,
  category text not null,
  message text not null,
  details jsonb not null default '{}'::jsonb,
  delivered_to_chat_id bigint,
  delivered_at timestamptz,
  created_at timestamptz not null default now(),
  constraint alerts_severity_check check (severity in ('info', 'warning', 'critical'))
);

create index if not exists telegram_users_username_normalized_idx
  on app.telegram_users (username_normalized)
  where username_normalized is not null;

create index if not exists telegram_users_active_chat_idx
  on app.telegram_users (chat_id)
  where status = 'active' and chat_id is not null;

create index if not exists telegram_users_funnel_stage_idx
  on app.telegram_users (funnel_stage, stage_updated_at);

create index if not exists telegram_users_analysis_ai_request_idx
  on app.telegram_users (analysis_ai_request_id);

create index if not exists messages_user_created_idx
  on app.messages (telegram_user_id, created_at, id);

create unique index if not exists messages_ai_request_unique_idx
  on app.messages (ai_request_id)
  where ai_request_id is not null;

create index if not exists ai_requests_source_message_idx
  on app.ai_requests (source_message_id);

create index if not exists ai_requests_user_purpose_created_idx
  on app.ai_requests (telegram_user_id, purpose, created_at);

create index if not exists alerts_severity_created_idx
  on app.alerts (severity, created_at);

alter table app.telegram_users enable row level security;
alter table app.admin_users enable row level security;
alter table app.messages enable row level security;
alter table app.ai_requests enable row level security;
alter table app.alerts enable row level security;

commit;
