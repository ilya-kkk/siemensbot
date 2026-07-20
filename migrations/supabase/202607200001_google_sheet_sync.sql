begin;

alter table app.telegram_users
  add column if not exists google_sheet_synced_at timestamptz;

create index if not exists telegram_users_unsynced_google_sheet_leads_idx
  on app.telegram_users (lead_at, id)
  where funnel_stage = 'lead' and google_sheet_synced_at is null;

comment on column app.telegram_users.google_sheet_synced_at is
  'When this lead was confirmed present in the configured Google Sheet.';

commit;
