from pathlib import Path

MIGRATION = Path("migrations/supabase/202607190001_referral_sources.sql")


def test_referral_sources_migration_adds_source_table_and_user_attribution() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "create table if not exists app.referral_sources" in sql
    assert "source_code text unique" in sql
    assert "title text not null" in sql
    assert "add column if not exists referral_source_id bigint" in sql
    assert "telegram_users_referral_source_id_fkey" in sql
    assert "references app.referral_sources(id)" in sql
    assert "telegram_users_referral_source_idx" in sql
    assert sql.count("enable row level security") == 1
