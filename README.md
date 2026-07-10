# Siemensbot

Telegram follow-up microservice with:

- admin Telegram bot;
- user-facing Telegram bot;
- FastAPI redirect/health API;
- campaign worker;
- Supabase Postgres schema in private `app` schema;
- OpenRouter AI chat and structured dialogue analysis.

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
python -m app.workers.campaign_worker
```

Or with Docker:

```bash
docker compose up --build
```

## Notes

- Bot API cannot send private messages by username alone. Import needs `chat_id`.
- Username-only imports are stored as `unresolved` and excluded from campaigns.
- URL button clicks are tracked through `/r/{token}` redirect links.
# siemensbot
