from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import check_database, get_session
from app.repositories import AppRepository

app = FastAPI(title="Siemensbot API")


@app.get("/health")
async def health() -> JSONResponse:
    db = await check_database()
    is_ready = bool(db["app_schema_ready"])
    return JSONResponse(
        {"status": "ok" if is_ready else "degraded", "db": db},
        status_code=200 if is_ready else 503,
    )


@app.get("/r/{token}")
async def redirect_link(
    token: str,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    repo = AppRepository(session)
    settings = get_settings()
    await repo.record_offer_click(token)
    config = await repo.get_app_config(settings.test_drive_url)
    destination = str(config["offer_url"] or settings.test_drive_url)
    return RedirectResponse(destination, status_code=302)
