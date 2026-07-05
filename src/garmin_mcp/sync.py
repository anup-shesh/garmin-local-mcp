"""Sync engine: incremental, resumable, rate-limit-aware.

For each date x endpoint: skip if already synced, fetch, snapshot the raw
payload (immutable), parse, upsert, record sync_state. Interrupted or
rate-limited runs resume from sync_state on the next invocation. `reparse`
rebuilds the whole database from raw snapshots without any network access.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import date as date_type
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import db, raw
from .auth import AuthError
from .config import Config
from .endpoints import ENDPOINTS, TABLE_KEYS, Endpoint, activity_date

# Backoff waits between retries after a rate-limit error; a rate limit hit
# after the last wait aborts the run (cleanly - sync_state makes it resumable).
BACKOFF_SECONDS: tuple[int, ...] = (30, 120, 600)

_sleep = time.sleep  # module-level so tests can stub it out

Progress = Callable[[str], None]


class RateLimitAbort(Exception):
    """Raised internally when backoff is exhausted; never escapes sync_range."""


def yesterday(config: Config) -> str:
    """Yesterday's date in the configured timezone (fallback: system timezone)."""
    tz = ZoneInfo(config.timezone) if config.timezone else None
    now = datetime.now(tz) if tz else datetime.now().astimezone()
    return (now.date() - timedelta(days=1)).isoformat()


def default_range(config: Config, days: int = 7) -> tuple[str, str]:
    """Last `days` days ending yesterday."""
    end = yesterday(config)
    start = (date_type.fromisoformat(end) - timedelta(days=days - 1)).isoformat()
    return start, end


def _dates(start: str, end: str) -> list[str]:
    d0, d1 = date_type.fromisoformat(start), date_type.fromisoformat(end)
    if d0 > d1:
        raise ValueError(f"start {start} is after end {end}")
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def _exception_names(exc: BaseException) -> set[str]:
    return {cls.__name__ for cls in type(exc).__mro__}


def _is_rate_limit(exc: BaseException) -> bool:
    if "GarminConnectTooManyRequestsError" in _exception_names(exc):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 429 or "429" in str(exc)


def _is_auth(exc: BaseException) -> bool:
    return "GarminConnectAuthenticationError" in _exception_names(exc)


def _fetch_with_backoff(endpoint: Endpoint, client, day: str, progress: Progress):
    """Fetch one payload, backing off on rate limits; AuthError on auth failure."""
    for wait in (*BACKOFF_SECONDS, None):
        try:
            return endpoint.fetch(client, day)
        except AuthError:
            raise
        except Exception as e:
            if _is_auth(e):
                raise AuthError(f"Garmin rejected the session mid-sync: {e}") from e
            if not _is_rate_limit(e):
                raise
            if wait is None:
                raise RateLimitAbort(str(e)) from e
            progress(f"rate limited; backing off {wait}s")
            _sleep(wait)


def _store(
    config: Config,
    conn: sqlite3.Connection,
    endpoint: Endpoint,
    day: str,
    payload,
    force: bool,
) -> int:
    """Snapshot the payload, parse it, upsert rows. Returns the row count."""
    snap_path = raw.daily_path(config.raw_dir, day, endpoint.name)
    if force and snap_path.exists():
        snap_path.unlink()  # snapshots are otherwise immutable; force overwrites
    raw.write_snapshot(conn, config.raw_dir, snap_path, payload, day, endpoint.name)

    if endpoint.name == "activities" and isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict) or item.get("activityId") is None:
                continue
            act_path = raw.activity_path(config.raw_dir, item["activityId"])
            if force and act_path.exists():
                act_path.unlink()
            raw.write_snapshot(
                conn, config.raw_dir, act_path, item, activity_date(item, day), "activities"
            )

    rows = endpoint.parse(payload, day)
    fetched_at = db.utcnow()
    for table, row in rows:
        db.upsert_partial(conn, table, {**row, "fetched_at": fetched_at}, TABLE_KEYS[table])
    return len(rows)


