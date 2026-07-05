"""Canonical metric registry.

Every query tool (query_metrics / correlate / baselines / anomalies) resolves
metric names through this one table, so the vocabulary stays consistent and
adding a metric is a one-line change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Metric:
    name: str
    table: str
    expr: str  # SQL expression over `table` (usually a bare column)
    numeric: bool = True  # False = categorical (excluded from correlate/baselines)


_METRICS = [
    # daily_wellness
    Metric("resting_hr", "daily_wellness", "resting_hr"),
    Metric("min_hr", "daily_wellness", "min_hr"),
    Metric("max_hr", "daily_wellness", "max_hr"),
    Metric("steps", "daily_wellness", "steps"),
    Metric("distance_m", "daily_wellness", "distance_m"),
    Metric("floors_up", "daily_wellness", "floors_up"),
    Metric("stress_avg", "daily_wellness", "stress_avg"),
    Metric("stress_max", "daily_wellness", "stress_max"),
    Metric("body_battery_high", "daily_wellness", "body_battery_high"),
    Metric("body_battery_low", "daily_wellness", "body_battery_low"),
    Metric("calories_total", "daily_wellness", "calories_total"),
    Metric("calories_active", "daily_wellness", "calories_active"),
    Metric("spo2_avg", "daily_wellness", "spo2_avg"),
    Metric("respiration_avg", "daily_wellness", "respiration_avg"),
    Metric("skin_temp_dev_c", "daily_wellness", "skin_temp_dev_c"),
    Metric("intensity_min_moderate", "daily_wellness", "intensity_min_moderate"),
    Metric("intensity_min_vigorous", "daily_wellness", "intensity_min_vigorous"),
    # sleep
    Metric("sleep_score", "sleep", "score"),
    Metric("sleep_duration_min", "sleep", "duration_min"),
    Metric("deep_min", "sleep", "deep_min"),
    Metric("light_min", "sleep", "light_min"),
    Metric("rem_min", "sleep", "rem_min"),
    Metric("awake_min", "sleep", "awake_min"),
    Metric("restless_moments", "sleep", "restless_moments"),
    Metric("nap_min", "sleep", "nap_min"),
    # hrv
    Metric("hrv", "hrv", "last_night_avg"),
    Metric("hrv_weekly", "hrv", "weekly_avg"),
    Metric("hrv_status", "hrv", "status", numeric=False),
    # training_status
    Metric("vo2max", "training_status", "vo2max"),
    Metric("acute_load", "training_status", "acute_load"),
    Metric("load_ratio", "training_status", "load_ratio"),
    # activities (aggregated per day)
    Metric("activity_count", "activities", "COUNT(*)"),
    Metric("activity_duration_s", "activities", "SUM(duration_s)"),
    Metric("activity_distance_m", "activities", "SUM(distance_m)"),
    Metric("elevation_gain_m", "activities", "SUM(elevation_gain_m)"),
    Metric("training_load", "activities", "SUM(training_load)"),
]

REGISTRY: dict[str, Metric] = {m.name: m for m in _METRICS}


def resolve(name: str) -> Metric:
    try:
        return REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(REGISTRY))
        raise ValueError(f"Unknown metric {name!r}. Known metrics: {known}") from None
