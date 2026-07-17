# Siemensbot

Inbound Telegram funnel with:

- admin Telegram bot;
- user-facing Telegram bot;
- context-aware ping worker;
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
- Funnel stages progress through `started`, `dialogue`, and `lead`; only a tracked button click creates a lead.
- The Telegram callback button marks the user as a lead and then analyzes the complete saved dialogue. It does not open an external form.
- Funnel statistics use Moscow-time `/start` cohorts and include the latest 14 calendar days.
