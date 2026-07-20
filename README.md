# Siemensbot

Inbound Telegram funnel with:

- admin Telegram bot;
- user-facing Telegram bot;
- context-aware ping worker;
- Google Sheets lead synchronization worker;
- FastAPI health API;
- Supabase Postgres schema in private `app` schema;
- OpenRouter AI chat, Russian voice-message transcription, and structured dialogue analysis.

## Local Setup

```bash
cp .env.example .env
# fill DATABASE_URL, bot tokens, OpenRouter key, admin usernames
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

Для локальной оценки sales-диалогов через DeepEval, двух HTML-отчётов и цикла
экспертной разметки см. [evals/README.md](evals/README.md).

## Prompt versions

Промпты хранятся как неизменяемые версии в `prompts/<тип>/<версия>.md`. Активные
версии выбираются в `prompts/active.json`; чтобы изменить промпт, создайте новый файл
с новым идентификатором версии и переключите конфиг. Уже протестированные и активные
файлы не редактируются.

Apply the SQL migration:

```bash
scripts/apply_migrations.sh
```

Run locally:

```bash
uvicorn app.main:app --reload
python -m app.bots.admin_bot
python -m app.bots.user_bot
python -m app.workers.ping_worker
python -m app.workers.google_sheets_worker
```

Or with Docker:

```bash
docker compose up --build
```

## Production Monitoring

Production includes component heartbeats, split liveness/readiness endpoints, a host-level resource
monitor, and an external Supabase watchdog. Technical alerts are sent by the admin bot only to the
active user configured by `TECH_ADMIN_USERNAME` (currently `@ilya_kkk`).

See [ops/MONITORING.md](ops/MONITORING.md) for migration, Supabase Cron, systemd, alert testing,
triage, and maintenance instructions.

## Notes

- Users enter the funnel themselves through the user-facing bot.
- Telegram voice messages are transcribed in memory as Russian OGG audio and enter the same dialogue flow as text. The transcription itself is not echoed to the user.
- `telegram_users` owns funnel stage, structured lead analysis, and aggregate click data.
- `config` stores the three ping delays managed from the admin bot.
- A technical or business admin can set one shared, one-shot alert for a future number of new
  `/start` users. Both admins receive durable Telegram notifications when it is installed and fired.
- `messages` stores the complete per-user timeline; `ai_requests` links each model call to its source and output messages.
- Pings are generated from the full dialogue context after 2, 24, and 72 hours of inactivity by default.
- Funnel stages progress through `started`, `dialogue`, and `lead`; only a tracked button click creates a lead. A Telegram 403 marks the terminal stage as `blocked`, while `pings_sent_count` retains how many pings were delivered before the block was detected.
- The Telegram callback button marks the user as a lead and then analyzes the complete saved dialogue. It does not open an external form.
- Funnel statistics use Moscow-time `/start` cohorts and include the latest 14 calendar days.

## Google Sheets lead sync

The dedicated worker checks every 10 minutes for rows in `app.telegram_users` where
`funnel_stage = 'lead'` and `google_sheet_synced_at is null`. When new leads exist, it rebuilds
the Google workbook from the same data and grouping used by the admin bot's Excel export:
the same business columns, one `Бот DD.MM.YYYY` worksheet per Moscow calendar day, and newest
leads first. Rebuilding the affected workbook before recording synchronization timestamps
makes retries idempotent without adding service columns to the business-facing sheets.

Put the service-account JSON outside Git in:

```text
secrets/google-service-account.json
```

Keep these settings in `.env`:

```dotenv
GOOGLE_SHEET=Exact spreadsheet title
GOOGLE_SERVICE_ACCOUNT_FILE=secrets/google-service-account.json
GOOGLE_SHEETS_SYNC_INTERVAL_SECONDS=600
GOOGLE_SHEETS_SYNC_BATCH_SIZE=500
```

`GOOGLE_SHEET` may also contain the full Google Sheets URL. Share the spreadsheet with the
JSON file's `client_email` as an Editor. Opening by title requires both the Google Sheets API
and Google Drive API to be enabled for the service-account project.
