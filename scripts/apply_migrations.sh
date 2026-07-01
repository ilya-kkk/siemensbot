#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is required" >&2
  exit 1
fi

PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-10}" \
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/supabase/202607010001_initial_schema.sql
