begin;

alter table if exists app.config
  drop column if exists offer_url;

alter table if exists app.telegram_users
  drop column if exists offer_token,
  drop column if exists offer_legacy_tokens;

commit;
