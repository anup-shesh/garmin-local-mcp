"""Server-side analysis over the local warehouse.

Pure functions on a sqlite3.Connection: trends, correlations, baselines,
anomalies, and compact day/activity views. All metric names resolve through
the registry in :mod:`metrics`, all math is stdlib, and every function returns
plain JSON-able dicts sized for a single MCP tool response.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date as date_type
from datetime import timedelta

from .metrics import Metric, resolve

AGGREGATES = ("daily", "weekly", "monthly")

# Default metric set for baselines/anomalies: the daily-readiness vitals.
DEFAULT_BASELINE_METRICS = (
    "resting_hr",
    "hrv",
    "sleep_score",
    "skin_temp_dev_c",
    "stress_avg",
    "steps",
)

# Tables checked for missing days by gaps(); training_status and activities
# legitimately have empty days, so absence there is not a gap.
_DAILY_TABLES = ("daily_wellness", "sleep", "hrv")

_MIN_CORR_N = 5
_MIN_STREAK_LEN = 5
_SCAN_LAG_RANGE = range(-7, 8)


# --- small helpers -----------------------------------------------------------


def _parse_date(value: str, label: str = "date") -> date_type:
    try:
        return date_type.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {label} {value!r}: expected YYYY-MM-DD") from None


def _validate_range(start: str, end: str) -> None:
    if _parse_date(start, "start") > _parse_date(end, "end"):
        raise ValueError(f"start {start} is after end {end}")


def _shift(day: str, days: int) -> str:
    return (date_type.fromisoformat(day) + timedelta(days=days)).isoformat()


def _dates(start: str, end: str) -> list[str]:
    d0, d1 = date_type.fromisoformat(start), date_type.fromisoformat(end)
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def _round2(value: float) -> float:
    return round(value, 2)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _pstdev(values: list[float], mean: float | None = None) -> float:
    """Population standard deviation (denominator n)."""
    m = _mean(values) if mean is None else mean
    return math.sqrt(sum((v - m) ** 2 for v in values) / len(values))


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson r; None when either series has zero variance."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return sxy / math.sqrt(sxx * syy)


def _ranks(values: list[float]) -> list[float]:
    """Ranks (1-based) with ties assigned their average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    return _pearson(_ranks(xs), _ranks(ys))


def _require_numeric(metric: Metric, operation: str) -> None:
    if not metric.numeric:
        raise ValueError(
            f"Metric {metric.name!r} is categorical and cannot be used in {operation}"
        )


# --- metric series ------------------------------------------------------------

Series = dict[str, float]  # date -> value (non-null only)


def _series_by_metric(
    conn: sqlite3.Connection, resolved: list[Metric], start: str, end: str
) -> dict[str, Series]:
    """Per-metric date->value maps, one query per source table.

    Metrics over the activities table are per-day aggregates (their registry
    exprs are SUM()/COUNT()), so that table is queried with GROUP BY date.
    """
    by_table: dict[str, list[Metric]] = {}
    for m in resolved:
        by_table.setdefault(m.table, []).append(m)

    out: dict[str, Series] = {m.name: {} for m in resolved}
    for table, table_metrics in by_table.items():
        selects = ", ".join(f"{m.expr} AS c{i}" for i, m in enumerate(table_metrics))
        sql = f"SELECT date, {selects} FROM {table} WHERE date BETWEEN ? AND ?"  # noqa: S608
        if table == "activities":
            sql += " GROUP BY date"
        for row in conn.execute(sql, (start, end)):
            for i, m in enumerate(table_metrics):
                value = row[f"c{i}"]
                if value is not None:
                    out[m.name][row["date"]] = value
    return out


def _bucket_label(day: str, aggregate: str) -> str:
    if aggregate == "weekly":
        d = date_type.fromisoformat(day)
        return (d - timedelta(days=d.weekday())).isoformat()  # ISO week's Monday
    return day[:7]  # monthly -> YYYY-MM


def _aggregate_series(series: Series, aggregate: str, summed: bool) -> Series:
    buckets: dict[str, list[float]] = {}
    for day, value in series.items():
        buckets.setdefault(_bucket_label(day, aggregate), []).append(value)
    return {
        label: _round2(sum(vs)) if summed else _round2(_mean(vs))
        for label, vs in buckets.items()
    }


