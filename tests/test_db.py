import sqlite3

from garmin_mcp import db


def _connect(tmp_path):
    return db.connect(tmp_path / "test.db")


def test_schema_created(tmp_path):
    conn = _connect(tmp_path)
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {
        "schema_version",
        "daily_wellness",
        "sleep",
        "hrv",
        "activities",
        "training_status",
        "raw_snapshots",
        "sync_state",
    } <= tables
    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.SCHEMA_VERSION


def test_migrate_idempotent(tmp_path):
    path = tmp_path / "test.db"
    db.connect(path).close()
    conn = db.connect(path)  # reopen: migrations must not re-apply
    count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    assert count == len(db.MIGRATIONS)


def test_upsert_overwrites_non_key_cols(tmp_path):
    conn = _connect(tmp_path)
    row = {"date": "2026-07-01", "resting_hr": 56, "steps": 5000, "source": "api"}
    db.upsert(conn, "daily_wellness", row, ("date",))
    db.upsert(conn, "daily_wellness", {**row, "resting_hr": 58}, ("date",))
    got = conn.execute("SELECT * FROM daily_wellness").fetchall()
    assert len(got) == 1
    assert got[0]["resting_hr"] == 58
    assert got[0]["steps"] == 5000


def test_source_check_constraint(tmp_path):
    conn = _connect(tmp_path)
    try:
        db.upsert(
            conn, "daily_wellness", {"date": "2026-07-01", "source": "chrome"}, ("date",)
        )
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised


def test_sync_state(tmp_path):
    conn = _connect(tmp_path)
    assert not db.synced_ok(conn, "sleep", "2026-07-01")
    db.mark_sync(conn, "sleep", "2026-07-01", "error", "boom")
    assert not db.synced_ok(conn, "sleep", "2026-07-01")
    db.mark_sync(conn, "sleep", "2026-07-01", "ok")
    assert db.synced_ok(conn, "sleep", "2026-07-01")
    row = conn.execute("SELECT * FROM sync_state").fetchone()
    assert row["attempts"] == 2
    db.mark_sync(conn, "hrv", "2026-07-01", "empty")
    assert db.synced_ok(conn, "hrv", "2026-07-01")
