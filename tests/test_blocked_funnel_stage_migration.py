from pathlib import Path

MIGRATION = Path("migrations/supabase/202607200002_blocked_funnel_stage.sql")


def test_blocked_funnel_stage_migration_extends_constraint_and_backfills() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "'started', 'dialogue', 'lead', 'blocked'" in sql
    assert "set funnel_stage = 'blocked'" in sql
    assert "where status = 'blocked'" in sql
    assert "stage_updated_at = updated_at" in sql
