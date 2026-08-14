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
import json
import re
import subprocess
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

# Providers whose API keys are on a FREE TIER (Google AI Studio free-tier
# projects — billy/issy/chronicler/ukfatguy/sarah run these). Hermes prices
# them at official list price, so their "estimated" cost is phantom spend:
# free tier bills $0 until rate limits are hit (then it errors, it doesn't
# bill). We zero those costs at aggregation time and expose the excluded
# amount, so the dashboard shows expected real billing rather than list
# price. If a profile ever upgrades to paid Gemini, remove "gemini" here.
FREE_TIER_PROVIDERS = frozenset({"gemini"})

# Reference per-day request limits for the RPD panel (Inc 5). From the
# 2026-08-14 rate-limit deep-dive: Google free-tier projects cap Lite models
# at 500 RPD and plain Flash at 20 RPD (Hermes-side traffic). These are
# REFERENCE values for eyeballing headroom — Google's own counter also counts
# AI Studio + direct API traffic to the same project, so parity is never exact.
# Models we don't have a verified limit for show as "—" (honest, not invented).
def _rpd_limit(model: str) -> Optional[int]:
    if not model:
        return None
    m = model.lower()
    if "lite" in m:
        return 500
    if m in {
        "gemini-2.0-flash",
        "gemini-3-flash",
        "gemini-3.5-flash",
    }:
        return 20
    return None




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
    SUM(m.cache_read_tokens) as cache_read_tokens,
    SUM(m.cache_write_tokens) as cache_write_tokens,
    SUM(m.reasoning_tokens) as reasoning_tokens,
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


def _free_tier_in_clause() -> tuple[str, list]:
    """SQL IN-clause + params for FREE_TIER_PROVIDERS (empty set → never match)."""
    if not FREE_TIER_PROVIDERS:
        return "0", []
    placeholders = ",".join("?" for _ in FREE_TIER_PROVIDERS)
    return f"({placeholders})", sorted(FREE_TIER_PROVIDERS)


def _free_tier_cost(conn: sqlite3.Connection, since: float) -> float:
    """Total free-tier (phantom) cost in this DB since `since`."""
    in_clause, params = _free_tier_in_clause()
    row = conn.execute(
        f"""SELECT COALESCE(SUM(m.estimated_cost_usd), 0) as free_cost
            FROM session_model_usage m
            JOIN sessions s ON s.id = m.session_id
            WHERE m.last_seen >= ? AND m.billing_provider IN {in_clause}""",
        (since, *params),
    ).fetchone()
    return float(row["free_cost"] or 0)


def _free_tier_by_profile(conn: sqlite3.Connection, since: float, label: str) -> dict:
    """Free-tier cost per profile_name, keyed exactly like _PROFILE_SUM_SQL."""
    in_clause, params = _free_tier_in_clause()
    rows = conn.execute(
        f"""SELECT COALESCE(s.profile_name, ?) as profile,
                   COALESCE(SUM(m.estimated_cost_usd), 0) as free_cost
            FROM session_model_usage m
            JOIN sessions s ON s.id = m.session_id
            WHERE m.last_seen >= ? AND m.billing_provider IN {in_clause}
            GROUP BY COALESCE(s.profile_name, ?)""",
        (label, since, *params, label),
    ).fetchall()
    return {r["profile"]: float(r["free_cost"] or 0) for r in rows}


