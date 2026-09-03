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
    assert list(ENDPOINTS) == [
        "usersummary", "sleep", "hrv", "training_status", "fitnessage",
        "endurance_score", "hill_score", "training_readiness", "race_predictions",
        "activities",
    ]
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


def test_parse_fitnessage():
    # partial training_status contribution, floats rounded to 2 decimals
    assert parse("fitnessage", load("fitnessage")) == [
        (
            "training_status",
            {
                "date": DATE,
                "fitness_age": 41.23,
                "achievable_fitness_age": 40.99,
            },
        )
    ]


def test_parse_fitnessage_missing_keys():
    [(table, row)] = parse("fitnessage", {"fitnessAge": 41.5})
    assert table == "training_status"
    assert row["fitness_age"] == 41.5
    assert row["achievable_fitness_age"] is None


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


def test_parse_endurance_score():
    """`classification` is an opaque int; the tier is derived from the ladder."""
    assert parse("endurance_score", load("endurance_score")) == [
        ("performance", {"date": DATE, "endurance_score": 7350, "endurance_class": "expert"})
    ]


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (4900, "below_intermediate"),  # under the lowest rung, not dropped
        (5100, "intermediate"),  # exactly on a boundary counts as that tier
        (6700, "well_trained"),  # CamelCase tier names become snake_case
        (8800, "elite"),
        (12000, "elite"),  # above the top rung stays at the top tier
    ],
)
def test_endurance_class_is_derived_from_the_threshold_ladder(score, expected):
    payload = load("endurance_score") | {"overallScore": score}
    [(_, row)] = parse("endurance_score", payload)
    assert row["endurance_class"] == expected


def test_endurance_class_falls_back_to_a_string_classification():
    """No ladder in the payload: use `classification` if it is a plain string."""
    payload = {"calendarDate": DATE, "overallScore": 7350, "classification": "EXPERT"}
    [(_, row)] = parse("endurance_score", payload)
    assert row["endurance_class"] == "expert"


def test_parse_hill_score():
    assert parse("hill_score", load("hill_score")) == [
        (
            "performance",
            {
                "date": DATE,
                "hill_score": 30,
                "hill_endurance_score": 18,
                "hill_strength_score": 4,
            },
        )
    ]


def test_parse_training_readiness_prefers_the_post_wake_snapshot():
    """The list holds a later scheduled update; the morning reading is the score."""
    assert parse("training_readiness", load("training_readiness")) == [
        (
            "performance",
            {
                "date": DATE,
                "readiness_score": 71,
                "readiness_level": "high",
                "recovery_time_min": 90,
            },
        )
    ]


def test_parse_training_readiness_falls_back_to_first_entry():
    """Firmware that never sets inputContext still yields a row."""
    payload = [{"calendarDate": DATE, "score": 44, "level": "LOW", "recoveryTime": 600}]
    [(_, row)] = parse("training_readiness", payload)
    assert row["readiness_score"] == 44 and row["recovery_time_min"] == 600


def test_parse_race_predictions_picks_the_requested_date():
    """The daily range form returns neighbouring days too; only DATE is stored."""
    assert parse("race_predictions", load("race_predictions")) == [
        (
            "performance",
            {
                "date": DATE,
                "race_5k_s": 1498,
                "race_10k_s": 3145,
                "race_half_s": 6972,
                "race_marathon_s": 14655,
            },
        )
    ]


@pytest.mark.parametrize(
    ("name", "payload", "expected"),
    [
        # Alternate spellings, because these three payload shapes are inferred
        # rather than verified - see the provenance note in endpoints.py.
        ("endurance_score", {"enduranceScore": 7350}, ("endurance_score", 7350)),
        ("hill_score", {"hillScore": 42}, ("hill_score", 42)),
        ("race_predictions", {"raceTime5K": 1498}, ("race_5k_s", 1498)),
    ],
)
def test_parse_accepts_alternate_key_spellings(name, payload, expected):
    [(table, row)] = parse(name, payload)
    assert table == "performance"
    assert row[expected[0]] == expected[1]


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("endurance_score", {"calendarDate": "2025-12-01", "overallScore": 7350}),
        ("hill_score", {"calendarDate": "2025-12-01", "overallScore": 42}),
        ("training_readiness", [{"calendarDate": "2025-12-01", "score": 71}]),
    ],
)
def test_parse_rejects_a_payload_stamped_with_another_date(name, payload):
    """metrics-service can answer an out-of-range date with the latest reading."""
    assert parse(name, payload) == []


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("usersummary", None),
        ("endurance_score", None),
        ("endurance_score", {}),
        ("endurance_score", {"calendarDate": DATE}),  # stamped, but no score
        ("hill_score", None),
        ("hill_score", {"userProfilePK": 1234567}),
        ("training_readiness", None),
        ("training_readiness", []),
        ("training_readiness", [{"calendarDate": DATE}]),
        ("race_predictions", None),
        ("race_predictions", []),
        ("race_predictions", [{"calendarDate": "2025-12-01", "time5K": 1512}]),
        ("usersummary", {"privacyProtected": None}),
        ("sleep", None),
        ("sleep", {"dailySleepDTO": {"id": None, "calendarDate": None}}),
        ("hrv", None),
        ("hrv", {"userProfilePk": 100000001}),  # no hrvSummary
        ("training_status", None),
        ("training_status", {"mostRecentTrainingStatus": {"latestTrainingStatusData": {}}}),
        ("fitnessage", None),
        ("fitnessage", {"chronologicalAge": 45, "components": {}}),
        ("activities", None),
        ("activities", []),
    ],
)
def test_parse_empty_payloads(name, payload):
    assert parse(name, payload) == []