def _describe(values: list[float]) -> dict:
    if not values:
        return {"mean": None, "min": None, "max": None, "sd": None}
    mean = _mean(values)
    return {
        "mean": _round2(mean),
        "min": min(values),
        "max": max(values),
        "sd": _round2(_pstdev(values, mean)),
    }


# --- public API ----------------------------------------------------------------


def query_metrics(
    conn: sqlite3.Connection,
    metrics: list[str],
    start: str,
    end: str,
    aggregate: str = "daily",
    stats: bool = False,
) -> dict:
    """Columnar time series for one or more metrics, optionally aggregated.

    Weekly buckets are ISO weeks labelled by their Monday; monthly buckets are
    YYYY-MM. On aggregation, activities-table metrics (counts, durations,
    distances, load) are summed across the bucket; everything else is averaged.
    """
    if aggregate not in AGGREGATES:
        raise ValueError(f"aggregate must be one of {AGGREGATES}, got {aggregate!r}")
    if not metrics:
        raise ValueError("metrics must be a non-empty list of metric names")
    _validate_range(start, end)
    resolved = [resolve(name) for name in metrics]
    if aggregate != "daily":
        categorical = [m.name for m in resolved if not m.numeric]
        if categorical:
            raise ValueError(
                f"Categorical metrics {categorical} cannot be aggregated {aggregate}; "
                "use aggregate='daily'"
            )

    series = _series_by_metric(conn, resolved, start, end)
    if aggregate != "daily":
        for m in resolved:
            series[m.name] = _aggregate_series(
                series[m.name], aggregate, summed=m.table == "activities"
            )

    labels: list[str] = sorted({day for s in series.values() for day in s})
    rows = [[label, *(series[m.name].get(label) for m in resolved)] for label in labels]
    out = {
        "cols": ["date", *(m.name for m in resolved)],
        "rows": rows,
        "n": len(rows),
        "aggregate": aggregate,
    }
    if stats:
        out["stats"] = {
            m.name: _describe(list(series[m.name].values())) for m in resolved if m.numeric
        }
    return out


def correlate(
    conn: sqlite3.Connection,
    metric_a: str,
    metric_b: str,
    start: str,
    end: str,
    lag_days: int = 0,
    scan_lags: bool = False,
) -> dict:
    """Pearson + Spearman correlation between two metrics over [start, end].

    A positive lag pairs metric_a on day D with metric_b on day D+lag (a leads
    b). scan_lags additionally tries lags -7..+7 and reports the strongest
    |pearson| as best_lag.
    """
    ma, mb = resolve(metric_a), resolve(metric_b)
    _require_numeric(ma, "correlate")
    _require_numeric(mb, "correlate")
    _validate_range(start, end)

    lags = sorted({lag_days, *(_SCAN_LAG_RANGE if scan_lags else ())})
    a_series = _series_by_metric(conn, [ma], start, end)[ma.name]
    b_series = _series_by_metric(
        conn, [mb], _shift(start, min(lags)), _shift(end, max(lags))
    )[mb.name]

    def pairs(lag: int) -> tuple[list[float], list[float]]:
        xs, ys = [], []
        for day, x in sorted(a_series.items()):
            y = b_series.get(_shift(day, lag))
            if y is not None:
                xs.append(x)
                ys.append(y)
        return xs, ys

    xs, ys = pairs(lag_days)
    note = None
    pearson_r = spearman_rho = None
    if len(xs) < _MIN_CORR_N:
        note = f"only {len(xs)} overlapping days; need at least {_MIN_CORR_N}"
    else:
        pearson_r, spearman_rho = _pearson(xs, ys), _spearman(xs, ys)
        if pearson_r is None:
            note = "one of the series is constant over this range"

    best_lag = None
    if scan_lags:
        for lag in _SCAN_LAG_RANGE:
            lxs, lys = pairs(lag)
            if len(lxs) < _MIN_CORR_N:
                continue
            r = _pearson(lxs, lys)
            if r is not None and (best_lag is None or abs(r) > abs(best_lag["r"])):
                best_lag = {"lag": lag, "r": round(r, 3)}

    return {
        "n": len(xs),
        "pearson_r": None if pearson_r is None else round(pearson_r, 3),
        "spearman_rho": None if spearman_rho is None else round(spearman_rho, 3),
        "lag_days": lag_days,
        "best_lag": best_lag,
        "note": note,
    }