def _free_tier_by_day(conn: sqlite3.Connection, since: float) -> dict:
    """Free-tier cost per day, keyed exactly like _DAILY_SUM_SQL."""
    in_clause, params = _free_tier_in_clause()
    rows = conn.execute(
        f"""SELECT DATE(s.started_at, 'unixepoch') as day,
                   COALESCE(SUM(m.estimated_cost_usd), 0) as free_cost
            FROM session_model_usage m
            JOIN sessions s ON s.id = m.session_id
            WHERE m.last_seen >= ? AND m.billing_provider IN {in_clause}
            GROUP BY DATE(s.started_at, 'unixepoch')""",
        (since, *params),
    ).fetchall()
    return {r["day"]: float(r["free_cost"] or 0) for r in rows}


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
        "free_tier_cost": 0.0,
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
            free_cost = _free_tier_cost(conn, since)
            free_by_profile = _free_tier_by_profile(conn, since, label)
            free_by_day = _free_tier_by_day(conn, since)

            row = conn.execute(_SESSION_SUM_SQL, (since,)).fetchone()
            merged["session_count"] += row["session_count"]
            merged["total_input_tokens"] += row["total_input_tokens"]
            merged["total_output_tokens"] += row["total_output_tokens"]
            merged["total_cost"] += max(0.0, (row["total_cost"] or 0) - free_cost)
            merged["free_tier_cost"] += free_cost
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
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "reasoning_tokens": 0,
                    "api_calls": 0,
                })
                if r["billing_provider"] in FREE_TIER_PROVIDERS:
                    e["free_tier"] = True
                    # list-price estimate is phantom spend — keep tokens, drop cost
                    e["cost"] += 0.0
                else:
                    e["cost"] += r["cost"] or 0
                e["input_tokens"] += r["input_tokens"] or 0
                e["output_tokens"] += r["output_tokens"] or 0
                e["cache_read_tokens"] += r["cache_read_tokens"] or 0
                e["cache_write_tokens"] += r["cache_write_tokens"] or 0
                e["reasoning_tokens"] += r["reasoning_tokens"] or 0
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
                e["cost"] += max(0.0, (r["cost"] or 0) - free_by_profile.get(key, 0.0))
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
                e["cost"] += max(0.0, (r["cost"] or 0) - free_by_day.get(day, 0.0))
                e["input_tokens"] += r["input_tokens"] or 0
                e["output_tokens"] += r["output_tokens"] or 0
                e["session_count"] += r["session_count"]
        finally:
            conn.close()

    merged["by_model"] = sorted(merged["by_model"].values(), key=lambda x: -x["cost"])
    merged["by_profile"] = sorted(merged["by_profile"].values(), key=lambda x: -x["cost"])
    merged["daily"] = sorted(merged["daily"].values(), key=lambda x: x["day"])
    merged["free_tier_providers"] = sorted(FREE_TIER_PROVIDERS)

    # Inc 2: daily burn + projected monthly (post-zeroing, from merged daily series)
    today_utc = datetime.now(timezone.utc).date()
    merged["today_cost"] = 0.0
    merged["avg_daily_burn"] = 0.0
    merged["projected_monthly"] = 0.0
    if merged["daily"]:
        last_day = merged["daily"][-1]
        if last_day["day"] == today_utc.isoformat():
            merged["today_cost"] = last_day["cost"]
        first = date.fromisoformat(merged["daily"][0]["day"])
        elapsed = max(1, (today_utc - first).days + 1)
        if days:
            elapsed = min(elapsed, days)
        merged["avg_daily_burn"] = merged["total_cost"] / elapsed
        merged["projected_monthly"] = merged["avg_daily_burn"] * 30

    # Inc 3: effective rates per model (USD per 1M tokens, post-zeroing)
    for e in merged["by_model"]:
        e["usd_per_1m_input"] = (
            round(e["cost"] / e["input_tokens"] * 1_000_000, 4) if e["input_tokens"] else None
        )
        e["usd_per_1m_output"] = (
            round(e["cost"] / e["output_tokens"] * 1_000_000, 4) if e["output_tokens"] else None
        )

    # Inc 4: spend threshold alerts. Thresholds via env (defaults: £0.79-adj
    # friendly $1.00/day, $15.00/month projected). Tray + dashboard react to level.
    alert_daily = float(os.environ.get("COST_ALERT_DAILY_USD", "1.00"))
    alert_monthly = float(os.environ.get("COST_ALERT_MONTHLY_USD", "15.00"))
    daily_breached = merged["today_cost"] >= alert_daily
    monthly_breached = merged["projected_monthly"] >= alert_monthly
    if daily_breached or monthly_breached:
        level = "critical"
    elif (alert_daily and merged["today_cost"] >= 0.8 * alert_daily) or (
        alert_monthly and merged["projected_monthly"] >= 0.8 * alert_monthly
    ):
        level = "warn"
    else:
        level = "ok"
    merged["alerts"] = {
        "level": level,
        "daily": {
            "threshold_usd": alert_daily,
            "current_usd": merged["today_cost"],
            "breached": daily_breached,
        },
        "monthly": {
            "threshold_usd": alert_monthly,
            "current_usd": merged["projected_monthly"],
            "breached": monthly_breached,
        },
    }
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
        rows = [dict(r) for r in rows]
        for row in rows:
            # Free-tier providers: list-price estimate is phantom spend.
            # Keep the row visible but show it as free.
            if row.get("billing_provider") in FREE_TIER_PROVIDERS:
                row["estimated_cost_usd"] = 0.0
                row["cost_status"] = "free_tier"
        return rows
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
        rows = [dict(r) for r in rows]
        for row in rows:
            # Free-tier providers: keep usage, drop the phantom list-price cost.
            if row.get("billing_provider") in FREE_TIER_PROVIDERS:
                row["cost"] = 0.0
                row["free_tier"] = True
        return rows
    finally:
        conn.close()


