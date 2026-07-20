begin;

alter table app.telegram_users
  drop constraint if exists telegram_users_funnel_stage_check,
  add constraint telegram_users_funnel_stage_check
    check (funnel_stage in ('started', 'dialogue', 'lead', 'blocked'));

-- Preserve the number of successfully delivered pings in pings_sent_count and
-- expose users that had already blocked the bot before this migration.
update app.telegram_users
set funnel_stage = 'blocked',
    stage_updated_at = updated_at
where status = 'blocked'
  and funnel_stage <> 'blocked';

commit;
