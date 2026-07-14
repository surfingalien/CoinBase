from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


DEFAULT_SQLITE_URL = "sqlite+aiosqlite:///./trading.db"


def _async_db_url(url: str) -> str:
    """Normalize a database URL to an async SQLAlchemy driver.

    Railway's Postgres add-on injects DATABASE_URL as `postgresql://...`
    (and legacy `postgres://...`), but SQLAlchemy's async engine needs an
    explicit async driver — `postgresql+asyncpg://...`. SQLite likewise needs
    `sqlite+aiosqlite://`. This lets the DATABASE_URL Railway provides be used
    verbatim: point DATABASE_URL at the Postgres reference and it just works,
    while the local SQLite default is untouched.

    A blank/whitespace value falls back to the local SQLite default rather
    than crashing the whole app at import time — this happens when a
    DATABASE_URL env var is present but set to an empty string (e.g. an
    unresolved `${{Postgres.DATABASE_URL}}` reference on Railway), which
    otherwise overrides the settings default with "".
    """
    url = (url or "").strip()
    if not url:
        return DEFAULT_SQLITE_URL
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


def _add_missing_columns(sync_conn) -> None:
    """Minimal forward-only migration: create_all() never alters existing
    tables, so columns added to the models after a database was first created
    have to be added here or every ORM SELECT against an old database fails.
    Only nullable columns can be handled this way, which is all we need."""
    from sqlalchemy import inspect, text

    added_columns = {
        "orders": {"fees_usd": "FLOAT"},
        "positions": {
            "entry_fees_usd": "FLOAT",
            "strategy": "VARCHAR",
            "managed": "BOOLEAN",
            "day_mark_price": "FLOAT",
            "day_mark_date": "VARCHAR",
        },
    }
    inspector = inspect(sync_conn)
    for table, columns in added_columns.items():
        if table not in inspector.get_table_names():
            continue
        existing = {col["name"] for col in inspector.get_columns(table)}
        for name, sql_type in columns.items():
            if name not in existing:
                sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
