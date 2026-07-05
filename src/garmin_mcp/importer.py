"""Offline ingest of decoded FIT bundles into the warehouse.

FIT-sourced rows are the fallback layer: they fill days the API hasn't (or
can't) provide, but an existing API-sourced row is authoritative and is never
overwritten unless the caller forces it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .db import upsert
from .fitdecode import decode_bundle, to_rows


def import_bundle(conn: sqlite3.Connection, folder: Path, force: bool = False) -> dict:
    """Decode one export bundle and upsert its rows. Returns an import report."""
    decoded = decode_bundle(Path(folder))
    date = decoded.get("date")
    if not date:
        raise ValueError(
            f"Could not determine the bundle's date from {folder} "
            "(no timestamps decoded and no YYYY-MM-DD in the folder name)"
        )

    rows = to_rows(decoded)
    report: dict = {"date": date, "imported": [], "skipped": [], "quality_flags": []}
    for table, row in rows.items():
        if row is None or all(v is None for k, v in row.items() if k not in ("date", "source")):
            continue
        existing = conn.execute(
            f"SELECT source FROM {table} WHERE date=?", (date,)  # noqa: S608 - fixed table names
        ).fetchone()
        if existing and existing["source"] == "api" and not force:
            report["skipped"].append(f"{table} (api row exists; use force to overwrite)")
            continue
        upsert(conn, table, row, ("date",))
        report["imported"].append(table)

    sleep_row = rows.get("sleep") or {}
    if sleep_row.get("quality_flags"):
        report["quality_flags"] = json.loads(sleep_row["quality_flags"])
    return report
