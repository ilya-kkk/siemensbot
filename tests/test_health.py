import pytest

from app import main


@pytest.mark.asyncio
async def test_live_health_is_dependency_independent() -> None:
    response = await main.health_live()
    assert response.status_code == 200
    assert response.body == b'{"status":"ok"}'


@pytest.mark.asyncio
async def test_ready_health_is_degraded_when_database_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable() -> bool:
        return False

    monkeypatch.setattr(main, "_database_ready", unavailable)
    response = await main.health_ready()

    assert response.status_code == 503
    assert b'"database":false' in response.body


@pytest.mark.asyncio
async def test_aggregate_health_rejects_stale_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def available() -> bool:
        return True

    class _SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    class _Repo:
        def __init__(self, _session) -> None:
            pass

        async def get_service_health(self, components, _stale_seconds):
            return {
                component: {
                    "status": "stale" if component == "ping_worker" else "ok",
                    "updated_at": None,
                }
                for component in components
            }

    monkeypatch.setattr(main, "_database_ready", available)
    monkeypatch.setattr(main, "SessionLocal", _SessionContext)
    monkeypatch.setattr(main, "AppRepository", _Repo)

    response = await main.health()
    assert response.status_code == 503
    assert b'"ping_worker":{"status":"stale"' in response.body
