"""Sync engine tests: offline, driven by a fake client (no garminconnect import)."""

import json
from pathlib import Path

import pytest

from garmin_mcp import db, raw, sync
from garmin_mcp.auth import AuthError
from garmin_mcp.config import Config

FIXTURES = Path(__file__).parent / "fixtures"

DAY1 = "2026-01-15"
DAY2 = "2026-01-16"

DATA_TABLES = ("daily_wellness", "sleep", "hrv", "training_status", "activities")


def load(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeClient:
    """Returns fixture payloads on DAY1 and empty payloads on other days."""

    def __init__(self):
        self.calls = []

    def _payload(self, endpoint, date, empty):
        self.calls.append((endpoint, date))
        return load(endpoint) if date == DAY1 else empty

    def get_stats(self, date):
        return self._payload("usersummary", date, {})

    def get_sleep_data(self, date):
        return self._payload("sleep", date, {})

    def get_hrv_data(self, date):
        return self._payload("hrv", date, None)

    def get_training_status(self, date):
        return self._payload("training_status", date, None)

    def get_activities_by_date(self, start, end):
        return self._payload("activities", start, [])


class GarminConnectTooManyRequestsError(Exception):
    """Same class *name* as garminconnect's - sync detects it without the import."""


class RateLimitedClient(FakeClient):
    def get_stats(self, date):
        raise GarminConnectTooManyRequestsError("Too many requests (429)")


@pytest.fixture
def config(tmp_path):
    cfg = Config(
        data_dir=tmp_path,
        timezone="America/New_York",
        units="metric",
        request_delay_seconds=0.0,
        baseline_window_days=28,
    )
    cfg.ensure_dirs()
    return cfg


def quiet(_msg):
    pass


def run(config, conn, client, **kwargs):
    kwargs.setdefault("progress", quiet)
    return sync.sync_range(config, conn, client, DAY1, DAY2, **kwargs)


def dump_db(conn):
    """Everything reparse must reproduce, minus volatile timestamps/counters."""
    out = {}
    for table in DATA_TABLES:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 - fixed names
        out[table] = sorted(
            tuple((k, r[k]) for k in r.keys() if k != "fetched_at") for r in rows
        )
    out["sync_state"] = sorted(
        tuple(r) for r in conn.execute("SELECT endpoint, date, status FROM sync_state")
    )
    out["raw_snapshots"] = sorted(
        tuple(r)
        for r in conn.execute("SELECT path, date, endpoint, bytes, sha256 FROM raw_snapshots")
    )
    return out


def test_sync_range_rows_land(config):
    conn = db.connect(config.db_path)
    client = FakeClient()
    report = run(config, conn, client)

    wellness = conn.execute("SELECT * FROM daily_wellness WHERE date=?", (DAY1,)).fetchone()
    assert wellness["resting_hr"] == 55
    assert wellness["skin_temp_dev_c"] == -0.3  # sleep's partial merged in, nothing clobbered
    assert wellness["source"] == "api"
    assert conn.execute("SELECT score FROM sleep WHERE date=?", (DAY1,)).fetchone()[0] == 82
    assert conn.execute("SELECT status FROM hrv WHERE date=?", (DAY1,)).fetchone()[0] == "balanced"
    activity = conn.execute("SELECT * FROM activities").fetchone()
    assert activity["activity_id"] == 90000000001
    assert activity["raw_path"] == "activities/90000000001.json"

    # snapshots on disk, including the per-activity one
    assert raw.daily_path(config.raw_dir, DAY1, "sleep").is_file()
    assert raw.daily_path(config.raw_dir, DAY2, "hrv").is_file()  # "null" is still history
    assert raw.activity_path(config.raw_dir, 90000000001).is_file()

    # sync_state: ok on the full day, empty where the API had nothing
    assert db.synced_ok(conn, "usersummary", DAY1)
    state = {
        (r["endpoint"], r["date"]): r["status"]
        for r in conn.execute("SELECT * FROM sync_state")
    }
    assert state[("sleep", DAY1)] == "ok"
    assert state[("hrv", DAY2)] == "empty"
    assert state[("activities", DAY2)] == "empty"

    assert report["aborted"] is None and not report["resumable"]
    assert report["requests"] == 10  # 5 endpoints x 2 days
    assert report["endpoints"]["usersummary"] == {"ok": 1, "empty": 1, "skipped": 0, "error": 0}


def test_rerun_is_idempotent(config):
    conn = db.connect(config.db_path)
    client = FakeClient()
    run(config, conn, client)
    calls = len(client.calls)
    snapshot = raw.daily_path(config.raw_dir, DAY1, "sleep")
    content = snapshot.read_bytes()

    report = run(config, conn, client)
    assert len(client.calls) == calls  # no new fetches
    assert report["requests"] == 0
    assert all(s == {"ok": 0, "empty": 0, "skipped": 2, "error": 0}
               for s in report["endpoints"].values())
    assert snapshot.read_bytes() == content

    # even with sync_state cleared, existing snapshots are never overwritten
    with conn:
        conn.execute("DELETE FROM sync_state")
    run(config, conn, client)
    assert snapshot.read_bytes() == content


def test_rate_limit_backs_off_then_aborts(config, monkeypatch):
    sleeps = []
    monkeypatch.setattr(sync, "_sleep", sleeps.append)
    conn = db.connect(config.db_path)
    report = run(config, conn, RateLimitedClient())

    assert sleeps == [30, 120, 600]
    assert report["aborted"] == "rate_limited"
    assert report["resumable"] is True
    assert report["requests"] == 0
    row = conn.execute(
        "SELECT status, last_error FROM sync_state WHERE endpoint='usersummary' AND date=?",
        (DAY1,),
    ).fetchone()
    assert row["status"] == "error" and "rate limited" in row["last_error"]
    # aborted before any other endpoint was attempted
    assert conn.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0] == 1