def sync_range(
    config: Config,
    conn: sqlite3.Connection,
    client,
    start: str,
    end: str,
    endpoints: list[str] | None = None,
    force: bool = False,
    progress: Progress = print,
) -> dict:
    """Sync [start, end] (inclusive, oldest first). Returns a report dict.

    Rate-limit exhaustion and auth failures abort cleanly with resumable=True;
    any other per-(date, endpoint) error is recorded in sync_state and skipped.
    """
    names = list(endpoints) if endpoints else list(ENDPOINTS)
    unknown = [n for n in names if n not in ENDPOINTS]
    if unknown:
        raise ValueError(f"Unknown endpoints: {unknown}. Known: {list(ENDPOINTS)}")

    report: dict = {
        "start": start,
        "end": end,
        "requests": 0,
        "rows": 0,
        "aborted": None,
        "resumable": False,
        "endpoints": {n: {"ok": 0, "empty": 0, "skipped": 0, "error": 0} for n in names},
    }
    made_request = False
    for day in _dates(start, end):
        for name in names:
            endpoint = ENDPOINTS[name]
            stats = report["endpoints"][name]
            if not force and db.synced_ok(conn, name, day):
                stats["skipped"] += 1
                continue
            if made_request and config.request_delay_seconds > 0:
                _sleep(config.request_delay_seconds)
            made_request = True
            try:
                payload = _fetch_with_backoff(endpoint, client, day, progress)
                report["requests"] += 1
            except RateLimitAbort as e:
                db.mark_sync(conn, name, day, "error", f"rate limited: {e}")
                stats["error"] += 1
                report["aborted"] = "rate_limited"
                report["resumable"] = True
                progress(f"{day} {name}: rate limited - aborting; re-run to resume")
                return report
            except AuthError as e:
                db.mark_sync(conn, name, day, "error", str(e))
                stats["error"] += 1
                report["aborted"] = "auth"
                report["resumable"] = True
                report["auth_error"] = e.to_dict()
                progress(f"{day} {name}: auth error - aborting ({e.hint})")
                return report
            except Exception as e:
                db.mark_sync(conn, name, day, "error", str(e))
                stats["error"] += 1
                progress(f"{day} {name}: error - {e}")
                continue
            try:
                n_rows = _store(config, conn, endpoint, day, payload, force)
            except Exception as e:
                db.mark_sync(conn, name, day, "error", f"parse/store: {e}")
                stats["error"] += 1
                progress(f"{day} {name}: parse/store error - {e}")
                continue
            status = "ok" if n_rows else "empty"
            db.mark_sync(conn, name, day, status)
            stats[status] += 1
            report["rows"] += n_rows
            progress(f"{day} {name}: {status}" + (f" ({n_rows} rows)" if n_rows else ""))
    return report


def reparse(config: Config, conn: sqlite3.Connection, progress: Progress = print) -> dict:
    """Rebuild database tables from raw snapshots on disk. Fully offline.

    Re-runs every parse function, re-registers snapshots in raw_snapshots, and
    restores sync_state coverage so the next `sync` does not re-fetch.
    """
    report = {"daily_snapshots": 0, "activity_snapshots": 0, "rows": 0, "unknown_endpoints": 0}
    for day, name, path in raw.iter_daily_snapshots(config.raw_dir):
        endpoint = ENDPOINTS.get(name)
        if endpoint is None:
            report["unknown_endpoints"] += 1
            continue
        payload = raw.read_snapshot(path)
        raw.index_snapshot(conn, config.raw_dir, path, day, name)
        rows = endpoint.parse(payload, day)
        fetched_at = db.utcnow()
        for table, row in rows:
            db.upsert_partial(conn, table, {**row, "fetched_at": fetched_at}, TABLE_KEYS[table])
        db.mark_sync(conn, name, day, "ok" if rows else "empty")
        report["daily_snapshots"] += 1
        report["rows"] += len(rows)

    activities_dir = config.raw_dir / "activities"
    if activities_dir.is_dir():
        parse = ENDPOINTS["activities"].parse
        for path in sorted(activities_dir.glob("*.json")):
            item = raw.read_snapshot(path)
            if not isinstance(item, dict):
                continue
            day = activity_date(item)
            raw.index_snapshot(conn, config.raw_dir, path, day, "activities")
            rows = parse([item], day or "")
            fetched_at = db.utcnow()
            for table, row in rows:
                db.upsert_partial(conn, table, {**row, "fetched_at": fetched_at}, TABLE_KEYS[table])
            report["activity_snapshots"] += 1
            report["rows"] += len(rows)

    progress(
        f"reparse: {report['daily_snapshots']} daily + {report['activity_snapshots']} activity "
        f"snapshots -> {report['rows']} rows"
    )
    return report
