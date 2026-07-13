#!/usr/bin/env python3
"""Small host-level monitor for Siemensbot; uses only the Python standard library."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SERVICES = (
    "siemensbot_api",
    "siemensbot_admin_bot",
    "siemensbot_user_bot",
    "siemensbot_ping_worker",
)


def utc_now() -> float:
    return datetime.now(UTC).timestamp()


def read_chat_id(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def cpu_sample(path: Path = Path("/proc/stat")) -> tuple[int, int]:
    values = [int(value) for value in path.read_text().splitlines()[0].split()[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def cpu_percent(previous: list[int] | None, current: tuple[int, int]) -> float | None:
    if previous is None:
        return None
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (total_delta - idle_delta) / total_delta))


def memory_available_percent(path: Path = Path("/proc/meminfo")) -> float:
    values: dict[str, int] = {}
    for line in path.read_text().splitlines():
        key, raw = line.split(":", 1)
        values[key] = int(raw.strip().split()[0])
    return 100.0 * values["MemAvailable"] / values["MemTotal"]


def docker_states() -> dict[str, dict[str, Any]]:
    command = ["docker", "inspect", *SERVICES]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)
        rows = json.loads(result.stdout)
    except Exception:
        return {}
    return {
        str(row.get("Name", "")).lstrip("/"): {
            "id": row.get("Id"),
            "running": bool(row.get("State", {}).get("Running")),
            "restarts": int(row.get("RestartCount", 0)),
        }
        for row in rows
    }


class HostMonitor:
    def __init__(self, state_path: Path, cache_path: Path, token: str | None) -> None:
        self.state_path = state_path
        self.cache_path = cache_path
        self.token = token
        self.state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"incidents": {}, "counters": {}, "docker": {}}

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.state_path)

    def notify(self, severity: str, category: str, message: str) -> bool:
        chat_id = read_chat_id(self.cache_path)
        if not self.token or chat_id is None:
            return False
        text = (
            f"{'✅' if severity == 'RECOVERED' else '🚨'} {severity} · production\n"
            f"{category}\n{message}\n"
            f"{datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=data,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return 200 <= response.status < 300
        except Exception:
            return False

    def set_incident(self, key: str, active: bool, message: str, now: float) -> None:
        incidents = self.state.setdefault("incidents", {})
        incident = incidents.setdefault(key, {"open": False, "last_notified": 0.0})
        if active and not incident["open"]:
            delivered = self.notify("CRITICAL", key, message)
            incident.update(open=True, opened_at=now, last_notified=now if delivered else 0.0)
        elif active and now - float(incident.get("last_notified", 0)) >= 3600:
            if self.notify("CRITICAL", key, f"Still failing: {message}"):
                incident["last_notified"] = now
        elif not active and incident["open"]:
            if self.notify("RECOVERED", key, message):
                incident.update(open=False, resolved_at=now)

    def consecutive(self, key: str, condition: bool) -> int:
        counters = self.state.setdefault("counters", {})
        counters[key] = int(counters.get(key, 0)) + 1 if condition else 0
        return int(counters[key])

    def check_http(self, now: float) -> None:
        ready_url = os.getenv("SIEMENSBOT_READY_URL", "http://127.0.0.1:8001/health/ready")
        ready = http_ok(ready_url)
        ready_failures = self.consecutive("api_database_failures", not ready)
        self.set_incident(
            "api_database",
            ready_failures >= 2,
            "API and database readiness check failed"
            if not ready
            else "API and database recovered",
            now,
        )
        if os.getenv("SUPABASE_WATCHDOG_ENABLED", "false").strip().lower() not in {
            "1",
            "true",
            "yes",
        }:
            return
        watchdog_url = os.getenv("SIEMENSBOT_WATCHDOG_URL", "http://127.0.0.1:8001/health/watchdog")
        watchdog = http_ok(watchdog_url)
        watchdog_failures = self.consecutive("watchdog_failures", not watchdog)
        self.set_incident(
            "supabase_watchdog",
            watchdog_failures >= 3,
            "Supabase watchdog heartbeat is stale" if not watchdog else "Watchdog recovered",
            now,
        )

    def check_resources(self, now: float) -> None:
        current_cpu = cpu_sample()
        previous_cpu = self.state.get("cpu_sample")
        usage = cpu_percent(previous_cpu, current_cpu)
        self.state["cpu_sample"] = list(current_cpu)
        if usage is not None:
            high = self.consecutive("cpu_high", usage > 90)
            low = self.consecutive("cpu_recovered", usage < 80)
            open_cpu = self.state.get("incidents", {}).get("cpu", {}).get("open", False)
            self.set_incident(
                "cpu",
                high >= 10 if not open_cpu else low < 5,
                f"CPU usage is {usage:.1f}%" if high else f"CPU recovered to {usage:.1f}%",
                now,
            )

        available = memory_available_percent()
        low_memory = self.consecutive("memory_low", available < 10)
        memory_ok = self.consecutive("memory_recovered", available > 15)
        open_memory = self.state.get("incidents", {}).get("memory", {}).get("open", False)
        self.set_incident(
            "memory",
            low_memory >= 5 if not open_memory else memory_ok < 5,
            f"Available RAM is {available:.1f}%",
            now,
        )

        disk = shutil.disk_usage("/")
        disk_used = 100.0 * disk.used / disk.total
        open_disk = self.state.get("incidents", {}).get("disk", {}).get("open", False)
        disk_active = disk_used > 85 if not open_disk else disk_used >= 80
        level = "critical" if disk_used >= 95 else "warning"
        self.set_incident("disk", disk_active, f"Root disk is {disk_used:.1f}% used ({level})", now)

    def check_crash_loops(self, now: float) -> None:
        states = docker_states()
        saved = self.state.setdefault("docker", {})
        all_events = self.state.setdefault("restart_events", {})
        if not isinstance(all_events, dict):
            all_events = {}
            self.state["restart_events"] = all_events
        for service, current in states.items():
            crash_events = all_events.setdefault(service, [])
            crash_events[:] = [value for value in crash_events if now - float(value) <= 600]
            previous = saved.get(service)
            if previous and previous.get("id") == current.get("id"):
                increase = max(0, current["restarts"] - int(previous.get("restarts", 0)))
                crash_events.extend([now] * increase)
            saved[service] = current
            self.set_incident(
                f"container_crash_loop:{service}",
                len(crash_events) >= 3,
                f"{service} restarted {len(crash_events)} times in 10 minutes",
                now,
            )

    def run_once(self) -> None:
        now = utc_now()
        self.check_http(now)
        self.check_resources(now)
        self.check_crash_loops(now)
        self.save()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    root = Path(os.getenv("SIEMENSBOT_ROOT", Path(__file__).resolve().parents[1]))
    monitor = HostMonitor(
        Path(os.getenv("SIEMENSBOT_MONITOR_STATE", "/var/lib/siemensbot-monitor/state.json")),
        Path(os.getenv("TECH_ADMIN_CHAT_CACHE_PATH", root / "runtime/tech_admin_chat_id")),
        os.getenv("ADMIN_BOT_TOKEN"),
    )
    while True:
        monitor.run_once()
        if args.once:
            return
        time.sleep(60)


if __name__ == "__main__":
    main()
