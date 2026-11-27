"""Database engine and migrations."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

log = logging.getLogger(__name__)


def make_engine(url: str, pool_size: int = 5) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_recycle=1800,
        connect_args={"timeout": 5, "command_timeout": 30},
    )


def _upgrade(url: str, ini: Path) -> None:
    cfg = Config(str(ini))
    cfg.attributes["database_url"] = url
    command.upgrade(cfg, "head")


async def run_migrations(url: str, ini: Path) -> None:
    """Apply Alembic migrations (``alembic upgrade head``) in a worker thread."""
    await asyncio.to_thread(_upgrade, url, ini)
    log.info("database schema is up to date")


async def ping(engine: AsyncEngine, timeout: float = 2.0) -> bool:
    try:
        async with asyncio.timeout(timeout):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


def to_dt(ts: float | None) -> datetime | None:
    return datetime.fromtimestamp(ts, UTC) if ts is not None else None
