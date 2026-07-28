"""
Database queries against Hermes state.db.

All data is read-only. Writes nothing.
"""

import sqlite3
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def get_db_path() -> str:
    """Locate the Hermes state.db, checking profiles too."""
    hermes_home = os.environ.get("HERMES_HOME") or Path.home() / ".hermes"
    # Primary: current profile's state.db
    profile = os.environ.get("HERMES_PROFILE")
    if profile:
        candidate = Path(hermes_home) / "profiles" / profile / "state.db"
        if candidate.exists():
            return str(candidate)
    # Fallback: default state.db
    candidate = Path(hermes_home) / "state.db"
    if candidate.exists():
        return str(candidate)
    # Last resort: search profiles
    profiles_dir = Path(hermes_home) / "profiles"
    if profiles_dir.exists():
        for p in sorted(profiles_dir.iterdir()):
            if p.is_dir():
                candidate = p / "state.db"
                if candidate.exists():
                    return str(candidate)
    raise FileNotFoundError(
        "Could not find Hermes state.db. Is Hermes installed and has it run before?"
    )


def get_connection() -> sqlite3.Connection:
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    return conn


def get_summary(days: Optional[int] = 30) -> dict:
    """Aggregate cost, token, and session stats over a period."""
    conn = get_connection()
    try:
        since = (
            datetime.now(timezone.utc).timestamp() - days * 86400
            if days
            else 0
        )

        # Overall stats
        row = conn.execute(
            """
            SELECT
                COUNT(*) as session_count,
                COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                COALESCE(SUM(output_tokens), 0) as total_output_tokens,
                COALESCE(SUM(estimated_cost_usd), 0) as total_cost,
                COALESCE(SUM(cache_read_tokens), 0) as cache_read_tokens,
                COALESCE(SUM(cache_write_tokens), 0) as cache_write_tokens,
                COALESCE(SUM(reasoning_tokens), 0) as reasoning_tokens
            FROM sessions
            WHERE started_at >= ?
        """,
            (since,),
        ).fetchone()
        stats = dict(row)

        # Cost by model
        model_rows = conn.execute(
            """
            SELECT
                model,
                SUM(estimated_cost_usd) as cost,
                SUM(input_tokens) as input_tokens,
                SUM(output_tokens) as output_tokens,
                COUNT(*) as session_count
            FROM sessions
            WHERE started_at >= ? AND model IS NOT NULL AND model != ''
            GROUP BY model
            ORDER BY cost DESC
        """,
            (since,),
        ).fetchall()
        stats["by_model"] = [dict(r) for r in model_rows]

        # Cost by profile
        profile_rows = conn.execute(
            """
            SELECT
                COALESCE(profile_name, 'default') as profile,
                SUM(estimated_cost_usd) as cost,
                SUM(input_tokens) as input_tokens,
                SUM(output_tokens) as output_tokens,
                COUNT(*) as session_count
            FROM sessions
            WHERE started_at >= ?
            GROUP BY profile_name
            ORDER BY cost DESC
        """,
            (since,),
        ).fetchall()
        stats["by_profile"] = [dict(r) for r in profile_rows]

        # Daily cost breakdown (last 30 days)
        daily_rows = conn.execute(
            """
            SELECT
                DATE(started_at, 'unixepoch') as day,
                SUM(estimated_cost_usd) as cost,
                SUM(input_tokens) as input_tokens,
                SUM(output_tokens) as output_tokens,
                COUNT(*) as session_count
            FROM sessions
            WHERE started_at >= ?
            GROUP BY DATE(started_at, 'unixepoch')
            ORDER BY day ASC
        """,
            (since,),
        ).fetchall()
        stats["daily"] = [dict(r) for r in daily_rows]

        return stats
    finally:
        conn.close()


def get_recent_sessions(limit: int = 20) -> list[dict]:
    """Most recent sessions with cost and token info."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT
                id, title, profile_name, model, started_at, ended_at,
                input_tokens, output_tokens, estimated_cost_usd,
                cost_status, billing_provider, message_count
            FROM sessions
            WHERE model IS NOT NULL AND model != ''
            ORDER BY started_at DESC
            LIMIT ?
        """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_profile_cost_summary(days: Optional[int] = 30) -> list[dict]:
    """Detailed per-profile breakdown (uses session_model_usage for precision)."""
    conn = get_connection()
    try:
        since = (
            datetime.now(timezone.utc).timestamp() - days * 86400
            if days
            else 0
        )
        rows = conn.execute(
            """
            SELECT
                s.profile_name,
                m.model,
                m.billing_provider,
                SUM(m.input_tokens) as input_tokens,
                SUM(m.output_tokens) as output_tokens,
                SUM(m.estimated_cost_usd) as cost,
                SUM(m.api_call_count) as api_calls
            FROM session_model_usage m
            JOIN sessions s ON s.id = m.session_id
            WHERE m.last_seen >= ?
            GROUP BY s.profile_name, m.model, m.billing_provider
            ORDER BY s.profile_name, cost DESC
        """,
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