def _latest_date(conn: sqlite3.Connection, tables: set[str]) -> str | None:
    latest = [
        conn.execute(f"SELECT MAX(date) FROM {table}").fetchone()[0]  # noqa: S608
        for table in sorted(tables)
    ]
    dates = [d for d in latest if d is not None]
    return max(dates) if dates else None


def baselines(
    conn: sqlite3.Connection,
    metrics: list[str] | None,
    window_days: int,
    end: str | None = None,
) -> dict:
    """Personal mean +/- sd bands per metric over a trailing window.

    The window ends at `end` (default: the latest date with any data for the
    requested metrics); `current` is the most recent value inside the window
    and vs_band says where it sits relative to the band.
    """
    if window_days < 2:
        raise ValueError(f"window_days must be at least 2, got {window_days}")
    names = list(metrics) if metrics else list(DEFAULT_BASELINE_METRICS)
    resolved = [resolve(name) for name in names]
    for m in resolved:
        _require_numeric(m, "baselines")

    if end is None:
        end = _latest_date(conn, {m.table for m in resolved})
    else:
        _parse_date(end, "end")
    empty = {"mean": None, "sd": None, "band": None, "current": None, "vs_band": None, "n": 0}
    if end is None:  # no data at all
        return {"start": None, "end": None, "window_days": window_days,
                "metrics": {m.name: dict(empty) for m in resolved}}

    start = _shift(end, -(window_days - 1))
    series = _series_by_metric(conn, resolved, start, end)
    out: dict[str, dict] = {}
    for m in resolved:
        s = series[m.name]
        if not s:
            out[m.name] = dict(empty)
            continue
        values = [s[day] for day in sorted(s)]
        mean = _mean(values)
        sd = _pstdev(values, mean)
        low, high = _round2(mean - sd), _round2(mean + sd)
        current = values[-1]
        vs_band = "below" if current < low else "above" if current > high else "in"
        out[m.name] = {
            "mean": _round2(mean),
            "sd": _round2(sd),
            "band": [low, high],
            "current": current,
            "vs_band": vs_band,
            "n": len(values),
        }
    return {"start": start, "end": end, "window_days": window_days, "metrics": out}


def anomalies(
    conn: sqlite3.Connection,
    metrics: list[str] | None,
    start: str,
    end: str,
    z: float = 2.0,
) -> dict:
    """Z-score outliers plus sustained streaks against the full-range mean.

    Anomalies are days at least `z` standard deviations from the [start, end]
    mean; streaks are runs of >= 5 consecutive days on the same side of it.
    """
    if z <= 0:
        raise ValueError(f"z must be positive, got {z}")
    names = list(metrics) if metrics else list(DEFAULT_BASELINE_METRICS)
    resolved = [resolve(name) for name in names]
    for m in resolved:
        _require_numeric(m, "anomalies")
    _validate_range(start, end)

    series = _series_by_metric(conn, resolved, start, end)
    found: list[dict] = []
    streaks: list[dict] = []
    for m in resolved:
        s = series[m.name]
        if len(s) < 2:
            continue
        days = sorted(s)
        values = [s[day] for day in days]
        mean = _mean(values)
        sd = _pstdev(values, mean)

        if sd > 0:
            for day, value in zip(days, values, strict=True):
                score = (value - mean) / sd
                if abs(score) >= z:
                    found.append({
                        "date": day,
                        "metric": m.name,
                        "value": value,
                        "z": round(score, 2),
                        "direction": "high" if score > 0 else "low",
                    })

        current: dict | None = None
        prev_day: str | None = None
        for day, value in zip(days, values, strict=True):
            kind = "above_mean" if value > mean else "below_mean" if value < mean else None
            contiguous = prev_day is not None and _shift(prev_day, 1) == day
            if current is not None and kind == current["kind"] and contiguous:
                current["end"] = day
                current["len"] += 1
            else:
                if current is not None and current["len"] >= _MIN_STREAK_LEN:
                    streaks.append(current)
                current = (
                    {"metric": m.name, "kind": kind, "start": day, "end": day, "len": 1}
                    if kind
                    else None
                )
            prev_day = day
        if current is not None and current["len"] >= _MIN_STREAK_LEN:
            streaks.append(current)

    found.sort(key=lambda a: (a["date"], a["metric"]))
    return {"anomalies": found, "streaks": streaks}


