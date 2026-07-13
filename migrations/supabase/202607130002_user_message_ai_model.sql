begin;

alter table app.telegram_users
  add column if not exists funnel_stage text,
  add column if not exists stage_updated_at timestamptz,
  add column if not exists started_at timestamptz,
  add column if not exists dialogue_started_at timestamptz,
  add column if not exists offer_shown_at timestamptz,
  add column if not exists lead_at timestamptz,
  add column if not exists analysis_ai_request_id bigint,
  add column if not exists analysis_output jsonb,
  add column if not exists niche text,
  add column if not exists revenue_estimate text,
  add column if not exists average_check text,
  add column if not exists sales_volume text,
  add column if not exists main_problem text,
  add column if not exists lead_temperature text,
  add column if not exists summary text,
  add column if not exists confidence numeric(5, 4),
  add column if not exists analyzed_at timestamptz,
  add column if not exists offer_token text,
  add column if not exists offer_legacy_tokens text[],
  add column if not exists offer_first_clicked_at timestamptz,
  add column if not exists offer_last_clicked_at timestamptz,
  add column if not exists offer_click_count integer;

alter table app.messages
  add column if not exists ai_request_id bigint;

alter table app.ai_requests
  add column if not exists source_message_id bigint,
  add column if not exists user_snapshot jsonb;

update app.telegram_users u
set started_at = coalesce(
      u.started_at,
      (
        select min(m.created_at)
        from app.messages m
        where m.telegram_user_id = u.id
          and m.direction = 'incoming'
          and coalesce(m.text, '') ~ '^/start(@[^ ]+)?([[:space:]]|$)'
      )
    ),
    dialogue_started_at = coalesce(
      u.dialogue_started_at,
      (
        select min(m.created_at)
        from app.messages m
        where m.telegram_user_id = u.id
          and m.direction = 'incoming'
          and m.message_type = 'text'
          and coalesce(m.text, '') !~ '^/start(@[^ ]+)?([[:space:]]|$)'
      )
    );

do $$
begin
  if to_regclass('app.dialogues') is not null then
    execute $sql$
      update app.telegram_users u
      set offer_shown_at = coalesce(u.offer_shown_at, x.offer_shown_at)
      from (
        select telegram_user_id, min(offer_sent_at) as offer_shown_at
        from app.dialogues
        where offer_sent_at is not null
        group by telegram_user_id
      ) x
      where u.id = x.telegram_user_id
    $sql$;
  end if;
end $$;

do $$
begin
  if to_regclass('app.leads') is not null then
    execute $sql$
      update app.telegram_users u
      set lead_at = coalesce(u.lead_at, x.lead_at)
      from (
        select telegram_user_id, min(created_at) as lead_at
        from app.leads
        group by telegram_user_id
      ) x
      where u.id = x.telegram_user_id
    $sql$;
  end if;
end $$;

do $$
begin
  if to_regclass('app.dialogue_analyses') is not null then
    execute $sql$
      with latest as (
        select distinct on (telegram_user_id)
          telegram_user_id,
          ai_request_id,
          structured_output,
          niche,
          revenue_estimate,
          average_check,
          sales_volume,
          main_problem,
          confidence,
          created_at
        from app.dialogue_analyses
        order by telegram_user_id, created_at desc, id desc
      )
      update app.telegram_users u
      set analysis_ai_request_id = latest.ai_request_id,
          analysis_output = latest.structured_output,
          niche = latest.niche,
          revenue_estimate = latest.revenue_estimate,
          average_check = latest.average_check,
          sales_volume = latest.sales_volume,
          main_problem = latest.main_problem,
          lead_temperature = latest.structured_output ->> 'lead_temperature',
          summary = latest.structured_output ->> 'summary',
          confidence = latest.confidence,
          analyzed_at = latest.created_at
      from latest
      where u.id = latest.telegram_user_id
    $sql$;
  end if;
end $$;

do $$
begin
  if to_regclass('app.tracked_links') is not null then
    execute $sql$
      with token_sets as (
        select
          telegram_user_id,
          (array_agg(token order by created_at desc, id desc))[1] as latest_token,
          array_agg(token order by created_at desc, id desc) as tokens
        from app.tracked_links
        group by telegram_user_id
      )
      update app.telegram_users u
      set offer_token = coalesce(u.offer_token, token_sets.latest_token),
          offer_legacy_tokens = (
            select coalesce(array_agg(distinct value), '{}'::text[])
            from unnest(
              coalesce(u.offer_legacy_tokens, '{}'::text[]) || token_sets.tokens
            ) as aliases(value)
          )
      from token_sets
      where u.id = token_sets.telegram_user_id
    $sql$;
  end if;
end $$;

do $$
begin
  if to_regclass('app.link_clicks') is not null then
    execute $sql$
      update app.telegram_users u
      set offer_first_clicked_at = coalesce(u.offer_first_clicked_at, x.first_clicked_at),
          offer_last_clicked_at = greatest(u.offer_last_clicked_at, x.last_clicked_at),
          offer_click_count = coalesce(u.offer_click_count, 0) + x.click_count
      from (
        select
          telegram_user_id,
          min(clicked_at) as first_clicked_at,
          max(clicked_at) as last_clicked_at,
          count(*)::integer as click_count
        from app.link_clicks
        group by telegram_user_id
      ) x
      where u.id = x.telegram_user_id
    $sql$;
  end if;
