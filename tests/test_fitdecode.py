"""Tests for the wellness FIT bundle decoder.

Pure-logic tests run on fabricated decoded dicts. The integration test needs a
real Garmin Connect "Export Wellness Data" daily folder and is skipped unless
the GARMIN_FIT_BUNDLE environment variable points at one (no personal .fit
files are committed to the repo).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from garmin_mcp import fitdecode


def _decoded(**overrides):
    base = {
        "date": "2026-07-02",
        "heart_rate": {"min": 48, "avg": 62, "max": 120, "samples": 900},
        "steps": 9000,
        "distance_m": 7000.0,
        "stress": {"avg": 30, "max": 88},
        "sleep": {
            "start": "2026-07-01T23:10:00",
            "end": "2026-07-02T06:40:00",
            "duration_min": 450,
        },
        "sleep_stages": {
            "deep_min": 80,
            "light_min": 240,
            "rem_min": 90,
            "awake_min": 20,
            "max_logging_gap_min": 10,
        },
        "sleep_score": 86,
        "hrv": {
            "last_night_avg": 52,
            "weekly_avg": 50,
            "high_5min": 68,
            "baseline_low": 45,
            "baseline_high": 60,
            "status": "balanced",
        },
        "skin_temp": {"nightly_c": 33.9, "deviation_c": -0.2, "avg_dev_7d": 0.05},
        "rhr_on_device": 52,
        "naps": [],
    }
    base.update(overrides)
    return base


# --- quality_flags -----------------------------------------------------------


def test_clean_night_has_no_flags():
    assert fitdecode.quality_flags(_decoded()) == []


def test_rhr_flag_fires_only_above_margin():
    hr = {"min": 48, "avg": 62, "max": 120, "samples": 900}
    at_margin = _decoded(heart_rate=hr, rhr_on_device=58)  # exactly min + 10
    above = _decoded(heart_rate=hr, rhr_on_device=59)  # min + 11
    assert "rhr_far_above_hr_floor" not in fitdecode.quality_flags(at_margin)
    assert fitdecode.quality_flags(above) == ["rhr_far_above_hr_floor"]


def test_sparse_stage_flag_threshold():
    stages = dict(_decoded()["sleep_stages"])
    ok = _decoded(sleep_stages={**stages, "max_logging_gap_min": 44})
    sparse = _decoded(sleep_stages={**stages, "max_logging_gap_min": 45})
    assert fitdecode.quality_flags(ok) == []
    assert fitdecode.quality_flags(sparse) == ["sparse_sleep_stage_logging"]


def test_flags_tolerate_missing_sections():
    empty = _decoded(heart_rate=None, sleep_stages=None, rhr_on_device=None)
    assert fitdecode.quality_flags(empty) == []


# --- to_rows -----------------------------------------------------------------


def test_to_rows_clean_mapping():
    rows = fitdecode.to_rows(_decoded())
    assert set(rows) == {"sleep", "hrv", "daily_wellness"}

    sleep = rows["sleep"]
    assert sleep["date"] == "2026-07-02"
    assert sleep["score"] == 86
    assert sleep["duration_min"] == 450
    assert (sleep["deep_min"], sleep["light_min"], sleep["rem_min"], sleep["awake_min"]) == (
        80,
        240,
        90,
        20,
    )
    assert sleep["start_ts"] == "2026-07-01T23:10:00"
    assert sleep["quality_flags"] is None
    assert sleep["nap_min"] is None

    hrv = rows["hrv"]
    assert hrv["last_night_avg"] == 52
    assert hrv["status"] == "balanced"
    assert (hrv["baseline_low"], hrv["baseline_high"]) == (45, 60)

    wellness = rows["daily_wellness"]
    assert wellness["resting_hr"] == 52  # unflagged -> kept
    assert (wellness["min_hr"], wellness["max_hr"]) == (48, 120)
    assert wellness["steps"] == 9000
    assert wellness["distance_m"] == 7000.0
    assert (wellness["stress_avg"], wellness["stress_max"]) == (30, 88)
    assert wellness["skin_temp_dev_c"] == -0.2

    for row in rows.values():
        assert row["source"] == "fit"
        assert row["fetched_at"]


def test_to_rows_drops_flagged_rhr():
    rows = fitdecode.to_rows(_decoded(rhr_on_device=69))  # 21 bpm above the HR floor
    assert rows["daily_wellness"]["resting_hr"] is None
    assert json.loads(rows["sleep"]["quality_flags"]) == ["rhr_far_above_hr_floor"]


def test_to_rows_sums_nap_minutes():
    naps = [
        {"start": "2026-07-02T13:00:00", "end": "2026-07-02T13:25:00", "duration_min": 25},
        {"start": "2026-07-02T17:00:00", "end": "2026-07-02T17:35:00", "duration_min": 35},
    ]
    rows = fitdecode.to_rows(_decoded(naps=naps))
    assert rows["sleep"]["nap_min"] == 60


def test_to_rows_tolerates_empty_decode(tmp_path):
    decoded = fitdecode.decode_bundle(tmp_path / "Garmin 2026-01-01")
    rows = fitdecode.to_rows(decoded)
    assert rows["daily_wellness"]["resting_hr"] is None
    assert rows["sleep"]["score"] is None


# --- decode_bundle -----------------------------------------------------------


def test_decode_bundle_empty_folder(tmp_path):
    folder = tmp_path / "Garmin 2026-01-01"
    folder.mkdir()
    decoded = fitdecode.decode_bundle(folder)
    assert decoded["date"] == "2026-01-01"  # from the folder name
    for key in ("heart_rate", "stress", "sleep", "sleep_stages", "hrv", "skin_temp"):
        assert decoded[key] is None
    assert decoded["sleep_score"] is None
    assert decoded["rhr_on_device"] is None
    assert decoded["naps"] == []
    assert fitdecode.quality_flags(decoded) == []


def test_decode_bundle_skips_unreadable_files(tmp_path):
    folder = tmp_path / "Garmin 2026-01-01"
    folder.mkdir()
    (folder / "junk_WELLNESS.fit").write_bytes(b"not a fit file at all")
    decoded = fitdecode.decode_bundle(folder)
    assert decoded["heart_rate"] is None  # skipped, not raised


# --- integration (opt-in, real data) ------------------------------------------

BUNDLE = os.environ.get("GARMIN_FIT_BUNDLE")


@pytest.mark.skipif(not BUNDLE, reason="GARMIN_FIT_BUNDLE not set")
def test_decode_real_bundle():
    decoded = fitdecode.decode_bundle(Path(BUNDLE))

    assert decoded["date"] is not None
    hr = decoded["heart_rate"]
    assert hr is not None and hr["min"] < hr["max"]
    assert decoded["sleep"] is not None and decoded["sleep"]["duration_min"] > 0
    stages = decoded["sleep_stages"]
    assert stages is not None
    assert stages["deep_min"] + stages["light_min"] + stages["rem_min"] > 0
    if decoded["sleep_score"] is not None:
        assert 0 <= decoded["sleep_score"] <= 100
    if decoded["rhr_on_device"] is not None:
        assert 30 <= decoded["rhr_on_device"] <= 120

    rows = fitdecode.to_rows(decoded)
    assert rows["sleep"]["date"] == decoded["date"]
    assert rows["daily_wellness"]["source"] == "fit"
