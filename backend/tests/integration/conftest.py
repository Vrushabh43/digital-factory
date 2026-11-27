from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from dmf.persistence.db import make_engine, run_migrations
from dmf.settings import REPO_ROOT

TEST_DB = os.environ.get("TEST_DATABASE_URL")
_migrated = False


@pytest.fixture
async def db():
    """Fresh, migrated PostgreSQL database (tables truncated before each test)."""
    global _migrated
    if not TEST_DB:
        pytest.skip("TEST_DATABASE_URL not set (PostgreSQL integration tests)")
    if not _migrated:
        await run_migrations(TEST_DB, REPO_ROOT / "database" / "alembic.ini")
        _migrated = True
    engine = make_engine(TEST_DB)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE events, cell_passes, pieces, cell_status_history, tracker_snapshot, writer_progress "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield engine
    await engine.dispose()