def test_auth_error_aborts_with_hint(config):
    class AuthFailClient(FakeClient):
        def get_stats(self, date):
            raise AuthError("Stored tokens were rejected")

    conn = db.connect(config.db_path)
    report = run(config, conn, AuthFailClient())
    assert report["aborted"] == "auth"
    assert report["resumable"] is True
    assert "login" in report["auth_error"]["hint"]


def test_endpoint_error_continues(config):
    class FlakyClient(FakeClient):
        def get_sleep_data(self, date):
            raise RuntimeError("boom")

    conn = db.connect(config.db_path)
    report = run(config, conn, FlakyClient())
    assert report["aborted"] is None
    assert report["endpoints"]["sleep"] == {"ok": 0, "empty": 0, "skipped": 0, "error": 2}
    assert report["endpoints"]["hrv"]["ok"] == 1  # later endpoints still ran
    row = conn.execute(
        "SELECT last_error FROM sync_state WHERE endpoint='sleep' AND date=?", (DAY1,)
    ).fetchone()
    assert "boom" in row["last_error"]


def test_partial_upsert_survives_either_order(config):
    conn = db.connect(config.db_path)
    client = FakeClient()
    run(config, conn, client, endpoints=["sleep"])
    row = conn.execute("SELECT * FROM daily_wellness WHERE date=?", (DAY1,)).fetchone()
    assert row["skin_temp_dev_c"] == -0.3 and row["resting_hr"] is None

    run(config, conn, client, endpoints=["usersummary"])
    row = conn.execute("SELECT * FROM daily_wellness WHERE date=?", (DAY1,)).fetchone()
    assert row["skin_temp_dev_c"] == -0.3  # untouched by the later usersummary upsert
    assert row["resting_hr"] == 55


def test_throttle_between_requests(config, monkeypatch):
    sleeps = []
    monkeypatch.setattr(sync, "_sleep", sleeps.append)
    delayed = Config(
        data_dir=config.data_dir,
        timezone=config.timezone,
        units=config.units,
        request_delay_seconds=1.5,
        baseline_window_days=28,
    )
    conn = db.connect(delayed.db_path)
    run(delayed, conn, FakeClient())
    assert sleeps == [1.5] * 9  # between requests only, not before the first


def test_reparse_rebuilds_identical_db(config):
    conn = db.connect(config.db_path)
    run(config, conn, FakeClient())
    expected = dump_db(conn)
    conn.close()

    for suffix in ("", "-wal", "-shm"):
        Path(f"{config.db_path}{suffix}").unlink(missing_ok=True)

    conn = db.connect(config.db_path)
    report = sync.reparse(config, conn, progress=quiet)
    assert report["daily_snapshots"] == 10
    assert report["activity_snapshots"] == 1
    assert dump_db(conn) == expected


def test_yesterday_and_default_range(config):
    end = sync.yesterday(config)
    start, end2 = sync.default_range(config)
    assert end2 == end
    assert len(start) == 10 and start < end


def test_unknown_endpoint_rejected(config):
    conn = db.connect(config.db_path)
    with pytest.raises(ValueError, match="Unknown endpoints"):
        run(config, conn, FakeClient(), endpoints=["body_battery"])
