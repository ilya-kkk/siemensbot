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
  source text not null default 'new_start',
  status text not null default 'active',
  imported_at timestamptz not null default now(),
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz,
  first_followup_sent_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint telegram_users_source_check check (source in ('old_import', 'new_start')),
  constraint telegram_users_status_check check (status in ('active', 'blocked', 'invalid', 'unresolved'))
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

create table if not exists app.campaigns (
  id bigint generated always as identity primary key,
  name text not null,
  followup_text text not null,
  batch_size integer not null,
  interval_minutes integer not null,
  status text not null default 'draft',
  created_by_admin_id bigint references app.admin_users(id) on delete set null,
  started_at timestamptz,
  paused_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint campaigns_batch_size_check check (batch_size > 0),
  constraint campaigns_interval_minutes_check check (interval_minutes > 0),
  constraint campaigns_status_check check (status in ('draft', 'running', 'paused', 'completed', 'canceled'))
);

create table if not exists app.campaign_recipients (
  id bigint generated always as identity primary key,
  campaign_id bigint not null references app.campaigns(id) on delete cascade,
  telegram_user_id bigint not null references app.telegram_users(id) on delete cascade,
  batch_number integer not null,
  scheduled_at timestamptz not null,
  status text not null default 'pending',
  attempts integer not null default 0,
  locked_at timestamptz,
  sent_at timestamptz,
  telegram_message_id bigint,
  telegram_error_code integer,
  telegram_error_description text,
  retry_after_seconds integer,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint campaign_recipients_batch_number_check check (batch_number >= 1),
  constraint campaign_recipients_attempts_check check (attempts >= 0),
  constraint campaign_recipients_status_check check (
    status in ('pending', 'processing', 'sent', 'failed', 'skipped', 'rescheduled')
  ),
  constraint campaign_recipients_campaign_user_unique unique (campaign_id, telegram_user_id)
);

create table if not exists app.dialogues (
  id bigint generated always as identity primary key,
  telegram_user_id bigint not null references app.telegram_users(id) on delete cascade,
  status text not null default 'active',
  offer_sent_at timestamptz,
  analysis_requested_at timestamptz,
  analysis_completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint dialogues_status_check check (status in ('active', 'offer_sent', 'analyzed', 'closed'))
);

create table if not exists app.messages (
  id bigint generated always as identity primary key,
  dialogue_id bigint not null references app.dialogues(id) on delete cascade,
  telegram_user_id bigint not null references app.telegram_users(id) on delete cascade,
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
  telegram_user_id bigint references app.telegram_users(id) on delete set null,
  dialogue_id bigint references app.dialogues(id) on delete set null,
  provider text not null default 'openrouter',
  model text not null,
  purpose text not null,
  status text not null default 'success',
  request_payload jsonb not null default '{}'::jsonb,
  response_payload jsonb not null default '{}'::jsonb,
  prompt_tokens integer,
  completion_tokens integer,
  total_tokens integer,
  usage_cost numeric(12, 6),
  error_message text,
  created_at timestamptz not null default now(),
  constraint ai_requests_purpose_check check (purpose in ('chat', 'analysis')),
  constraint ai_requests_status_check check (status in ('success', 'failed'))
);

create table if not exists app.dialogue_analyses (
  id bigint generated always as identity primary key,
  telegram_user_id bigint not null references app.telegram_users(id) on delete cascade,
  dialogue_id bigint not null references app.dialogues(id) on delete cascade,
  ai_request_id bigint references app.ai_requests(id) on delete set null,
  structured_output jsonb not null,
  niche text,
  revenue_estimate text,
  average_check text,
  sales_volume text,
  main_problem text,
  confidence numeric(5, 4),
  created_at timestamptz not null default now(),
  constraint dialogue_analyses_dialogue_unique unique (dialogue_id)
);

create table if not exists app.tracked_links (
  id bigint generated always as identity primary key,
  telegram_user_id bigint not null references app.telegram_users(id) on delete cascade,
  dialogue_id bigint references app.dialogues(id) on delete set null,
  token text not null unique,
  destination_url text not null,
  created_at timestamptz not null default now()
);

create table if not exists app.link_clicks (
  id bigint generated always as identity primary key,
  tracked_link_id bigint not null references app.tracked_links(id) on delete cascade,
  telegram_user_id bigint not null references app.telegram_users(id) on delete cascade,
  ip_address inet,
  user_agent text,
  clicked_at timestamptz not null default now()
);

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

create unique index if not exists telegram_users_unresolved_username_unique
  on app.telegram_users (username_normalized)
  where chat_id is null and username_normalized is not null;

create index if not exists telegram_users_active_chat_idx
  on app.telegram_users (chat_id)
  where status = 'active' and chat_id is not null;

create index if not exists campaign_recipients_campaign_id_idx
  on app.campaign_recipients (campaign_id);

create index if not exists campaign_recipients_telegram_user_id_idx
  on app.campaign_recipients (telegram_user_id);

create index if not exists campaign_recipients_pending_due_idx
  on app.campaign_recipients (scheduled_at, id)
  where status in ('pending', 'rescheduled');

create index if not exists campaign_recipients_status_campaign_idx
  on app.campaign_recipients (status, campaign_id);

create index if not exists dialogues_telegram_user_id_idx
  on app.dialogues (telegram_user_id);

create index if not exists messages_dialogue_created_idx
  on app.messages (dialogue_id, created_at, id);

create index if not exists messages_user_created_idx
  on app.messages (telegram_user_id, created_at, id);

create index if not exists ai_requests_user_purpose_created_idx
  on app.ai_requests (telegram_user_id, purpose, created_at);

create index if not exists dialogue_analyses_user_id_idx
  on app.dialogue_analyses (telegram_user_id);

create index if not exists tracked_links_user_id_idx
  on app.tracked_links (telegram_user_id);

create index if not exists link_clicks_tracked_link_id_idx
  on app.link_clicks (tracked_link_id);

create index if not exists link_clicks_user_clicked_idx
  on app.link_clicks (telegram_user_id, clicked_at);

create index if not exists alerts_severity_created_idx
  on app.alerts (severity, created_at);

alter table app.telegram_users enable row level security;
alter table app.admin_users enable row level security;
alter table app.campaigns enable row level security;
alter table app.campaign_recipients enable row level security;
alter table app.dialogues enable row level security;
alter table app.messages enable row level security;
alter table app.ai_requests enable row level security;
alter table app.dialogue_analyses enable row level security;
alter table app.tracked_links enable row level security;
alter table app.link_clicks enable row level security;
alter table app.alerts enable row level security;

commit;
