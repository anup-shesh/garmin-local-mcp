from garmin_mcp import db, raw


def test_write_and_skip(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    raw_dir = tmp_path / "raw"
    p = raw.daily_path(raw_dir, "2026-07-02", "sleep")
    assert p == raw_dir / "daily" / "2026" / "2026-07-02" / "sleep.json"

    assert raw.write_snapshot(conn, raw_dir, p, {"score": 86}, "2026-07-02", "sleep")
    assert raw.read_snapshot(p) == {"score": 86}

    # immutability: second write is skipped, content unchanged
    assert not raw.write_snapshot(conn, raw_dir, p, {"score": 0}, "2026-07-02", "sleep")
    assert raw.read_snapshot(p) == {"score": 86}

    row = conn.execute("SELECT * FROM raw_snapshots").fetchone()
    assert row["path"] == "daily/2026/2026-07-02/sleep.json"
    assert row["endpoint"] == "sleep"
    assert row["bytes"] > 0


def test_iter_daily_snapshots(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    raw_dir = tmp_path / "raw"
    for date, ep in [("2026-07-02", "sleep"), ("2026-07-01", "hrv"), ("2026-07-01", "sleep")]:
        raw.write_snapshot(conn, raw_dir, raw.daily_path(raw_dir, date, ep), {}, date, ep)
    got = [(d, e) for d, e, _ in raw.iter_daily_snapshots(raw_dir)]
    assert got == [("2026-07-01", "hrv"), ("2026-07-01", "sleep"), ("2026-07-02", "sleep")]


def test_iter_empty(tmp_path):
    assert list(raw.iter_daily_snapshots(tmp_path / "raw")) == []