end $$;

update app.telegram_users
set offer_shown_at = coalesce(offer_shown_at, lead_at),
    lead_at = coalesce(lead_at, offer_shown_at),
    funnel_stage = case
      when lead_at is not null or offer_shown_at is not null then 'lead'
      when dialogue_started_at is not null then 'dialogue'
      else 'started'
    end,
    stage_updated_at = coalesce(lead_at, offer_shown_at, dialogue_started_at, started_at, first_seen_at),
    analysis_output = coalesce(analysis_output, '{}'::jsonb),
    offer_legacy_tokens = coalesce(offer_legacy_tokens, '{}'::text[]),
    offer_click_count = coalesce(offer_click_count, 0);

update app.ai_requests ar
set source_message_id = (
      select m.id
      from app.messages m
      where m.telegram_user_id = ar.telegram_user_id
        and m.direction = 'incoming'
        and m.created_at <= ar.created_at
      order by m.created_at desc, m.id desc
      limit 1
    ),
    user_snapshot = jsonb_build_object(
      'id', u.id,
      'chat_id', u.chat_id,
      'telegram_user_id', u.telegram_user_id,
      'username', u.username,
      'first_name', u.first_name,
      'last_name', u.last_name,
      'status', u.status,
      'funnel_stage', u.funnel_stage,
      'started_at', u.started_at,
      'dialogue_started_at', u.dialogue_started_at,
      'offer_shown_at', u.offer_shown_at,
      'lead_at', u.lead_at
    )
from app.telegram_users u
where u.id = ar.telegram_user_id
  and (ar.source_message_id is null or ar.user_snapshot is null);

with mapped as (
  select
    ar.id as ai_request_id,
    (
      select m.id
      from app.messages m
      where m.telegram_user_id = ar.telegram_user_id
        and m.direction = 'outgoing'
        and m.created_at >= ar.created_at
      order by m.created_at, m.id
      limit 1
    ) as output_message_id
  from app.ai_requests ar
  where ar.purpose = 'chat'
)
update app.messages m
set ai_request_id = mapped.ai_request_id
from mapped
where m.id = mapped.output_message_id
  and m.ai_request_id is null;

alter table app.telegram_users
  alter column funnel_stage set default 'started',
  alter column funnel_stage set not null,
  alter column stage_updated_at set default now(),
  alter column stage_updated_at set not null,
  alter column analysis_output set default '{}'::jsonb,
  alter column analysis_output set not null,
  alter column offer_legacy_tokens set default '{}'::text[],
  alter column offer_legacy_tokens set not null,
  alter column offer_click_count set default 0,
  alter column offer_click_count set not null;

alter table app.ai_requests
  alter column source_message_id set not null,
  alter column user_snapshot set default '{}'::jsonb,
  alter column user_snapshot set not null,
  alter column telegram_user_id set not null;

alter table app.telegram_users
  drop constraint if exists telegram_users_funnel_stage_check,
  drop constraint if exists telegram_users_lead_temperature_check,
  drop constraint if exists telegram_users_offer_click_count_check,
  add constraint telegram_users_funnel_stage_check
    check (funnel_stage in ('started', 'dialogue', 'lead')),
  add constraint telegram_users_lead_temperature_check
    check (lead_temperature is null or lead_temperature in ('cold', 'warm', 'hot', 'unknown')),
  add constraint telegram_users_offer_click_count_check
    check (offer_click_count >= 0);

alter table app.ai_requests
  drop constraint if exists ai_requests_telegram_user_id_fkey,
  drop constraint if exists ai_requests_source_message_id_fkey,
  add constraint ai_requests_telegram_user_id_fkey
    foreign key (telegram_user_id) references app.telegram_users(id) on delete cascade,
  add constraint ai_requests_source_message_id_fkey
    foreign key (source_message_id) references app.messages(id) on delete cascade;

alter table app.messages
  drop constraint if exists messages_ai_request_id_fkey,
  add constraint messages_ai_request_id_fkey
    foreign key (ai_request_id) references app.ai_requests(id) on delete set null;

alter table app.telegram_users
  drop constraint if exists telegram_users_analysis_ai_request_id_fkey,
  add constraint telegram_users_analysis_ai_request_id_fkey
    foreign key (analysis_ai_request_id) references app.ai_requests(id) on delete set null;

create unique index if not exists telegram_users_offer_token_key
  on app.telegram_users (offer_token);

create index if not exists telegram_users_offer_legacy_tokens_idx
  on app.telegram_users using gin (offer_legacy_tokens);

create index if not exists telegram_users_funnel_stage_idx
  on app.telegram_users (funnel_stage, stage_updated_at);

create index if not exists telegram_users_analysis_ai_request_idx
  on app.telegram_users (analysis_ai_request_id);

create unique index if not exists messages_ai_request_unique_idx
  on app.messages (ai_request_id)
  where ai_request_id is not null;

create index if not exists ai_requests_source_message_idx
  on app.ai_requests (source_message_id);

drop table if exists app.link_clicks;
drop table if exists app.tracked_links;
drop table if exists app.leads;
drop table if exists app.dialogue_analyses;

alter table app.messages
  drop column if exists dialogue_id;

alter table app.ai_requests
  drop column if exists dialogue_id;

drop table if exists app.dialogues;

commit;
