from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

settings = get_settings()

engine: AsyncEngine = create_async_engine(
    settings.async_database_url,
    pool_pre_ping=True,
    connect_args={"statement_cache_size": 0},
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def check_database() -> dict[str, str | bool]:
    async with SessionLocal() as session:
        result = await session.execute(
            text(
                """
                select
                  current_database() as database,
                  current_schema() as schema,
                  to_regclass('app.telegram_users') is not null as app_schema_ready
                """
            )
        )
        row = result.one()
        return {
            "database": row.database,
            "schema": row.schema,
            "app_schema_ready": bool(row.app_schema_ready),
        }
