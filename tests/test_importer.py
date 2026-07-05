import pytest

from garmin_mcp import db, importer


@pytest.fixture
def decoded(monkeypatch):
    """Bypass FIT parsing: importer tests exercise the upsert/precedence logic."""
    payload = {
        "date": "2026-06-24",
        "sleep_score": 84,
        "sleep": {"start": "2026-06-23T22:50:00", "end": "2026-06-24T06:09:00",
                  "duration_min": 440},
        "sleep_stages": {"deep_min": 52, "light_min": 219, "rem_min": 92, "awake_min": 74,
                         "max_logging_gap_min": 60},
        "hrv": {"last_night_avg": 33, "weekly_avg": 33, "high_5min": 50,
                "baseline_low": 29, "baseline_high": 35, "status": "balanced"},
        "skin_temp": {"nightly_c": 35.8, "deviation_c": -0.07, "avg_dev_7d": None},
        "heart_rate": {"min": 53, "avg": 74, "max": 166},
        "steps": 4773, "distance_m": 3728.8,
        "stress": {"avg": 28, "max": 99},
        "rhr_on_device": 69,
        "naps": [],
    }
    monkeypatch.setattr(importer, "decode_bundle", lambda folder: payload)
    return payload


def test_import_bundle(tmp_path, decoded):
    conn = db.connect(tmp_path / "t.db")
    report = importer.import_bundle(conn, tmp_path)
    assert report["date"] == "2026-06-24"
    assert set(report["imported"]) == {"sleep", "hrv", "daily_wellness"}
    assert "rhr_far_above_hr_floor" in report["quality_flags"]

    row = conn.execute("SELECT * FROM daily_wellness WHERE date='2026-06-24'").fetchone()
    assert row["resting_hr"] is None  # flagged on-device RHR is withheld
    assert row["min_hr"] == 53
    assert row["source"] == "fit"
    sleep = conn.execute("SELECT * FROM sleep WHERE date='2026-06-24'").fetchone()
    assert sleep["score"] == 84


def test_api_rows_not_overwritten(tmp_path, decoded):
    conn = db.connect(tmp_path / "t.db")
    db.upsert(conn, "sleep", {"date": "2026-06-24", "score": 99, "source": "api"}, ("date",))
    report = importer.import_bundle(conn, tmp_path)
    assert any(s.startswith("sleep") for s in report["skipped"])
    assert conn.execute("SELECT score FROM sleep").fetchone()[0] == 99

    report = importer.import_bundle(conn, tmp_path, force=True)
    assert "sleep" in report["imported"]
    assert conn.execute("SELECT score FROM sleep").fetchone()[0] == 84


def test_missing_date_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "decode_bundle", lambda folder: {"date": None})
    conn = db.connect(tmp_path / "t.db")
    with pytest.raises(ValueError, match="date"):
        importer.import_bundle(conn, tmp_path)
