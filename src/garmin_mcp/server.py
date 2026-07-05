"""FastMCP stdio server: 12 compact tools over the local warehouse.

Responses are deliberately compact (typically well under 2KB): columnar
tables, server-side aggregates, and row caps instead of raw API payloads, so
any question fits in a single tool call without flooding the model's context.

The server never prompts. Auth problems come back as structured
{"error", "hint"} dicts pointing at the login CLI, and every analysis tool
works fully offline over already-synced history.
"""

from __future__ import annotations

import functools
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import date as date_type
from datetime import timedelta
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import analysis, auth, config, db, importer
from . import sync as sync_engine
from .config import Config

SYNC_MAX_DAYS = 60
DEFAULT_RANGE_DAYS = 30

_TABLES = ("daily_wellness", "sleep", "hrv", "training_status", "activities")

mcp = FastMCP("garmin")

_config: Config | None = None


def _cfg() -> Config:
    """Load config lazily, once (env-driven; `serve` can inject the CLI's)."""
    global _config
    if _config is None:
        _config = config.load()
    return _config


def _connect(cfg: Config) -> sqlite3.Connection:
    # One short-lived connection per call: sqlite connections are cheap and
    # this avoids stale handles across a long-running server process.
    return db.connect(cfg.db_path)


def _default_range(cfg: Config, start: str | None, end: str | None) -> tuple[str, str]:
    """Fill missing bounds: end=yesterday, start=end minus 29 days."""
    end = end or sync_engine.yesterday(cfg)
    if start is None:
        start = (
            date_type.fromisoformat(end) - timedelta(days=DEFAULT_RANGE_DAYS - 1)
        ).isoformat()
    return start, end


