# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 G4OCCT Contributors
"""Job queue backed by SQLite.

Jobs move through the following states:
  queued → running → complete | failed

Workers (remote or local) poll ``GET /jobs/next`` to claim a job and then
``POST /jobs/{job_id}/result`` to submit the outcome.
"""

import json
import os
import uuid
from datetime import datetime, timezone

import aiosqlite

DB_PATH = os.environ.get("JOB_DB_PATH", "/tmp/g4occt_jobs.db")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    document_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    element_id  TEXT NOT NULL,
    sim_config  TEXT NOT NULL DEFAULT '{}',
    step_data   TEXT,
    status      TEXT NOT NULL DEFAULT 'queued',
    worker_id   TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    results     TEXT
);
"""

CREATE_WORKERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS workers (
    id           TEXT PRIMARY KEY,
    capabilities TEXT NOT NULL DEFAULT '{}',
    last_seen    TEXT NOT NULL,
    user_id      TEXT
);
"""


class _DB:
    """Async context manager that opens a fresh SQLite connection."""

    def __init__(self) -> None:
        self._conn: aiosqlite.Connection | None = None

    async def __aenter__(self) -> aiosqlite.Connection:
        self._conn = await aiosqlite.connect(DB_PATH)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._conn.execute(CREATE_WORKERS_TABLE_SQL)
        await self._conn.commit()
        return self._conn

    async def __aexit__(self, *exc) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


def get_db() -> "_DB":
    """Return an async context manager that yields a ready SQLite connection."""
    return _DB()


# ---------------------------------------------------------------------------
# Job helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def create_job(
    user_id: str,
    document_id: str,
    workspace_id: str,
    element_id: str,
    sim_config: dict,
    step_data: str | None = None,
) -> dict:
    job_id = str(uuid.uuid4())
    now = _now()
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO jobs
                (id, user_id, document_id, workspace_id, element_id,
                 sim_config, step_data, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                job_id,
                user_id,
                document_id,
                workspace_id,
                element_id,
                json.dumps(sim_config),
                step_data,
                now,
                now,
            ),
        )
        await db.commit()
    return await get_job(job_id)


async def get_job(job_id: str) -> dict | None:
    async with get_db() as db:
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
            if row is None:
                return None
            return dict(row)


async def list_jobs(user_id: str) -> list[dict]:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def claim_next_job(worker_id: str) -> dict | None:
    """Atomically claim the oldest queued job for *worker_id*.

    Uses a single UPDATE … RETURNING statement so that the SELECT and UPDATE
    happen as one indivisible operation, eliminating the TOCTOU race condition
    that exists when the two are separate statements.
    """
    now = _now()
    async with get_db() as db:
        async with db.execute(
            """
            UPDATE jobs
            SET status = 'running', worker_id = ?, updated_at = ?
            WHERE id IN (
                SELECT id
                FROM jobs
                WHERE status = 'queued'
                ORDER BY created_at ASC
                LIMIT 1
            )
            RETURNING *
            """,
            (worker_id, now),
        ) as cur:
            row = await cur.fetchone()
        await db.commit()
    if row is None:
        return None
    return dict(row)


async def complete_job(job_id: str, results: dict) -> dict | None:
    now = _now()
    async with get_db() as db:
        await db.execute(
            "UPDATE jobs SET status='complete', results=?, updated_at=? WHERE id=?",
            (json.dumps(results), now, job_id),
        )
        await db.commit()
    return await get_job(job_id)


async def fail_job(job_id: str, error: str) -> dict | None:
    now = _now()
    async with get_db() as db:
        await db.execute(
            "UPDATE jobs SET status='failed', results=?, updated_at=? WHERE id=?",
            (json.dumps({"error": error}), now, job_id),
        )
        await db.commit()
    return await get_job(job_id)


# ---------------------------------------------------------------------------
# Worker registration helpers
# ---------------------------------------------------------------------------

async def register_worker(worker_id: str, capabilities: dict, user_id: str | None = None) -> None:
    now = _now()
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO workers (id, capabilities, last_seen, user_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                capabilities = excluded.capabilities,
                last_seen = excluded.last_seen,
                user_id = excluded.user_id
            """,
            (worker_id, json.dumps(capabilities), now, user_id),
        )
        await db.commit()


async def list_workers() -> list[dict]:
    async with get_db() as db:
        async with db.execute("SELECT * FROM workers ORDER BY last_seen DESC") as cur:
            return [dict(r) for r in await cur.fetchall()]
