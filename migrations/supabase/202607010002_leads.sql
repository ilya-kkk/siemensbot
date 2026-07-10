begin;

create table if not exists app.leads (
  id bigint generated always as identity primary key,
  telegram_user_id bigint not null references app.telegram_users(id) on delete cascade,
  dialogue_id bigint not null references app.dialogues(id) on delete cascade,
  telegram_id bigint,
  chat_id bigint,
  username text,
  first_name text,
  last_name text,
  contact_name text not null,
  niche text,
  revenue_estimate text,
  average_check text,
  sales_volume text,
  main_problem text,
  lead_temperature text,
  summary text,
  confidence numeric(5, 4),
  structured_output jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint leads_dialogue_unique unique (dialogue_id),
  constraint leads_temperature_check check (
    lead_temperature is null or lead_temperature in ('cold', 'warm', 'hot', 'unknown')
  )
);

create index if not exists leads_telegram_user_id_idx
  on app.leads (telegram_user_id);

create index if not exists leads_created_at_idx
  on app.leads (created_at desc, id desc);

alter table app.leads enable row level security;

commit;
