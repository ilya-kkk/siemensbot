import json
from pathlib import Path

from ops.host_monitor import HostMonitor, cpu_percent, memory_available_percent


def test_cpu_percent_uses_counter_delta() -> None:
    assert cpu_percent([100, 20], (200, 40)) == 80.0
    assert cpu_percent(None, (200, 40)) is None


def test_memory_available_percent(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 1000 kB\nMemAvailable: 125 kB\n", encoding="utf-8")
    assert memory_available_percent(meminfo) == 12.5


def test_incident_is_deduplicated_and_recovers(tmp_path: Path) -> None:
    monitor = HostMonitor(tmp_path / "state.json", tmp_path / "chat", "token")
    notifications: list[tuple[str, str, str]] = []
    monitor.notify = lambda severity, category, message: bool(
        notifications.append((severity, category, message)) or True
    )

    monitor.set_incident("database", True, "down", 100)
    monitor.set_incident("database", True, "still down", 200)
    monitor.set_incident("database", True, "hourly", 3800)
    monitor.set_incident("database", False, "up", 3900)

    assert [item[0] for item in notifications] == ["CRITICAL", "CRITICAL", "RECOVERED"]


def test_monitor_state_is_persisted(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    monitor = HostMonitor(state_path, tmp_path / "chat", None)
    monitor.state["marker"] = 42
    monitor.save()

    assert json.loads(state_path.read_text())["marker"] == 42
    assert HostMonitor(state_path, tmp_path / "chat", None).state["marker"] == 42
