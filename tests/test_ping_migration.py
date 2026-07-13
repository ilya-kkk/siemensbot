from pathlib import Path

MIGRATION = Path("migrations/supabase/202607130003_config_pings.sql")


def test_ping_migration_uses_single_config_and_user_columns() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "create table if not exists app.config" in sql
    assert "ping_1_delay_minutes integer not null default 120" in sql
    assert "ping_2_delay_minutes integer not null default 1440" in sql
    assert "ping_3_delay_minutes integer not null default 4320" in sql
    assert "constraint config_singleton_check check (id = 1)" in sql
    assert "config_ping_delays_check" in sql
    assert "ping_1_delay_minutes > 0" in sql
    assert "ping_1_delay_minutes < ping_2_delay_minutes" in sql
    assert "ping_2_delay_minutes < ping_3_delay_minutes" in sql
    assert "pings_sent_count smallint" in sql
    assert "pings_sent_count between 0 and 3" in sql
    assert "ping_3_answered_at timestamptz" in sql
    assert "references app.ai_requests(id)" in sql
    assert "create table" not in sql.replace("create table if not exists app.config", "")


def test_ping_migration_backfills_click_leads_and_ai_purpose() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "purpose in ('chat', 'analysis', 'ping')" in sql
    assert "coalesce(offer_click_count, 0) > 0 then 'lead'" in sql
    assert "then coalesce(" in sql
    assert "offer_first_clicked_at" in sql
    assert "set ping_anchor_at = now()" in sql


def test_inbound_migration_preserves_all_historical_offer_tokens() -> None:
    sql = Path("migrations/supabase/202607130002_user_message_ai_model.sql").read_text(
        encoding="utf-8"
    ).lower()

    assert "offer_legacy_tokens text[]" in sql
    assert "array_agg(token order by created_at desc, id desc) as tokens" in sql
    assert "using gin (offer_legacy_tokens)" in sql
