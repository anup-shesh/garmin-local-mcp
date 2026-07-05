"""Immutable raw JSON snapshot layer.

Layout:
    <data_dir>/raw/daily/YYYY/YYYY-MM-DD/<endpoint>.json
    <data_dir>/raw/activities/<activity_id>.json

Snapshots are the source of truth and are never overwritten (existing files are
skipped), so history survives API breakage and parser bugs alike.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from .db import upsert, utcnow


def daily_path(raw_dir: Path, date: str, endpoint: str) -> Path:
    return raw_dir / "daily" / date[:4] / date / f"{endpoint}.json"


def activity_path(raw_dir: Path, activity_id: int) -> Path:
    return raw_dir / "activities" / f"{activity_id}.json"


def write_snapshot(
    conn: sqlite3.Connection,
    raw_dir: Path,
    path: Path,
    payload: object,
    date: str | None,
    endpoint: str,
) -> bool:
    """Write one snapshot and index it. Returns False (skip) if it already exists."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=1, sort_keys=True).encode("utf-8")
    path.write_bytes(data)
    upsert(
        conn,
        "raw_snapshots",
        {
            "path": str(path.relative_to(raw_dir)).replace("\\", "/"),
            "date": date,
            "endpoint": endpoint,
            "fetched_at": utcnow(),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        ("path",),
    )
    return True


def read_snapshot(path: Path) -> object:
    with path.open("rb") as f:
        return json.load(f)


def iter_daily_snapshots(raw_dir: Path) -> Iterator[tuple[str, str, Path]]:
    """Yield (date, endpoint, path) for every daily snapshot on disk, sorted by date."""
    daily = raw_dir / "daily"
    if not daily.is_dir():
        return
    for day_dir in sorted(daily.glob("*/????-??-??")):
        for f in sorted(day_dir.glob("*.json")):
            yield day_dir.name, f.stem, f
