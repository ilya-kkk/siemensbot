from pathlib import Path


def test_monitoring_migration_contains_required_state_tables() -> None:
    sql = Path("migrations/supabase/202607130004_monitoring.sql").read_text().lower()

    assert "create table if not exists app.service_heartbeats" in sql
    assert "create table if not exists app.monitor_incidents" in sql
    assert "enable row level security" in sql