# ─── Inc 5: usage/rate-limit telemetry (RPD + journald 429 watch) ───────────

_RPD_SQL = """
SELECT
    DATE(m.last_seen, 'unixepoch') as day,
    m.model,
    m.billing_provider,
    SUM(m.api_call_count) as requests
FROM session_model_usage m
WHERE m.last_seen >= ?
GROUP BY DATE(m.last_seen, 'unixepoch'), m.model, m.billing_provider
"""


def get_rpd(days: int = 14) -> list[dict]:
    """Per-day, per-model request counts across ALL profile DBs.

    Built from session_model_usage.api_call_count — accurate for Hermes
    traffic, NOT Google's counter (which also counts AI Studio + direct API
    calls to the same project). TRUE RPM/TPM peaks are not reconstructable
    from state.db (session-level aggregates only) — say so in the UI.
    """
    since = datetime.now(timezone.utc).timestamp() - days * 86400 if days else 0
    merged: dict[tuple, dict] = {}
    for label, path in get_all_db_paths():
        try:
            conn = _connect(path)
        except Exception:
            continue
        try:
            for r in conn.execute(_RPD_SQL, (since,)):
                key = (r["day"], r["model"], r["billing_provider"])
                e = merged.setdefault(key, {
                    "day": r["day"],
                    "model": r["model"],
                    "billing_provider": r["billing_provider"],
                    "requests": 0,
                })
                e["requests"] += r["requests"] or 0
        finally:
            conn.close()
    rows = sorted(merged.values(), key=lambda x: (x["day"], x["model"]))
    for e in rows:
        e["rpd_limit"] = _rpd_limit(e["model"])
    return rows


# systemd user units whose agent loops we attribute rate-limit events to.
# The desktop backend (hermes-serve.service) is where profile-switched
# desktop traffic logs; per-profile units are exact.
_JOURNALD_UNITS = [
    "hermes-serve.service",
    "hermes-serve-ukfatguy.service",
    "hermes-serve-issy.service",
    "hermes-serve-billy.service",
    "hermes-serve-sarah.service",
    "hermes-gateway.service",
]

_RE_FAILED = re.compile(r"API call failed \(attempt (\d+)/(\d+)\)")
_RE_BACKOFF = re.compile(r"Rate limited\. Waiting ([\d.]+)s \(attempt (\d+)/(\d+)\)")
_RE_EXHAUSTED = re.compile(r"Rate limited after (\d+) retries")
_RE_FINAL = re.compile(r"Final error:")


def _unit_profile(unit: str) -> str:
    m = re.match(r"hermes-serve-(.+)\.service", unit)
    if m:
        return m.group(1)
    if unit == "hermes-serve.service":
        return "desktop"
    if unit == "hermes-gateway.service":
        return "gateway"
    return unit


