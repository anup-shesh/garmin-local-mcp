"""Synthetic demo store.

Lets someone evaluate the analysis tools without a Garmin account, which is
otherwise a hard prerequisite: `demo` seeds a store, `serve` answers questions
over it immediately.

The data is generated, not recorded, but it is not noise. A latent recovery
factor drives HRV up and resting HR down together, training load raises the
next day's resting HR, and a six-day illness window sits in the middle of the
range. So `correlate`, `baselines` and `anomalies` return real structure rather
than the flat nothing that random values would produce.

Deterministic: the same seed always produces the same store.
"""

from __future__ import annotations

import math
import random
import sqlite3
from datetime import date as date_type
from datetime import timedelta

from . import db

DEMO_DAYS = 180
DEMO_SEED = 20260803

# The generated athlete. Loosely realistic mid-40s recreational runner.
_RHR_BASE = 57.0
_HRV_BASE = 34.0
_VO2_START, _VO2_END = 43.5, 47.0

_ILLNESS_LEN = 6
_MISSING_SLEEP_NIGHTS = 3

_ACTIVITY_TYPES = (
    ("running", 45, 75, 8.0, 11.0, 40, 140),
    ("cycling", 50, 95, 18.0, 34.0, 120, 420),
    ("hiking", 90, 180, 6.0, 14.0, 250, 700),
    ("strength_training", 30, 50, 0.0, 0.0, 0, 0),
)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _dates(end: date_type, days: int) -> list[date_type]:
    return [end - timedelta(days=n) for n in range(days - 1, -1, -1)]


