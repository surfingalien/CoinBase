from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


def _async_db_url(url: str) -> str:
    """Normalize a database URL to an async SQLAlchemy driver.

    Railway's Postgres add-on injects DATABASE_URL as `postgresql://...`
    (and legacy `postgres://...`), but SQLAlchemy's async engine needs an
    explicit async driver — `postgresql+asyncpg://...`. SQLite likewise needs
    `sqlite+aiosqlite://`. This lets the DATABASE_URL Railway provides be used
    verbatim: point DATABASE_URL at the Postgres reference and it just works,
    while the local SQLite default is untouched.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    if url.startswith("sqlite://") and "+aiosqlite" not in url:
        return "sqlite+aiosqlite://" + url[len("sqlite://"):]
    return url


engine = create_async_engine(_async_db_url(settings.database_url), echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
