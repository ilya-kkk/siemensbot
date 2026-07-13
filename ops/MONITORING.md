# Siemensbot production monitoring

The monitoring path deliberately has two independent halves:

- the host service detects database/API/resource failures while the VPS is alive;
- the Supabase watchdog calls the public health endpoint and detects a dead or unreachable VPS.
- the admin bot keeps a pinned tech status message updated once a minute.

Both send through `ADMIN_BOT_TOKEN` to the active `TECH_ADMIN_USERNAME`. The application caches
the resolved chat ID in `runtime/tech_admin_chat_id`, so a database outage does not block host
alerts.

## 1. Prepare the application

Apply migrations and create the runtime directory before starting Compose:

```bash
scripts/apply_migrations.sh
install -d -m 700 runtime
docker compose up -d --build
```

The configured tech admin must have opened the admin bot at least once. On startup the admin bot
loads that existing chat ID into `runtime/tech_admin_chat_id`; each later `/start` refreshes it.
Confirm the file exists without printing its contents:

```bash
test -s runtime/tech_admin_chat_id
```

In the admin bot, run `/test_alert`. Only the tech admin can use this command.

The admin bot also creates and pins a tech status message in the tech admin chat. It updates every
`TECH_STATUS_UPDATE_SECONDS` seconds, default 60, and stores the message id in
`TECH_STATUS_MESSAGE_CACHE_PATH`. If the VPS is down, this message stops changing; treat a timestamp
older than 2-3 minutes as a signal to check the server manually.

## 2. Deploy the external Supabase watchdog

Generate a long random `WATCHDOG_SECRET`, then configure Edge Function secrets. Keep the values out
of shell history where possible:

```bash
supabase secrets set ADMIN_BOT_TOKEN=... \
  TECH_ADMIN_USERNAME=@ilya_kkk \
  PUBLIC_BASE_URL=https://your-public-host \
  WATCHDOG_SECRET=...
supabase functions deploy siemensbot-watchdog --no-verify-jwt
```

`SUPABASE_DB_URL` is supplied by the hosted Edge Function environment. Run
`ops/supabase_watchdog_setup.sql.example` once in Supabase SQL Editor after replacing the project
URL and shared secret placeholders. The shared secret in Vault must equal the Edge Function's
`WATCHDOG_SECRET`.

Verify that the job runs and that its own heartbeat is current:

```bash
curl -fsS https://your-public-host/health
curl -fsS https://your-public-host/health/watchdog
```

Supabase Cron job history is available in `cron.job_run_details`. The function sends one DOWN,
hourly reminders, and one RECOVERED notification.

## 3. Install the host monitor

The unit assumes the repository is deployed at `/opt/siemensbot`. Adjust the unit if the path is
different.

```bash
install -d -m 700 /var/lib/siemensbot-monitor
install -m 0644 ops/siemensbot-monitor.service /etc/systemd/system/
install -m 0600 /dev/null /etc/siemensbot-monitor.env
```

Put only the following values in `/etc/siemensbot-monitor.env`:

```dotenv
ADMIN_BOT_TOKEN=the_existing_admin_bot_token
SIEMENSBOT_ROOT=/opt/siemensbot
TECH_ADMIN_CHAT_CACHE_PATH=/opt/siemensbot/runtime/tech_admin_chat_id
TECH_STATUS_MESSAGE_CACHE_PATH=/opt/siemensbot/runtime/tech_status_message_id
TECH_STATUS_UPDATE_SECONDS=60
# Set to true only after the Supabase Edge Function and Cron job are running.
SUPABASE_WATCHDOG_ENABLED=true
```

Enable and inspect the service:

```bash
systemctl daemon-reload
systemctl enable --now siemensbot-monitor
systemctl status siemensbot-monitor
journalctl -u siemensbot-monitor --since '15 minutes ago'
```

The monitor checks every minute. State and alert cooldowns survive restarts in
`/var/lib/siemensbot-monitor/state.json`.

## Triage and maintenance

Useful first checks:

```bash
docker compose ps
docker compose logs --since 15m api user_bot admin_bot ping_worker
curl -i http://127.0.0.1:8001/health/live
curl -i http://127.0.0.1:8001/health/ready
curl -i http://127.0.0.1:8001/health
```

For planned downtime, stop the host monitor and temporarily unschedule the external job before
stopping the application:

```bash
systemctl stop siemensbot-monitor
```

```sql
select cron.unschedule('siemensbot-watchdog-every-minute');
```

After maintenance, recreate the Cron job with `ops/supabase_watchdog_setup.sql.example`, confirm
`/health` is green, and start the host monitor again:

```bash
systemctl start siemensbot-monitor
```

If the cached recipient is lost, restart `admin_bot` while the database is available or send
`/start` to the admin bot as `@ilya_kkk`.

## Known limitation

If `ADMIN_BOT_TOKEN` itself is revoked, the bot cannot report that failure through Telegram. The
failure remains visible in application, Edge Function, and systemd logs.
