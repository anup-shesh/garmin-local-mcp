"""Analysis layer tests over a deterministic synthetic warehouse.

30 days (2026-06-01 .. 2026-06-30, June 1st is a Monday) seeded by formula:

- resting_hr: 55 + i%5, except a planted spike (80) on i=20 and a planted
  low streak (53) on i=5..10 (the run of below-mean days extends to i=12).
- steps: 8000 + 100*i (rising linearly, so the latest value sits above band).
- stress_avg (correlation input a): (i*7) % 23 + 20.
- sleep: skipped entirely on i=14 (the planted gap); score 80 + i%3;
  duration_min (correlation input b) = 2*a[i-1] + i%2, i.e. b lags a by one
  day; quality_flags planted on i=1.
- hrv: 60 + i%4, status always "balanced".
- 4 activities across 3 days (two on day one).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from garmin_mcp import analysis, db

BASE = date(2026, 6, 1)
DAYS = 30
GAP_I = 14  # sleep row deliberately missing
SPIKE_I = 20  # planted resting_hr anomaly


def day(i: int) -> str:
    return (BASE + timedelta(days=i)).isoformat()


def stress(i: int) -> int:
    return (i * 7) % 23 + 20


def resting_hr(i: int) -> int:
    if i == SPIKE_I:
        return 80
    if 5 <= i <= 10:
        return 53
    return 55 + i % 5


def seed(conn) -> None:
    for i in range(DAYS):
        d = day(i)
        db.upsert(
            conn,
            "daily_wellness",
            {
                "date": d,
                "resting_hr": resting_hr(i),
                "steps": 8000 + 100 * i,
                "stress_avg": stress(i),
                "source": "api",
            },
            ("date",),
        )
        if i != GAP_I:
            db.upsert(
                conn,
                "sleep",
                {
                    "date": d,
                    "score": 80 + i % 3,
                    "duration_min": 100.0 if i == 0 else 2 * stress(i - 1) + i % 2,
                    "quality_flags": '["short_sleep"]' if i == 1 else None,
                    "source": "api",
                },
                ("date",),
            )
        db.upsert(
            conn,
            "hrv",
            {"date": d, "last_night_avg": 60 + i % 4, "status": "balanced", "source": "api"},
            ("date",),
        )

    activities = [
        (1, day(0), "2026-06-01T07:00:00", "Morning Run", "running", 1800.0, 5000.0, 50.0, 150),
        (2, day(0), "2026-06-01T09:00:00", "Ride", "cycling", 3600.0, 20000.0, 120.0, 130),
        (3, day(2), "2026-06-03T07:00:00", "Long Run", "running", 3600.0, 10000.0, 80.0, 155),
        (4, day(8), "2026-06-09T07:00:00", "Tempo Run", "running", 2700.0, 8000.0, 60.0, 160),
    ]
    for aid, d, ts, name, typ, dur, dist, elev, hr in activities:
        db.upsert(
            conn,
            "activities",
            {
                "activity_id": aid, "date": d, "start_ts": ts, "name": name, "type": typ,
                "duration_s": dur, "distance_m": dist, "elevation_gain_m": elev, "avg_hr": hr,
            },
            ("activity_id",),
        )
    db.mark_sync(conn, "sleep", day(GAP_I), "error", "boom")


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "garmin.db")
    seed(conn)
    yield conn
    conn.close()


# --- query_metrics -----------------------------------------------------------


def test_query_metrics_daily(conn):
    res = analysis.query_metrics(conn, ["resting_hr", "sleep_score"], day(0), day(6))
    assert res["cols"] == ["date", "resting_hr", "sleep_score"]
    assert res["aggregate"] == "daily"
    assert res["n"] == 7 and len(res["rows"]) == 7
    assert res["rows"][0] == [day(0), 55, 80]
    assert res["rows"][6] == [day(6), 53, 80]
    assert "stats" not in res


def test_query_metrics_daily_null_join(conn):
    # sleep is missing on the gap day; the joined row keeps a null cell
    res = analysis.query_metrics(conn, ["resting_hr", "sleep_score"], day(GAP_I), day(GAP_I))
    assert res["rows"] == [[day(GAP_I), resting_hr(GAP_I), None]]


def test_query_metrics_weekly_averages(conn):
    res = analysis.query_metrics(
        conn, ["resting_hr", "sleep_score"], day(0), day(13), aggregate="weekly"
    )
    # 2026-06-01 is a Monday, so weeks are labelled by it and 2026-06-08
    assert [r[0] for r in res["rows"]] == ["2026-06-01", "2026-06-08"]
    assert res["rows"][0][1] == round((55 + 56 + 57 + 58 + 59 + 53 + 53) / 7, 2)  # 55.86
    assert res["rows"][0][2] == round((80 + 81 + 82 + 80 + 81 + 82 + 80) / 7, 2)  # 80.86
    assert res["rows"][1][1] == round((53 + 53 + 53 + 53 + 56 + 57 + 58) / 7, 2)  # 54.71


def test_query_metrics_weekly_sums_activity_metrics(conn):
    res = analysis.query_metrics(
        conn, ["activity_count", "activity_distance_m"], day(0), day(13), aggregate="weekly"
    )
    rows = {r[0]: r[1:] for r in res["rows"]}
    assert rows["2026-06-01"] == [3, 35000.0]  # 2 activities on day 0 + 1 on day 2
    assert rows["2026-06-08"] == [1, 8000.0]


def test_query_metrics_monthly(conn):
    res = analysis.query_metrics(conn, ["steps"], day(0), day(29), aggregate="monthly")
    assert res["rows"] == [["2026-06", round(sum(8000 + 100 * i for i in range(30)) / 30, 2)]]


def test_query_metrics_stats_block(conn):
    res = analysis.query_metrics(conn, ["resting_hr"], day(0), day(29), stats=True)
    stats = res["stats"]["resting_hr"]
    assert stats["min"] == 53 and stats["max"] == 80
    assert stats["mean"] == 57.1  # sum is exactly 1713 over 30 days
    assert stats["sd"] > 0


def test_query_metrics_categorical_daily_ok_but_not_aggregable(conn):
    res = analysis.query_metrics(conn, ["hrv_status"], day(0), day(2))
    assert res["rows"][0] == [day(0), "balanced"]
    with pytest.raises(ValueError, match="[Cc]ategorical"):
        analysis.query_metrics(conn, ["hrv_status"], day(0), day(13), aggregate="weekly")


def test_query_metrics_rejects_bad_input(conn):
    with pytest.raises(ValueError, match="Unknown metric"):
        analysis.query_metrics(conn, ["nope"], day(0), day(5))
    with pytest.raises(ValueError, match="aggregate"):
        analysis.query_metrics(conn, ["steps"], day(0), day(5), aggregate="hourly")
    with pytest.raises(ValueError, match="after"):
        analysis.query_metrics(conn, ["steps"], day(5), day(0))
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        analysis.query_metrics(conn, ["steps"], "June 1st", day(5))


# --- correlate ----------------------------------------------------------------


def test_correlate_strong_positive_at_lag_1(conn):
    # sleep_duration_min on day D+1 is 2*stress_avg(D) + tiny wobble
    res = analysis.correlate(
        conn, "stress_avg", "sleep_duration_min", day(0), day(29), lag_days=1
    )
    assert res["n"] == 28  # 29 lead days minus the planted sleep gap
    assert res["pearson_r"] > 0.95
    assert res["spearman_rho"] > 0.95
    assert res["lag_days"] == 1
    assert res["note"] is None and res["best_lag"] is None


def test_correlate_scan_lags_finds_the_planted_lag(conn):
    res = analysis.correlate(
        conn, "stress_avg", "sleep_duration_min", day(0), day(29), scan_lags=True
    )
    assert res["best_lag"]["lag"] == 1
    assert res["best_lag"]["r"] > 0.95
    # at lag 0 the relationship is much weaker than at the true lag
    assert abs(res["pearson_r"]) < res["best_lag"]["r"]


def test_correlate_too_few_points(conn):
    res = analysis.correlate(conn, "stress_avg", "sleep_duration_min", day(0), day(2))
    assert res["pearson_r"] is None and res["spearman_rho"] is None
    assert "need at least 5" in res["note"]


def test_correlate_refuses_categorical(conn):
    with pytest.raises(ValueError, match="categorical"):
        analysis.correlate(conn, "hrv_status", "resting_hr", day(0), day(29))


# --- baselines ------------------------------------------------------------------


def test_baselines_bands_and_position(conn):
    res = analysis.baselines(conn, None, window_days=28, end=day(29))
    assert res["start"] == day(2) and res["end"] == day(29)

    rhr = res["metrics"]["resting_hr"]
    assert rhr["n"] == 28
    assert rhr["band"][0] < rhr["mean"] < rhr["band"][1]
    assert rhr["current"] == 59  # last seeded value, inside the band
    assert rhr["vs_band"] == "in"

    steps = res["metrics"]["steps"]
    assert steps["current"] == 8000 + 100 * 29
    assert steps["vs_band"] == "above"  # linear rise puts the latest above mean+sd

    # never seeded -> an explicit empty entry, not a crash
    skin = res["metrics"]["skin_temp_dev_c"]
    assert skin == {"mean": None, "sd": None, "band": None, "current": None,
                    "vs_band": None, "n": 0}


def test_baselines_defaults_end_to_latest_data(conn):
    res = analysis.baselines(conn, ["resting_hr"], window_days=7)
    assert res["end"] == day(29)
    assert res["metrics"]["resting_hr"]["n"] == 7


def test_baselines_refuses_categorical(conn):
    with pytest.raises(ValueError, match="categorical"):
        analysis.baselines(conn, ["hrv_status"], window_days=28)


# --- anomalies ------------------------------------------------------------------


def test_anomalies_catches_planted_spike_and_streak(conn):
    res = analysis.anomalies(conn, ["resting_hr"], day(0), day(29), z=2.0)
    assert res["anomalies"] == [
        {"date": day(SPIKE_I), "metric": "resting_hr", "value": 80,
         "z": res["anomalies"][0]["z"], "direction": "high"}
    ]
    assert res["anomalies"][0]["z"] > 2
    # low days i=5..10 (53) plus i=11..12 (56, 57) all sit below the 57.1 mean
    assert res["streaks"] == [
        {"metric": "resting_hr", "kind": "below_mean", "start": day(5), "end": day(12), "len": 8}
    ]


def test_anomalies_flat_metric_yields_nothing(conn):
    # hrv cycles tightly around its mean: no |z| >= 2 days, no 5-day streaks
    res = analysis.anomalies(conn, ["hrv"], day(0), day(29))
    assert res["anomalies"] == [] and res["streaks"] == []


# --- get_day ---------------------------------------------------------------------


def test_get_day_merges_all_tables(conn):
    res = analysis.get_day(conn, day(0))
    assert res["date"] == day(0)
    assert res["wellness"]["resting_hr"] == 55 and res["wellness"]["steps"] == 8000
    assert res["sleep"]["score"] == 80 and res["sleep"]["duration_min"] == 100.0
    assert "quality_flags" not in res["sleep"]
    assert res["hrv"] == {"last_night_avg": 60, "status": "balanced", "source": "api"}
    assert res["training"] == {}
    assert [a["activity_id"] for a in res["activities"]] == [1, 2]
    assert res["activities"][0] == {
        "activity_id": 1, "name": "Morning Run", "type": "running",
        "duration_s": 1800.0, "distance_m": 5000.0, "avg_hr": 150,
    }
    assert res["flags"] == []


def test_get_day_quality_flags_and_empty_day(conn):
    assert analysis.get_day(conn, day(1))["flags"] == ["short_sleep"]
    empty = analysis.get_day(conn, "2026-07-15")
    assert empty["wellness"] == {} and empty["activities"] == [] and empty["flags"] == []
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        analysis.get_day(conn, "yesterday")


# --- list_activities ----------------------------------------------------------------


def test_list_activities_orders_and_filters(conn):
    res = analysis.list_activities(conn)
    assert res["cols"][:4] == ["activity_id", "date", "name", "type"]
    assert [r[0] for r in res["rows"]] == [4, 3, 2, 1]  # newest first, later start first
    assert res["n"] == 4 and res["truncated"] is False

    running = analysis.list_activities(conn, type="running")
    assert [r[0] for r in running["rows"]] == [4, 3, 1]

    long_ones = analysis.list_activities(conn, min_distance_m=9000)
    assert [r[0] for r in long_ones["rows"]] == [3, 2]

    windowed = analysis.list_activities(conn, start=day(1), end=day(7))
    assert [r[0] for r in windowed["rows"]] == [3]


def test_list_activities_truncation(conn):
    res = analysis.list_activities(conn, limit=2)
    assert res["n"] == 2 and res["truncated"] is True
    assert [r[0] for r in res["rows"]] == [4, 3]
    with pytest.raises(ValueError, match="limit"):
        analysis.list_activities(conn, limit=0)


# --- gaps -------------------------------------------------------------------------


def test_gaps_finds_missing_day_and_sync_errors(conn):
    res = analysis.gaps(conn, day(0), day(29))
    assert res["missing"]["sleep"] == [day(GAP_I)]
    assert res["missing"]["daily_wellness"] == []
    assert res["missing"]["hrv"] == []
    assert res["sync_errors"] == [{"endpoint": "sleep", "date": day(GAP_I), "error": "boom"}]


def test_gaps_outside_coverage(conn):
    res = analysis.gaps(conn, "2026-07-01", "2026-07-03")
    assert res["missing"]["daily_wellness"] == ["2026-07-01", "2026-07-02", "2026-07-03"]
    assert res["sync_errors"] == []