def _tool_errors(fn: Callable[..., dict]) -> Callable[..., dict]:
    """Turn expected failures into structured results instead of protocol errors."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> dict:
        try:
            return fn(*args, **kwargs)
        except auth.AuthError as e:
            return e.to_dict()
        except ValueError as e:
            return {"error": str(e)}

    return wrapper


@mcp.tool()
@_tool_errors
def auth_status() -> dict:
    """Check whether stored Garmin Connect tokens exist.

    Use before sync, or when a sync failed with an auth error.
    """
    return auth.status(_cfg().tokens_dir)


@mcp.tool()
@_tool_errors
def sync(
    start: str | None = None,
    end: str | None = None,
    endpoints: list[str] | None = None,
) -> dict:
    """Fetch up to 60 days from Garmin Connect into the local store.

    Default: last 30 days ending yesterday. Use for catch-ups; multi-month
    backfills belong in the CLI.
    """
    cfg = _cfg()
    cfg.ensure_dirs()
    start, end = _default_range(cfg, start, end)
    n_days = (analysis._parse_date(end, "end") - analysis._parse_date(start, "start")).days + 1
    if n_days < 1:
        raise ValueError(f"start {start} is after end {end}")
    if n_days > SYNC_MAX_DAYS:
        return {
            "error": (
                f"Range is {n_days} days but this tool caps at {SYNC_MAX_DAYS}. "
                f"Run the CLI for big backfills: "
                f"garmin-local-mcp sync --from {start} --to {end}"
            )
        }
    client = auth.get_client(cfg.tokens_dir)
    with closing(_connect(cfg)) as conn:
        return sync_engine.sync_range(
            cfg, conn, client, start, end, endpoints=endpoints, progress=lambda _msg: None
        )


@mcp.tool()
@_tool_errors
def sync_status() -> dict:
    """Show local data coverage per table, last sync time, and pending sync errors.

    Use to see what date ranges are queryable.
    """
    cfg = _cfg()
    with closing(_connect(cfg)) as conn:
        coverage = {}
        for table in _TABLES:
            first, last, count = conn.execute(
                f"SELECT MIN(date), MAX(date), COUNT(*) FROM {table}"  # noqa: S608
            ).fetchone()
            coverage[table] = {"rows": count, "first": first, "last": last}
        last_sync = conn.execute("SELECT MAX(updated_at) FROM sync_state").fetchone()[0]
        errors = conn.execute(
            "SELECT endpoint, date, last_error FROM sync_state "
            "WHERE status='error' ORDER BY date DESC, endpoint LIMIT 10"
        ).fetchall()
        n_errors = conn.execute(
            "SELECT COUNT(*) FROM sync_state WHERE status='error'"
        ).fetchone()[0]
    return {
        "coverage": coverage,
        "last_sync": last_sync,
        "pending_errors": n_errors,
        "recent_errors": [
            {"endpoint": e["endpoint"], "date": e["date"], "error": e["last_error"]}
            for e in errors
        ],
    }


@mcp.tool()
@_tool_errors
def get_day(date: str) -> dict:
    """One merged view of a single day (YYYY-MM-DD): wellness, sleep, HRV,
    training status, activities, and data-quality flags.

    Use for 'how was <date>' questions.
    """
    with closing(_connect(_cfg())) as conn:
        return analysis.get_day(conn, date)


@mcp.tool()
@_tool_errors
def query_metrics(
    metrics: list[str],
    start: str,
    end: str,
    aggregate: str = "daily",
    stats: bool = False,
) -> dict:
    """Columnar time series for one or more metrics (e.g. resting_hr,
    sleep_score, steps) between two dates.

    Prefer weekly/monthly aggregate for ranges over ~60 days; stats=True adds
    mean/min/max/sd per metric.
    """
    with closing(_connect(_cfg())) as conn:
        return analysis.query_metrics(conn, metrics, start, end, aggregate, stats)


@mcp.tool()
@_tool_errors
def correlate(
    metric_a: str,
    metric_b: str,
    start: str | None = None,
    end: str | None = None,
    lag_days: int = 0,
    scan_lags: bool = False,
) -> dict:
    """Pearson/Spearman correlation between two metrics (default: last 30 days).

    Positive lag_days pairs metric_a on day D with metric_b on D+lag;
    scan_lags=True searches lags -7..+7 for the strongest relationship.
    """
    cfg = _cfg()
    start, end = _default_range(cfg, start, end)
    with closing(_connect(cfg)) as conn:
        return analysis.correlate(conn, metric_a, metric_b, start, end, lag_days, scan_lags)


@mcp.tool()
@_tool_errors
def baselines(metrics: list[str] | None = None, window_days: int | None = None) -> dict:
    """Personal mean +/- sd band per metric over a trailing window (default 28
    days; default metrics: resting_hr, hrv, sleep_score, skin_temp_dev_c,
    stress_avg, steps).

    Use to judge whether today's value is normal *for this user*.
    """
    cfg = _cfg()
    if window_days is None:
        window_days = cfg.baseline_window_days
    with closing(_connect(cfg)) as conn:
        return analysis.baselines(conn, metrics, window_days)


@mcp.tool()
@_tool_errors
def anomalies(
    metrics: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    z: float = 2.0,
) -> dict:
    """Outlier days (>= z standard deviations from the range mean) and
    sustained streaks (5+ consecutive days on one side of it).

    Default: last 30 days of the core wellness metrics.
    """
    cfg = _cfg()
    start, end = _default_range(cfg, start, end)
    with closing(_connect(cfg)) as conn:
        return analysis.anomalies(conn, metrics, start, end, z)


@mcp.tool()
@_tool_errors
def list_activities(
    type: str | None = None,
    start: str | None = None,
    end: str | None = None,
    min_distance_m: float | None = None,
    limit: int = 20,
) -> dict:
    """List recent activities newest-first as a compact table, filterable by
    type (e.g. 'running'), date range, and minimum distance.

    truncated=true means more rows exist beyond the limit.
    """
    with closing(_connect(_cfg())) as conn:
        return analysis.list_activities(conn, type, start, end, min_distance_m, limit)


@mcp.tool()
@_tool_errors
def get_activity(activity_id: int) -> dict:
    """Full stored summary row for one activity by id (from list_activities).

    Summary fields only - no GPS or sample streams.
    """
    with closing(_connect(_cfg())) as conn:
        row = conn.execute(
            "SELECT * FROM activities WHERE activity_id=?", (activity_id,)
        ).fetchone()
    if row is None:
        return {"error": f"No activity with id {activity_id}"}
    return {k: row[k] for k in row.keys()}


@mcp.tool()
@_tool_errors
def gaps(start: str | None = None, end: str | None = None) -> dict:
    """Missing days per table plus unresolved sync errors (default: first
    synced date through yesterday).

    Use to find holes worth re-syncing before drawing conclusions.
    """
    cfg = _cfg()
    with closing(_connect(cfg)) as conn:
        if start is None:
            start = conn.execute("SELECT MIN(date) FROM sync_state").fetchone()[0]
            if start is None:
                raise ValueError("No synced data yet - run the sync tool or CLI first")
        end = end or sync_engine.yesterday(cfg)
        return analysis.gaps(conn, start, end)


@mcp.tool()
@_tool_errors
def import_fit(folder: str) -> dict:
    """Import one manually exported Garmin wellness FIT bundle (folder of .fit
    files) - zero-auth offline ingest.

    Existing API-sourced rows are never overwritten.
    """
    cfg = _cfg()
    cfg.ensure_dirs()
    with closing(_connect(cfg)) as conn:
        return importer.import_bundle(conn, Path(folder))


def serve(cfg: Config | None = None) -> int:
    """Run the stdio MCP server (blocks until the client disconnects)."""
    global _config
    if cfg is not None:
        _config = cfg
    mcp.run(transport="stdio")
    return 0
