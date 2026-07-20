import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.alerts import send_critical_alert
from app.core.config import get_settings
from app.core.db import SessionLocal, check_database
from app.core.logging import configure_logging
from app.monitoring import heartbeat_loop, stop_background_task
from app.repositories import AppRepository

settings = get_settings()
configure_logging(settings)
logger = logging.getLogger(__name__)

COMPONENTS = ("api", "user_bot", "admin_bot", "ping_worker", "google_sheets_worker")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(heartbeat_loop("api", settings))
    try:
        yield
    finally:
        await stop_background_task(task)


app = FastAPI(title="Siemensbot API", lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled_exception(_request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled API error: %s", exc)
    await send_critical_alert(None, settings, "api", str(exc), {})
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


async def _database_ready() -> bool:
    try:
        async with asyncio.timeout(3):
            result = await check_database()
        return bool(result["app_schema_ready"])
    except Exception:
        return False


@app.get("/health/live")
async def health_live() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/health/ready")
async def health_ready() -> JSONResponse:
    ready = await _database_ready()
    return JSONResponse(
        {"status": "ok" if ready else "degraded", "checks": {"database": ready}},
        status_code=200 if ready else 503,
    )


@app.get("/health")
async def health() -> JSONResponse:
    database_ready = await _database_ready()
    component_health = {
        component: {"status": "unknown", "updated_at": None} for component in COMPONENTS
    }
    if database_ready:
        try:
            async with asyncio.timeout(3):
                async with SessionLocal() as session:
                    component_health = await AppRepository(session).get_service_health(
                        COMPONENTS,
                        settings.heartbeat_stale_seconds,
                    )
        except Exception:
            database_ready = False
    is_ready = database_ready and all(
        value["status"] == "ok" for value in component_health.values()
    )
    return JSONResponse(
        {
            "status": "ok" if is_ready else "degraded",
            "checks": {"database": database_ready, "components": component_health},
        },
        status_code=200 if is_ready else 503,
    )


@app.get("/health/watchdog")
async def health_watchdog() -> JSONResponse:
    healthy = False
    if await _database_ready():
        try:
            async with asyncio.timeout(3):
                async with SessionLocal() as session:
                    result = await AppRepository(session).get_service_health(
                        ("supabase_watchdog",),
                        180,
                    )
            healthy = result["supabase_watchdog"]["status"] == "ok"
        except Exception:
            healthy = False
    return JSONResponse(
        {"status": "ok" if healthy else "degraded"},
        status_code=200 if healthy else 503,
    )