def generate(
    conn: sqlite3.Connection,
    *,
    days: int = DEMO_DAYS,
    end: str | None = None,
    seed: int = DEMO_SEED,
) -> dict:
    """Populate `conn` with a synthetic but internally coherent store."""
    if days < 14:
        raise ValueError(f"days must be at least 14, got {days}")
    rng = random.Random(seed)
    end_date = date_type.fromisoformat(end) if end else date_type.today() - timedelta(days=1)
    all_days = _dates(end_date, days)

    # Keep the window inside the range whatever `days` is; a negative start
    # would index from the end and produce a backwards window.
    illness_start = max(3, min(days - _ILLNESS_LEN - 3, days // 2 - 20))
    illness = set(range(illness_start, illness_start + _ILLNESS_LEN))
    pool = list(range(max(3, days // 12), days - 2))
    missing_sleep = set(rng.sample(pool, min(_MISSING_SLEEP_NIGHTS, len(pool))))

    # Latent recovery factor: smooth AR(1) so streaks and baselines mean something.
    recovery = 0.0
    prev_load = 0.0
    rows = {"daily_wellness": 0, "sleep": 0, "hrv": 0, "training_status": 0, "activities": 0}
    activity_id = 9_000_000_000

    for i, day in enumerate(all_days):
        iso = day.isoformat()
        weekday = day.weekday()
        recovery = 0.72 * recovery + 0.28 * rng.gauss(0, 1)
        sick = i in illness
        # Illness pushes recovery down hard and lingers for a couple of days after.
        if sick:
            recovery -= 1.9
        elif i - _ILLNESS_LEN < illness_start <= i:
            recovery -= 0.6

        # --- activities -------------------------------------------------
        # Roughly four sessions a week, none while ill.
        train = (not sick) and rng.random() < (0.35 if weekday >= 5 else 0.60)
        load_today = 0.0
        if train:
            name, dmin, dmax, kmin, kmax, emin, emax = rng.choice(_ACTIVITY_TYPES)
            duration_s = rng.uniform(dmin, dmax) * 60
            distance_m = rng.uniform(kmin, kmax) * 1000
            elev = rng.uniform(emin, emax)
            avg_hr = int(rng.uniform(128, 152))
            max_hr = avg_hr + int(rng.uniform(12, 30))
            load_today = round(duration_s / 60 * rng.uniform(1.2, 2.4), 1)
            activity_id += 1
            db.upsert(
                conn,
                "activities",
                {
                    "activity_id": activity_id,
                    "date": iso,
                    "start_ts": f"{iso}T{rng.randint(6, 18):02d}:{rng.randint(0, 59):02d}:00",
                    "name": f"Demo {name.replace('_', ' ').title()}",
                    "type": name,
                    "duration_s": round(duration_s, 1),
                    "distance_m": round(distance_m, 1),
                    "elevation_gain_m": round(elev, 1),
                    "avg_hr": avg_hr,
                    "max_hr": max_hr,
                    "calories": int(duration_s / 60 * rng.uniform(8, 13)),
                    "avg_pace_s_per_km": (
                        round(duration_s / (distance_m / 1000), 1) if distance_m > 500 else None
                    ),
                    "training_load": load_today,
                    "aerobic_te": round(rng.uniform(1.8, 4.4), 1),
                    "anaerobic_te": round(rng.uniform(0.0, 1.6), 1),
                    "raw_path": None,
                    "fetched_at": db.utcnow(),
                },
                ("activity_id",),
            )
            rows["activities"] += 1

        # --- daily wellness ---------------------------------------------
        # Yesterday's training load shows up in today's resting HR, so a lag
        # scan over (training_load, resting_hr) finds +1 rather than 0.
        resting_hr = (
            _RHR_BASE
            - 2.4 * recovery
            + 0.026 * prev_load
            + (5.0 if sick else 0.0)
            + rng.gauss(0, 2.4)
        )
        steps = int(
            _clamp(
                (4200 if weekday >= 5 else 7600)
                + (3400 if train else 0)
                - (3000 if sick else 0)
                + rng.gauss(0, 1500),
                420,
                21000,
            )
        )
        db.upsert(
            conn,
            "daily_wellness",
            {
                "date": iso,
                "resting_hr": int(round(_clamp(resting_hr, 46, 78))),
                "min_hr": int(round(_clamp(resting_hr - rng.uniform(3, 8), 40, 70))),
                "max_hr": int(rng.uniform(112, 168)) if train else int(rng.uniform(96, 128)),
                "steps": steps,
                "distance_m": round(steps * rng.uniform(0.72, 0.81), 1),
                "floors_up": int(_clamp(rng.gauss(11, 6), 0, 48)),
                "stress_avg": int(_clamp(28 - 3.5 * recovery + (9 if sick else 0)
                                         + rng.gauss(0, 4), 12, 62)),
                "stress_max": int(_clamp(rng.gauss(82, 9), 50, 99)),
                "body_battery_high": int(_clamp(78 + 6 * recovery - (18 if sick else 0)
                                                + rng.gauss(0, 6), 30, 100)),
                "body_battery_low": int(_clamp(28 + 5 * recovery - (12 if sick else 0)
                                               + rng.gauss(0, 6), 5, 60)),
                "calories_total": int(
                    _clamp(rng.gauss(2450, 220) + (load_today * 1.6), 1500, 4200)
                ),
                "calories_active": int(_clamp(rng.gauss(520, 190) + (load_today * 1.5), 60, 2200)),
                "spo2_avg": round(_clamp(97.2 + 0.25 * recovery - (1.8 if sick else 0)
                                         + rng.gauss(0, 0.5), 88, 100), 1),
                "respiration_avg": round(_clamp(14.2 - 0.3 * recovery + (1.4 if sick else 0)
                                                + rng.gauss(0, 0.4), 11, 20), 1),
                "skin_temp_dev_c": round(_clamp((1.1 if sick else 0.0) + rng.gauss(0, 0.28),
                                                -1.5, 2.5), 1),
                "intensity_min_moderate": (
                    int(rng.uniform(4, 30)) if train else int(rng.uniform(0, 9))
                ),
                "intensity_min_vigorous": int(rng.uniform(8, 46)) if train else 0,
                "source": "api",
                "fetched_at": db.utcnow(),
            },
            ("date",),
        )
        rows["daily_wellness"] += 1

        # --- sleep -------------------------------------------------------
        if i not in missing_sleep:
            duration = _clamp(rng.gauss(420, 45) - (25 if sick else 0), 210, 560)
            deep = _clamp(rng.gauss(68, 18) + 6 * recovery - (20 if sick else 0), 0, 150)
            rem = _clamp(rng.gauss(96, 24) + 4 * recovery, 20, 210)
            awake = _clamp(rng.gauss(24, 11) + (14 if sick else 0), 0, 90)
            light = _clamp(duration - deep - rem - awake, 60, 400)
            duration = deep + rem + awake + light  # keep stages summing to duration
            bed_hour = 22 + rng.random() * 2.4
            start_dt = day - timedelta(days=1)
            db.upsert(
                conn,
                "sleep",
                {
                    "date": iso,
                    "score": int(_clamp(78 + 7 * recovery - (16 if sick else 0)
                                        + rng.gauss(0, 8), 20, 100)),
                    "duration_min": round(duration, 1),
                    "deep_min": int(deep),
                    "light_min": int(light),
                    "rem_min": int(rem),
                    "awake_min": int(awake),
                    "start_ts": (
                        f"{start_dt.isoformat()}T{int(bed_hour):02d}:"
                        f"{int(bed_hour % 1 * 60):02d}:00"
                    ),
                    "end_ts": f"{iso}T{6 + rng.randint(0, 2):02d}:{rng.randint(0, 59):02d}:00",
                    "avg_spo2": round(_clamp(96.8 - (1.9 if sick else 0) + rng.gauss(0, 0.6),
                                             88, 100), 1),
                    "avg_respiration": round(_clamp(14.0 + (1.3 if sick else 0)
                                                    + rng.gauss(0, 0.4), 11, 20), 1),
                    "avg_stress": int(_clamp(rng.gauss(22, 6) + (8 if sick else 0), 5, 60)),
                    "restless_moments": int(_clamp(rng.gauss(26, 11) + (14 if sick else 0), 0, 80)),
                    "nap_min": 0,
                    "quality_flags": None,
                    "source": "api",
                    "fetched_at": db.utcnow(),
                },
                ("date",),
            )
            rows["sleep"] += 1

        # --- hrv ---------------------------------------------------------
        hrv_value = _HRV_BASE + 4.5 * recovery - (5.5 if sick else 0.0) + rng.gauss(0, 3.6)
        hrv_value = _clamp(hrv_value, 16, 62)
        db.upsert(
            conn,
            "hrv",
            {
                "date": iso,
                "last_night_avg": int(round(hrv_value)),
                "weekly_avg": int(round(_clamp(hrv_value + rng.gauss(0, 1.3), 16, 62))),
                "high_5min": int(round(hrv_value + rng.uniform(6, 18))),
                "status": (
                    "poor" if hrv_value < 26 else "unbalanced" if hrv_value < 30 else "balanced"
                ),
                "baseline_low": 30,
                "baseline_high": 38,
                "source": "api",
                "fetched_at": db.utcnow(),
            },
            ("date",),
        )
        rows["hrv"] += 1

        # --- training status ---------------------------------------------
        acute = _clamp(prev_load * 3.4 + load_today * 2.1 + rng.gauss(0, 14), 0, 420)
        ratio = round(_clamp(acute / 190.0, 0.0, 2.2), 1)
        vo2 = _VO2_START + (_VO2_END - _VO2_START) * (i / max(days - 1, 1))
        vo2 -= 0.9 if sick else 0.0
        vo2 += 0.35 * math.sin(i / 21.0)
        db.upsert(
            conn,
            "training_status",
            {
                "date": iso,
                "status": (
                    "recovery_1" if ratio < 0.4
                    else "maintaining_1" if ratio < 0.9
                    else "productive_1" if ratio < 1.4
                    else "strained_1"
                ),
                "vo2max": round(vo2, 1),
                "acute_load": round(acute, 1),
                "load_ratio": ratio,
                "fitness_age": round(_clamp(38.5 - (vo2 - _VO2_START) * 0.8, 28, 55), 1),
                "achievable_fitness_age": round(_clamp(34.0 - (vo2 - _VO2_START) * 0.4, 26, 50), 1),
                "fetched_at": db.utcnow(),
            },
            ("date",),
        )
        rows["training_status"] += 1

        for endpoint in ("usersummary", "sleep", "hrv", "training_status", "activities"):
            if endpoint == "sleep" and i in missing_sleep:
                continue
            db.mark_sync(conn, endpoint, iso, "ok")

        prev_load = load_today

    _record_provenance(conn, days, all_days[0], all_days[-1], seed)
    return {
        "start": all_days[0].isoformat(),
        "end": all_days[-1].isoformat(),
        "days": days,
        "seed": seed,
        "rows": rows,
        "illness_window": (
            all_days[illness_start].isoformat(),
            all_days[illness_start + _ILLNESS_LEN - 1].isoformat(),
        ),
        "missing_sleep": sorted(all_days[i].isoformat() for i in missing_sleep),
    }


def _record_provenance(
    conn: sqlite3.Connection, days: int, start: date_type, end: date_type, seed: int
) -> None:
    """Mark the store as generated, so it can never be mistaken for real data."""
    with conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS demo_info ("
            "generated_at TEXT, days INT, start TEXT, end TEXT, seed INT)"
        )
        conn.execute("DELETE FROM demo_info")
        conn.execute(
            "INSERT INTO demo_info (generated_at, days, start, end, seed) VALUES (?, ?, ?, ?, ?)",
            (db.utcnow(), days, start.isoformat(), end.isoformat(), seed),
        )


def is_demo(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='demo_info'"
    ).fetchone()
    return row is not None
