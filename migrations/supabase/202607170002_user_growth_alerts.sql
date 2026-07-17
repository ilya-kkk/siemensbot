begin;

create table if not exists app.user_growth_alerts (
  id bigint generated always as identity primary key,
  threshold bigint not null,
  set_by_admin_user_id bigint references app.admin_users(id) on delete set null,
  set_by_username text not null,
  status text not null default 'active',
  set_at timestamptz not null default clock_timestamp(),
  triggered_at timestamptz,
  replaced_at timestamptz,
  reached_count bigint,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint user_growth_alerts_threshold_check check (threshold > 0),
  constraint user_growth_alerts_reached_count_check check (
    reached_count is null or reached_count >= 0
  ),
  constraint user_growth_alerts_status_check check (
    status in ('active', 'triggered', 'replaced')
  )
);

create unique index if not exists user_growth_alerts_one_active_idx
  on app.user_growth_alerts ((status))
  where status = 'active';

create index if not exists telegram_users_started_at_idx
  on app.telegram_users (started_at)
  where started_at is not null;

create table if not exists app.admin_notification_deliveries (
  id bigint generated always as identity primary key,
  growth_alert_id bigint not null references app.user_growth_alerts(id) on delete cascade,
  event text not null,
  admin_user_id bigint references app.admin_users(id) on delete set null,
  chat_id bigint not null,
  message_text text not null,
  status text not null default 'pending',
  attempt_count integer not null default 0,
  next_attempt_at timestamptz not null default now(),
  claim_token uuid,
  claimed_at timestamptz,
  delivered_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint admin_notification_deliveries_event_check check (
    event in ('installed', 'triggered')
  ),
  constraint admin_notification_deliveries_status_check check (
    status in ('pending', 'sent')
  ),
  constraint admin_notification_deliveries_attempt_count_check check (attempt_count >= 0),
  constraint admin_notification_deliveries_unique_recipient unique (
    growth_alert_id, event, admin_user_id
  )
);

create index if not exists admin_notification_deliveries_pending_idx
  on app.admin_notification_deliveries (next_attempt_at, id)
  where status = 'pending';

alter table app.user_growth_alerts enable row level security;
alter table app.admin_notification_deliveries enable row level security;

commit;