def get_rate_limit_events(days: int = 7) -> dict:
    """Parse journald for Hermes 429/rate-limit log lines (read-only watch).

    journalctl --user with --grep (systemd >= 247) returns only matching
    lines, but the scan still walks the whole journal: 30 days ≈ 1.15M lines
    ≈ 22s (measured 2026-08-14). So the scan window is bounded to 14 days and
    results are cached per 5-minute bucket — at most one slow scan per bucket,
    instant hits after that. The 'exhausted' lines ("Rate limited after N
    retries") are the real ceiling hits — one per retry cycle, so counting
    them is dedup-safe (the ⚠ failed-attempt / ⏱ backoff / 💀 final-error
    lines around it are context, not separate events).
    """
    days = min(max(days, 1), 14)  # journald scan cost bounds the window
    bucket = int(time.time() // 300)  # 5-minute cache bucket
    key = (days, bucket)
    with _rl_cache_lock:
        if key in _rl_cache:
            return _rl_cache[key]
    result = _build_rate_limit_events(days)
    with _rl_cache_lock:
        _rl_cache[key] = result
        for stale in [k for k in _rl_cache if k[1] < bucket - 1]:
            del _rl_cache[stale]
    return result


_rl_cache: dict = {}
_rl_cache_lock = threading.Lock()


def _build_rate_limit_events(days: int) -> dict:
    cmd = [
        "journalctl", "--user",
        f"--since={days} days ago",
        "-o", "json",
        "--grep=HTTP 429|Rate limited|Final error",
    ]
    for u in _JOURNALD_UNITS:
        cmd += ["-u", u]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception:
        return {"error": "journalctl failed", "days": days,
                "total_events": 0, "by_day": [], "by_unit": [], "recent": []}
    # journalctl exits 0 even on a bad --since / bad --grep (prints to stderr
    # and returns nothing) — treat any stderr output as a hard failure so the
    # panel shows an error instead of silently reporting "no events".
    if proc.returncode != 0 or proc.stderr.strip():
        return {"error": f"journalctl: {proc.stderr.strip()[:200]}", "days": days,
                "total_events": 0, "by_day": [], "by_unit": [], "recent": []}

    exhausted = []  # (ts_us, unit, attempts, detail)
    for ln in proc.stdout.splitlines():
        if not ln.strip().startswith("{"):
            continue
        try:
            j = json.loads(ln)
        except Exception:
            continue
        unit = j.get("_SYSTEMD_USER_UNIT") or j.get("_SYSTEMD_UNIT") or "?"
        ts_us = int(j.get("__REALTIME_TIMESTAMP") or 0)
        msg = j.get("MESSAGE") or ""
        m = _RE_EXHAUSTED.search(msg)
        if m:
            exhausted.append((ts_us, unit, int(m.group(1)), msg))

    by_day: dict[str, int] = {}
    by_unit: dict[str, int] = {}
    for ts_us, unit, attempts, detail in exhausted:
        day = datetime.fromtimestamp(ts_us / 1_000_000, timezone.utc).date().isoformat()
        by_day[day] = by_day.get(day, 0) + 1
        by_unit[unit] = by_unit.get(unit, 0) + 1

    recent = []
    for ts_us, unit, attempts, detail in sorted(exhausted, key=lambda x: -x[0])[:20]:
        recent.append({
            "ts": round(ts_us / 1_000_000, 1),
            "unit": unit,
            "profile": _unit_profile(unit),
            "attempts": attempts,
            "detail": detail[:220],
        })

    return {
        "days": days,
        "total_events": len(exhausted),
        "by_day": [{"day": k, "count": v} for k, v in sorted(by_day.items())],
        "by_unit": [
            {"unit": k, "profile": _unit_profile(k), "count": v}
            for k, v in sorted(by_unit.items(), key=lambda x: -x[1])
        ],
        "recent": recent,
    }


def get_usage_telemetry(days: int = 14) -> dict:
    """Inc 5 bundle: RPD series + journald rate-limit events.

    RPD is capped at the last 14 days (chart readability + bounded payload);
    the period selector still drives the spend panels. Rate-limit events are
    independently capped by the journald scan window in get_rate_limit_events.
    """
    return {
        "days": days,
        "rpd": get_rpd(min(days or 14, 14)),
        "rate_limits": get_rate_limit_events(days),
    }
