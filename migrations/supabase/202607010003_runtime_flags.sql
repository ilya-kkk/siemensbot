begin;

create table if not exists app.runtime_flags (
  key text primary key,
  bool_value boolean not null,
  updated_at timestamptz not null default now()
);

insert into app.runtime_flags (key, bool_value)
values ('client_bot_stopped', false)
on conflict (key) do nothing;

alter table app.runtime_flags enable row level security;

commit;
