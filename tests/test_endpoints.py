"""Parse functions map fixture payloads (fabricated, real-shaped) to exact rows."""

import json
from pathlib import Path

import pytest

from garmin_mcp.endpoints import ENDPOINTS

FIXTURES = Path(__file__).parent / "fixtures"

DATE = "2026-01-15"


def load(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def parse(name: str, payload, date: str = DATE):
    return ENDPOINTS[name].parse(payload, date)


def test_registry():
    assert list(ENDPOINTS) == ["usersummary", "sleep", "hrv", "training_status", "activities"]
    for name, endpoint in ENDPOINTS.items():
        assert endpoint.name == name


def test_parse_usersummary():
    assert parse("usersummary", load("usersummary")) == [
        (
            "daily_wellness",
            {
                "date": DATE,
                "resting_hr": 55,
                "min_hr": 48,
                "max_hr": 152,
                "steps": 8200,
                "distance_m": 6400,
                "floors_up": 13,
                "stress_avg": 27,
                "stress_max": 91,
                "body_battery_high": 88,
                "body_battery_low": 24,
                "calories_total": 2100,
                "calories_active": 350,
                "spo2_avg": 96.0,
                "respiration_avg": 14.0,
                "intensity_min_moderate": 21,
                "intensity_min_vigorous": 8,
                "source": "api",
            },
        )
    ]


def test_parse_sleep():
    rows = parse("sleep", load("sleep"))
    assert rows == [
        (
            "sleep",
            {
                "date": DATE,
                "score": 82,
                "duration_min": 440.0,
                "deep_min": 91,
                "light_min": 246,
                "rem_min": 93,
                "awake_min": 22,
                "start_ts": "2026-01-14T22:45:00",
                "end_ts": "2026-01-15T06:05:00",
                "avg_spo2": 95.0,
                "avg_respiration": 15.0,
                "avg_stress": 21,
                "restless_moments": 31,
                "nap_min": 20,
                "quality_flags": None,
                "source": "api",
            },
        ),
        # partial daily_wellness contribution: only the skin-temp column
        ("daily_wellness", {"date": DATE, "skin_temp_dev_c": -0.3}),
    ]


def test_parse_sleep_sparse_dto():
    """Old days lack sleepScores, restlessMomentsCount, SpO2 - all become None."""
    payload = {
        "dailySleepDTO": {"calendarDate": DATE, "sleepTimeSeconds": 13920,
                          "deepSleepSeconds": 3600},
        "skinTempDataExists": False,
    }
    [(table, row)] = parse("sleep", payload)
    assert (table, row["score"], row["duration_min"], row["deep_min"]) == (
        "sleep", None, 232.0, 60)
    assert row["restless_moments"] is None and row["start_ts"] is None


def test_parse_hrv():
    assert parse("hrv", load("hrv")) == [
        (
            "hrv",
            {
                "date": DATE,
                "last_night_avg": 39,
                "weekly_avg": 41,
                "high_5min": 58,
                "status": "balanced",
                "baseline_low": 34,
                "baseline_high": 41,
                "source": "api",
            },
        )
    ]


def test_parse_training_status():
    assert parse("training_status", load("training_status")) == [
        (
            "training_status",
            {
                "date": DATE,
                "status": "productive_1",
                "vo2max": 47.3,
                "acute_load": 187,
                "load_ratio": 1.1,
            },
        )
    ]


def test_parse_activities():
    assert parse("activities", load("activities")) == [
        (
            "activities",
            {
                "activity_id": 90000000001,
                "date": DATE,
                "start_ts": "2026-01-15T07:02:11",
                "name": "Morning Run",
                "type": "running",
                "duration_s": 1500.0,
                "distance_m": 5000.0,
                "elevation_gain_m": 42.0,
                "avg_hr": 149,
                "max_hr": 171,
                "calories": 388,
                "avg_pace_s_per_km": 300.0,
                "training_load": 74.5,
                "aerobic_te": 3.1,
                "anaerobic_te": 0.2,
                "raw_path": "activities/90000000001.json",
            },
        )
    ]


def test_parse_activities_no_distance():
    """Zero-distance activities (strength etc.) get no pace, and never divide by 0."""
    [(_, row)] = parse("activities", [{"activityId": 7, "duration": 1800.0, "distance": 0.0}])
    assert row["avg_pace_s_per_km"] is None
    assert row["date"] == DATE  # falls back to the sync date without startTimeLocal


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("usersummary", None),
        ("usersummary", {"privacyProtected": None}),
        ("sleep", None),
        ("sleep", {"dailySleepDTO": {"id": None, "calendarDate": None}}),
        ("hrv", None),
        ("hrv", {"userProfilePk": 100000001}),  # no hrvSummary
        ("training_status", None),
        ("training_status", {"mostRecentTrainingStatus": {"latestTrainingStatusData": {}}}),
        ("activities", None),
        ("activities", []),
    ],
)
def test_parse_empty_payloads(name, payload):
    assert parse(name, payload) == []
