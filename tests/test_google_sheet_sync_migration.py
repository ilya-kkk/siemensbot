from pathlib import Path


def test_google_sheet_sync_migration_adds_timestamp_and_partial_index() -> None:
    sql = (
        Path("migrations/supabase/202607200001_google_sheet_sync.sql")
        .read_text(encoding="utf-8")
        .lower()
    )

    assert "add column if not exists google_sheet_synced_at timestamptz" in sql
    assert "telegram_users_unsynced_google_sheet_leads_idx" in sql
    assert "funnel_stage = 'lead' and google_sheet_synced_at is null" in sql
