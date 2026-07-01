from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import check_database, get_session
from app.repositories import AppRepository

app = FastAPI(title="Siemensbot API")


@app.get("/health")
async def health() -> dict:
    db = await check_database()
    return {"status": "ok", "db": db}


@app.get("/r/{token}")
async def redirect_link(
    token: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    repo = AppRepository(session)
    destination = await repo.record_link_click(
        token=token,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    if not destination:
        settings = get_settings()
        destination = settings.test_drive_url
    return RedirectResponse(destination, status_code=302)
