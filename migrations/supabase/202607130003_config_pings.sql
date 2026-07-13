begin;

create table if not exists app.config (
  id smallint primary key default 1,
  offer_url text,
  ping_1_delay_minutes integer not null default 120,
  ping_2_delay_minutes integer not null default 1440,
  ping_3_delay_minutes integer not null default 4320,
  updated_at timestamptz not null default now(),
  constraint config_singleton_check check (id = 1),
  constraint config_ping_delays_check check (
    ping_1_delay_minutes > 0
    and ping_1_delay_minutes < ping_2_delay_minutes
    and ping_2_delay_minutes < ping_3_delay_minutes
  )
);

insert into app.config (id)
values (1)
on conflict (id) do nothing;

alter table app.config enable row level security;

alter table app.telegram_users
  add column if not exists pings_sent_count smallint,
  add column if not exists ping_anchor_at timestamptz,
  add column if not exists ping_1_sent_at timestamptz,
  add column if not exists ping_1_answered_at timestamptz,
  add column if not exists ping_2_sent_at timestamptz,
  add column if not exists ping_2_answered_at timestamptz,
  add column if not exists ping_3_sent_at timestamptz,
  add column if not exists ping_3_answered_at timestamptz,
  add column if not exists ping_claim_token uuid,
  add column if not exists ping_claim_number smallint,
  add column if not exists ping_claimed_at timestamptz,
  add column if not exists ping_retry_at timestamptz,
  add column if not exists ping_pending_ai_request_id bigint;

update app.telegram_users
set pings_sent_count = coalesce(pings_sent_count, 0);

alter table app.telegram_users
  alter column pings_sent_count set default 0,
  alter column pings_sent_count set not null,
  drop constraint if exists telegram_users_pings_sent_count_check,
  drop constraint if exists telegram_users_ping_claim_number_check,
  add constraint telegram_users_pings_sent_count_check
    check (pings_sent_count between 0 and 3),
  add constraint telegram_users_ping_claim_number_check
    check (ping_claim_number is null or ping_claim_number between 1 and 3);

alter table app.telegram_users
  drop constraint if exists telegram_users_ping_pending_ai_request_id_fkey,
  add constraint telegram_users_ping_pending_ai_request_id_fkey
    foreign key (ping_pending_ai_request_id)
    references app.ai_requests(id)
    on delete set null;

alter table app.ai_requests
  drop constraint if exists ai_requests_purpose_check,
  add constraint ai_requests_purpose_check
    check (purpose in ('chat', 'analysis', 'ping'));

-- Earlier migrations treated an offer display as a lead. A lead is now created
-- only by a tracked click; users who merely saw the offer return to their
-- furthest real dialogue stage.
update app.telegram_users
set funnel_stage = case
      when coalesce(offer_click_count, 0) > 0 then 'lead'
      when dialogue_started_at is not null then 'dialogue'
      else 'started'
    end,
    offer_first_clicked_at = case
      when coalesce(offer_click_count, 0) > 0 then coalesce(
        offer_first_clicked_at,
        offer_last_clicked_at,
        lead_at,
        stage_updated_at,
        updated_at,
        now()
      )
      else offer_first_clicked_at
    end,
    lead_at = case
      when coalesce(offer_click_count, 0) > 0 then coalesce(
        offer_first_clicked_at,
        offer_last_clicked_at,
        lead_at,
        stage_updated_at,
        updated_at,
        now()
      )
      else null
    end,
    stage_updated_at = case
      when coalesce(offer_click_count, 0) > 0
        then coalesce(offer_first_clicked_at, stage_updated_at)
      when dialogue_started_at is not null
        then dialogue_started_at
      else coalesce(started_at, first_seen_at, stage_updated_at)
    end;

-- Existing users must start a fresh schedule instead of receiving several
-- overdue pings immediately after deployment.
update app.telegram_users
set ping_anchor_at = now()
where ping_anchor_at is null
  and status = 'active'
  and funnel_stage <> 'lead';

create unique index if not exists telegram_users_ping_claim_token_key
  on app.telegram_users (ping_claim_token)
  where ping_claim_token is not null;

create index if not exists telegram_users_ping_due_idx
  on app.telegram_users (pings_sent_count, ping_anchor_at, ping_retry_at)
  where status = 'active'
    and funnel_stage <> 'lead'
    and pings_sent_count < 3;

create index if not exists telegram_users_ping_pending_ai_request_idx
  on app.telegram_users (ping_pending_ai_request_id)
  where ping_pending_ai_request_id is not null;

commit;
