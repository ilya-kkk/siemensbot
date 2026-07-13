begin;

drop table if exists app.campaign_recipients;
drop table if exists app.campaigns;

update app.telegram_users
set status = 'active', updated_at = now()
where status = 'unresolved';

drop index if exists app.telegram_users_unresolved_username_unique;

alter table app.telegram_users
  drop constraint if exists telegram_users_source_check,
  drop constraint if exists telegram_users_status_check,
  drop column if exists source,
  drop column if exists imported_at,
  drop column if exists first_followup_sent_at,
  add constraint telegram_users_status_check check (status in ('active', 'blocked', 'invalid'));

commit;