def get_day(conn: sqlite3.Connection, date: str) -> dict:
    """One merged, compact view of a single day across every table."""
    _parse_date(date)

    def row_slice(table: str, drop: tuple[str, ...] = ()) -> dict:
        row = conn.execute(
            f"SELECT * FROM {table} WHERE date=?", (date,)  # noqa: S608 - fixed names
        ).fetchone()
        if row is None:
            return {}
        skip = {"date", "fetched_at", *drop}
        return {k: row[k] for k in row.keys() if k not in skip and row[k] is not None}

    sleep = row_slice("sleep", drop=("quality_flags",))
    flags_json = conn.execute(
        "SELECT quality_flags FROM sleep WHERE date=?", (date,)
    ).fetchone()
    flags = json.loads(flags_json[0]) if flags_json and flags_json[0] else []

    activities = [
        {k: row[k] for k in ("activity_id", "name", "type", "duration_s", "distance_m", "avg_hr")}
        for row in conn.execute(
            "SELECT * FROM activities WHERE date=? ORDER BY start_ts, activity_id", (date,)
        )
    ]
    return {
        "date": date,
        "wellness": row_slice("daily_wellness"),
        "sleep": sleep,
        "hrv": row_slice("hrv"),
        "training": row_slice("training_status"),
        "activities": activities,
        "flags": flags,
    }


_ACTIVITY_COLS = (
    "activity_id", "date", "name", "type",
    "duration_s", "distance_m", "elevation_gain_m", "avg_hr",
)


def list_activities(
    conn: sqlite3.Connection,
    type: str | None = None,  # noqa: A002 - mirrors the MCP tool argument name
    start: str | None = None,
    end: str | None = None,
    min_distance_m: float | None = None,
    limit: int = 20,
) -> dict:
    """Recent activities as a columnar table, newest first, with filters."""
    if limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")
    clauses, params = [], []
    if type is not None:
        clauses.append("type = ?")
        params.append(type)
    if start is not None:
        _parse_date(start, "start")
        clauses.append("date >= ?")
        params.append(start)
    if end is not None:
        _parse_date(end, "end")
        clauses.append("date <= ?")
        params.append(end)
    if min_distance_m is not None:
        clauses.append("distance_m >= ?")
        params.append(min_distance_m)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT {', '.join(_ACTIVITY_COLS)} FROM activities{where} "  # noqa: S608
        f"ORDER BY date DESC, start_ts DESC LIMIT ?"
    )
    rows = conn.execute(sql, (*params, limit + 1)).fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    return {
        "cols": list(_ACTIVITY_COLS),
        "rows": [[row[c] for c in _ACTIVITY_COLS] for row in rows],
        "n": len(rows),
        "truncated": truncated,
    }


def gaps(conn: sqlite3.Connection, start: str, end: str) -> dict:
    """Missing days per daily table plus unresolved sync errors in the range."""
    _validate_range(start, end)
    all_days = set(_dates(start, end))
    missing: dict[str, list[str]] = {}
    for table in _DAILY_TABLES:
        have = {
            row[0]
            for row in conn.execute(
                f"SELECT date FROM {table} WHERE date BETWEEN ? AND ?",  # noqa: S608
                (start, end),
            )
        }
        missing[table] = sorted(all_days - have)
    sync_errors = [
        {"endpoint": row["endpoint"], "date": row["date"], "error": row["last_error"]}
        for row in conn.execute(
            "SELECT endpoint, date, last_error FROM sync_state "
            "WHERE status='error' AND date BETWEEN ? AND ? ORDER BY date, endpoint",
            (start, end),
        )
    ]
    return {"start": start, "end": end, "missing": missing, "sync_errors": sync_errors}
