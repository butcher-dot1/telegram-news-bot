"""
Async database module using asyncpg for Supabase PostgreSQL.
Provides connection pool management and helper functions for
users and daily_papers tables.
"""

import os
import asyncpg
import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# Global connection pool
_pool: asyncpg.Pool | None = None


async def init_db() -> None:
    """Initialize the asyncpg connection pool."""
    global _pool
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL environment variable is not set.")
    _pool = await asyncpg.create_pool(dsn=database_url, min_size=2, max_size=10)
    logger.info("Database connection pool initialized.")


async def close_db() -> None:
    """Close the asyncpg connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("Database connection pool closed.")


def _get_pool() -> asyncpg.Pool:
    """Get the active connection pool or raise an error."""
    if _pool is None:
        raise RuntimeError("Database pool is not initialized. Call init_db() first.")
    return _pool


# ─── User Operations ────────────────────────────────────────────────

async def add_user(user_id: int, plan: str, days: int = 30) -> None:
    """Insert or update a user subscription with the given plan and duration."""
    pool = _get_pool()
    start = date.today()
    expiry = start + timedelta(days=days)
    await pool.execute(
        """
        INSERT INTO users (user_id, plan, start_date, expiry_date)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (user_id) DO UPDATE
        SET plan = $2, start_date = $3, expiry_date = $4
        """,
        user_id, plan, start, expiry,
    )
    logger.info(f"User {user_id} subscribed to '{plan}' until {expiry}.")


async def get_user(user_id: int) -> dict | None:
    """Fetch a user's subscription details. Returns None if not found."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT user_id, plan, start_date, expiry_date FROM users WHERE user_id = $1",
        user_id,
    )
    return dict(row) if row else None


async def get_active_users(plan: str | None = None) -> list[dict]:
    """Fetch all users with active (non-expired) subscriptions.
    Optionally filter by plan name.
    """
    pool = _get_pool()
    if plan:
        rows = await pool.fetch(
            "SELECT user_id, plan FROM users WHERE expiry_date >= $1 AND plan = $2",
            date.today(), plan,
        )
    else:
        rows = await pool.fetch(
            "SELECT user_id, plan FROM users WHERE expiry_date >= $1",
            date.today(),
        )
    return [dict(r) for r in rows]


async def delete_expired_users() -> int:
    """Delete all users whose subscription has expired. Returns count deleted."""
    pool = _get_pool()
    result = await pool.execute(
        "DELETE FROM users WHERE expiry_date < $1",
        date.today(),
    )
    count = int(result.split()[-1])
    logger.info(f"Cleaned up {count} expired user(s).")
    return count


# ─── Daily Papers Operations ────────────────────────────────────────

async def add_paper(plan_name: str, file_id: str) -> None:
    """Save a new daily paper entry."""
    pool = _get_pool()
    await pool.execute(
        "INSERT INTO daily_papers (plan_name, file_id) VALUES ($1, $2)",
        plan_name, file_id,
    )
    logger.info(f"Saved paper for '{plan_name}' with file_id={file_id[:20]}...")


async def get_todays_papers() -> list[dict]:
    """Fetch all papers uploaded today."""
    pool = _get_pool()
    rows = await pool.fetch(
        "SELECT plan_name, file_id FROM daily_papers WHERE upload_date = $1",
        date.today(),
    )
    return [dict(r) for r in rows]
