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
        "performance",
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


def test_migration_v2_adds_fitness_age_columns(tmp_path):
    conn = _connect(tmp_path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(training_status)")}
    assert {"fitness_age", "achievable_fitness_age"} <= cols
    # data written after migration survives a re-open (migrations never re-apply)
    db.upsert(
        conn,
        "training_status",
        {"date": "2026-07-01", "vo2max": 47.3, "fitness_age": 41.23},
        ("date",),
    )
    conn.close()
    conn = db.connect(tmp_path / "test.db")
    row = conn.execute("SELECT * FROM training_status").fetchone()
    assert row["vo2max"] == 47.3 and row["fitness_age"] == 41.23
    assert row["achievable_fitness_age"] is None


def test_migration_v3_adds_performance_table(tmp_path):
    conn = _connect(tmp_path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(performance)")}
    assert {
        "date",
        "endurance_score",
        "endurance_class",
        "hill_score",
        "hill_endurance_score",
        "hill_strength_score",
        "readiness_score",
        "readiness_level",
        "recovery_time_min",
        "race_5k_s",
        "race_10k_s",
        "race_half_s",
        "race_marathon_s",
    } <= cols
    assert db.SCHEMA_VERSION == 3


def test_migration_v3_is_additive_for_an_existing_store(tmp_path):
    """A v2 store gains `performance` on open without losing or altering a row."""
    path = tmp_path / "test.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.executescript(db.MIGRATIONS[1])
        conn.executescript(db.MIGRATIONS[2])
        conn.execute(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, '2026-01-01T00:00:00Z')",
            [(1,), (2,)],
        )
        conn.execute(
            "INSERT INTO daily_wellness (date, resting_hr, steps) VALUES ('2026-01-15', 55, 8200)"
        )
    conn.close()

    conn = db.connect(path)  # applies v3 only
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 3
    row = conn.execute("SELECT * FROM daily_wellness WHERE date='2026-01-15'").fetchone()
    assert row["resting_hr"] == 55 and row["steps"] == 8200
    assert conn.execute("SELECT COUNT(*) FROM performance").fetchone()[0] == 0


def test_performance_partial_upserts_compose(tmp_path):
    """Four endpoints each write their own slice of one performance row."""
    conn = _connect(tmp_path)
    for row in (
        {"date": "2026-01-15", "endurance_score": 7350},
        {"date": "2026-01-15", "hill_score": 42, "hill_strength_score": 38},
        {"date": "2026-01-15", "readiness_score": 71},
        {"date": "2026-01-15", "race_5k_s": 1498},
    ):
        db.upsert_partial(conn, "performance", row, ("date",))
    stored = conn.execute("SELECT * FROM performance WHERE date='2026-01-15'").fetchone()
    assert stored["endurance_score"] == 7350
    assert stored["hill_score"] == 42 and stored["hill_strength_score"] == 38
    assert stored["readiness_score"] == 71
    assert stored["race_5k_s"] == 1498
    assert stored["hill_endurance_score"] is None


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
