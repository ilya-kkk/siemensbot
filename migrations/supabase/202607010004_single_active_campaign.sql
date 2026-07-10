begin;

with latest_active as (
  select id
  from app.campaigns
  where status in ('running', 'paused')
  order by id desc
  limit 1
)
update app.campaigns
set status = 'canceled',
    updated_at = now()
where status in ('running', 'paused')
  and id not in (select id from latest_active);

create unique index if not exists campaigns_single_active_idx
  on app.campaigns ((true))
  where status in ('running', 'paused');

commit;
