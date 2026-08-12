"""Database queries against Hermes state.db files.

All data is read-only. Writes nothing.

Multi-DB aggregation: every Hermes profile has its own state.db
(default: ~/.hermes/state.db, named profiles: ~/.hermes/profiles/*/state.db).
Summaries aggregate across ALL of them so the dashboard shows fleet-wide
spend, not just the default agent. Single-DB helpers (recent sessions)
still target the current profile's DB for backward compatibility.
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


def get_all_db_paths() -> list[tuple[str, Path]]:
    """Return [(profile_label, state.db path)] for default + every named profile."""
    hermes_home = os.environ.get("HERMES_HOME") or Path.home() / ".hermes"
    paths: list[tuple[str, Path]] = []
    default = Path(hermes_home) / "state.db"
    if default.exists():
        paths.append(("default", default))
    profiles_dir = Path(hermes_home) / "profiles"
    if profiles_dir.exists():
        for p in sorted(profiles_dir.iterdir()):
            if p.is_dir():
                cand = p / "state.db"
                if cand.exists():
                    paths.append((p.name, cand))
    return paths


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    return conn


_SESSION_SUM_SQL = """
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
"""

_MODEL_SUM_SQL = """
SELECT
    m.model,
    m.billing_provider,
    SUM(m.estimated_cost_usd) as cost,
    SUM(m.input_tokens) as input_tokens,
    SUM(m.output_tokens) as output_tokens,
    SUM(m.api_call_count) as api_calls
FROM session_model_usage m
JOIN sessions s ON s.id = m.session_id
WHERE m.last_seen >= ?
GROUP BY m.model, m.billing_provider
"""

_PROFILE_SUM_SQL = """
SELECT
    COALESCE(profile_name, ?) as profile,
    SUM(estimated_cost_usd) as cost,
    SUM(input_tokens) as input_tokens,
    SUM(output_tokens) as output_tokens,
    COUNT(*) as session_count
FROM sessions
WHERE started_at >= ?
GROUP BY profile_name
"""

_DAILY_SUM_SQL = """
SELECT
    DATE(started_at, 'unixepoch') as day,
    SUM(estimated_cost_usd) as cost,
    SUM(input_tokens) as input_tokens,
    SUM(output_tokens) as output_tokens,
    COUNT(*) as session_count
FROM sessions
WHERE started_at >= ?
GROUP BY DATE(started_at, 'unixepoch')
"""


def get_summary(days: Optional[int] = 30) -> dict:
    """Aggregate cost, token, and session stats across ALL profile DBs."""
    since = (
        datetime.now(timezone.utc).timestamp() - days * 86400
        if days
        else 0
    )

    merged = {
        "session_count": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cost": 0.0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "by_model": {},
        "by_profile": {},
        "daily": {},
        "databases": [],
    }

    for label, path in get_all_db_paths():
        merged["databases"].append(label)
        try:
            conn = _connect(path)
        except Exception:
            continue
        try:
            row = conn.execute(_SESSION_SUM_SQL, (since,)).fetchone()
            merged["session_count"] += row["session_count"]
            merged["total_input_tokens"] += row["total_input_tokens"]
            merged["total_output_tokens"] += row["total_output_tokens"]
            merged["total_cost"] += row["total_cost"]
            merged["cache_read_tokens"] += row["cache_read_tokens"]
            merged["cache_write_tokens"] += row["cache_write_tokens"]
            merged["reasoning_tokens"] += row["reasoning_tokens"]

            for r in conn.execute(_MODEL_SUM_SQL, (since,)):
                key = (r["model"], r["billing_provider"])
                e = merged["by_model"].setdefault(key, {
                    "model": r["model"],
                    "billing_provider": r["billing_provider"],
                    "cost": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "api_calls": 0,
                })
                e["cost"] += r["cost"] or 0
                e["input_tokens"] += r["input_tokens"] or 0
                e["output_tokens"] += r["output_tokens"] or 0
                e["api_calls"] += r["api_calls"] or 0

            for r in conn.execute(_PROFILE_SUM_SQL, (label, since)):
                key = r["profile"] or label
                e = merged["by_profile"].setdefault(key, {
                    "profile": key,
                    "cost": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "session_count": 0,
                })
                e["cost"] += r["cost"] or 0
                e["input_tokens"] += r["input_tokens"] or 0
                e["output_tokens"] += r["output_tokens"] or 0
                e["session_count"] += r["session_count"]

            for r in conn.execute(_DAILY_SUM_SQL, (since,)):
                day = r["day"]
                e = merged["daily"].setdefault(day, {
                    "day": day,
                    "cost": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "session_count": 0,
                })
                e["cost"] += r["cost"] or 0
                e["input_tokens"] += r["input_tokens"] or 0
                e["output_tokens"] += r["output_tokens"] or 0
                e["session_count"] += r["session_count"]
        finally:
            conn.close()

    merged["by_model"] = sorted(merged["by_model"].values(), key=lambda x: -x["cost"])
    merged["by_profile"] = sorted(merged["by_profile"].values(), key=lambda x: -x["cost"])
    merged["daily"] = sorted(merged["daily"].values(), key=lambda x: x["day"])
    return merged


def get_recent_sessions(limit: int = 20) -> list[dict]:
    """Most recent sessions with cost and token info (current DB)."""
    conn = _connect(Path(get_db_path()))
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
    """Detailed per-profile breakdown (current DB, uses session_model_usage)."""
    since = (
        datetime.now(timezone.utc).timestamp() - days * 86400
        if days
        else 0
    )
    conn = _connect(Path(get_db_path()))
    try:
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
