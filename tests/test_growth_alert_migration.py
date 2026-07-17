from pathlib import Path

MIGRATION = Path("migrations/supabase/202607170002_user_growth_alerts.sql")


def test_growth_alert_migration_contains_state_delivery_and_concurrency_guards() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "create table if not exists app.user_growth_alerts" in sql
    assert "create table if not exists app.admin_notification_deliveries" in sql
    assert "where status = 'active'" in sql
    assert "unique (\n    growth_alert_id, event, admin_user_id" in sql
    assert "telegram_users_started_at_idx" in sql
    assert sql.count("enable row level security") == 2
