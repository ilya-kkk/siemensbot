# Siemensbot

Inbound Telegram funnel with:

- admin Telegram bot;
- user-facing Telegram bot;
- context-aware ping worker;
- FastAPI redirect/health API;
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

## Notes

- Users enter the funnel themselves through the user-facing bot.
- Telegram voice messages are transcribed in memory as Russian OGG audio and enter the same dialogue flow as text. The transcription itself is not echoed to the user.
- `telegram_users` owns funnel stage, lead analysis, offer token, and aggregate click data.
- `config` stores the destination URL and the three ping delays. `TEST_DRIVE_URL` seeds an empty singleton once; after that the database is the source of truth and admins update both values from the admin bot.
- `messages` stores the complete per-user timeline; `ai_requests` links each model call to its source and output messages.
- Pings are generated from the full dialogue context after 2, 24, and 72 hours of inactivity by default.
- Funnel stages progress through `started`, `dialogue`, and `lead`; only a tracked button click creates a lead.
- URL button clicks are aggregated on the user through `/r/{token}` redirects. Set `APP_ENV=production` and an externally reachable `PUBLIC_BASE_URL` in production; startup fails without it so clicks cannot silently bypass tracking.
- Funnel statistics use Moscow-time `/start` cohorts and include the latest 14 calendar days.
