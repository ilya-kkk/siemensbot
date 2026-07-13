begin;

create table if not exists app.service_heartbeats (
  component text primary key,
  instance_id text,
  status text not null default 'ok',
  details jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now(),
  constraint service_heartbeats_status_check check (status in ('ok', 'degraded', 'stopping'))
);

create table if not exists app.monitor_incidents (
  incident_key text primary key,
  is_open boolean not null default false,
  opened_at timestamptz,
  last_notified_at timestamptz,
  resolved_at timestamptz,
  details jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

alter table app.service_heartbeats enable row level security;
alter table app.monitor_incidents enable row level security;

commit;
