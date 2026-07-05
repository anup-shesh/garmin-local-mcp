"""Endpoint registry: fetch + parse for every Garmin Connect endpoint we sync.

Parse functions are pure (no I/O), tolerate missing keys (absent data becomes
None, never an exception), and return [] to mean "the API had nothing for this
day". The sync engine snapshots raw payloads verbatim before parsing, so a
parser bug never loses data - `reparse` re-runs these functions offline.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# A parse result: (table name, row dict). Rows may be partial - the sync
# engine upserts them with db.upsert_partial so two endpoints can each
# contribute columns to the same row (e.g. sleep's skin-temp deviation joins
# usersummary's vitals in daily_wellness regardless of fetch order).
ParsedRow = tuple[str, dict]

TABLE_KEYS: dict[str, tuple[str, ...]] = {
    "daily_wellness": ("date",),
    "sleep": ("date",),
    "hrv": ("date",),
    "training_status": ("date",),
    "activities": ("activity_id",),
}


@dataclass(frozen=True)
class Endpoint:
    name: str
    fetch: Callable[[Any, str], Any]  # (client, date) -> raw payload
    parse: Callable[[Any, str], list[ParsedRow]]  # (payload, date) -> rows


def _get(obj: Any, *path: str) -> Any:
    """Walk nested dicts; None as soon as anything is missing or not a dict."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _int(v: Any) -> int | None:
    return None if v is None else round(v)


def _minutes(seconds: Any) -> int | None:
    return None if seconds is None else round(seconds / 60)


def _lower(v: Any) -> str | None:
    return v.lower() if isinstance(v, str) else None


def _iso_local(epoch_ms: Any) -> str | None:
    """Garmin '...TimestampLocal' epoch-ms values are already wall-clock local."""
    if epoch_ms is None:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _has_data(row: dict, ignore: tuple[str, ...] = ("date",)) -> bool:
    return any(v is not None for k, v in row.items() if k not in ignore)


# --- usersummary -> daily_wellness ----------------------------------------


def fetch_usersummary(client: Any, date: str) -> Any:
    return client.get_stats(date)


def parse_usersummary(payload: Any, date: str) -> list[ParsedRow]:
    if not isinstance(payload, dict):
        return []
    row = {
        "date": date,
        "resting_hr": payload.get("restingHeartRate"),
        "min_hr": payload.get("minHeartRate"),
        "max_hr": payload.get("maxHeartRate"),
        "steps": payload.get("totalSteps"),
        "distance_m": payload.get("totalDistanceMeters"),
        "floors_up": _int(payload.get("floorsAscended")),
        "stress_avg": payload.get("averageStressLevel"),
        "stress_max": payload.get("maxStressLevel"),
        "body_battery_high": payload.get("bodyBatteryHighestValue"),
        "body_battery_low": payload.get("bodyBatteryLowestValue"),
        "calories_total": _int(payload.get("totalKilocalories")),
        "calories_active": _int(payload.get("activeKilocalories")),
        "spo2_avg": payload.get("averageSpo2"),
        "respiration_avg": payload.get("avgWakingRespirationValue"),
        "intensity_min_moderate": payload.get("moderateIntensityMinutes"),
        "intensity_min_vigorous": payload.get("vigorousIntensityMinutes"),
    }
    if not _has_data(row):
        return []
    row["source"] = "api"
    return [("daily_wellness", row)]


# --- sleep -> sleep (+ skin temp into daily_wellness) ----------------------


def fetch_sleep(client: Any, date: str) -> Any:
    return client.get_sleep_data(date)


def parse_sleep(payload: Any, date: str) -> list[ParsedRow]:
    # The full payload is ~230KB of per-minute arrays; only summary fields are
    # parsed here - the raw snapshot keeps everything.
    if not isinstance(payload, dict):
        return []
    dto = payload.get("dailySleepDTO") or {}
    row = {
        "date": date,
        "score": _get(dto, "sleepScores", "overall", "value"),
        "duration_min": None
        if dto.get("sleepTimeSeconds") is None
        else round(dto["sleepTimeSeconds"] / 60, 1),
        "deep_min": _minutes(dto.get("deepSleepSeconds")),
        "light_min": _minutes(dto.get("lightSleepSeconds")),
        "rem_min": _minutes(dto.get("remSleepSeconds")),
        "awake_min": _minutes(dto.get("awakeSleepSeconds")),
        "start_ts": _iso_local(dto.get("sleepStartTimestampLocal")),
        "end_ts": _iso_local(dto.get("sleepEndTimestampLocal")),
        "avg_spo2": dto.get("averageSpO2Value"),
        "avg_respiration": dto.get("averageRespirationValue"),
        "avg_stress": _int(dto.get("avgSleepStress")),
        "restless_moments": payload.get("restlessMomentsCount"),
        "nap_min": _minutes(dto.get("napTimeSeconds")),
        "quality_flags": None,
    }
    if not _has_data(row):
        return []
    row["source"] = "api"
    rows: list[ParsedRow] = [("sleep", row)]
    skin_temp = payload.get("avgSkinTempDeviationC")
    if skin_temp is not None:
        # Partial row: contributes one column to daily_wellness without
        # clobbering whatever usersummary wrote (or will write).
        rows.append(("daily_wellness", {"date": date, "skin_temp_dev_c": skin_temp}))
    return rows


# --- hrv -> hrv -------------------------------------------------------------


def fetch_hrv(client: Any, date: str) -> Any:
    return client.get_hrv_data(date)


def parse_hrv(payload: Any, date: str) -> list[ParsedRow]:
    summary = _get(payload, "hrvSummary")
    if not isinstance(summary, dict):
        return []  # endpoint returns None (or no summary) on days without HRV
    row = {
        "date": date,
        "last_night_avg": summary.get("lastNightAvg"),
        "weekly_avg": summary.get("weeklyAvg"),
        "high_5min": summary.get("lastNight5MinHigh"),
        "status": _lower(summary.get("status")),
        "baseline_low": _get(summary, "baseline", "balancedLow"),
        "baseline_high": _get(summary, "baseline", "balancedUpper"),
    }
    if not _has_data(row):
        return []
    row["source"] = "api"
    return [("hrv", row)]


# --- training_status -> training_status -------------------------------------


def fetch_training_status(client: Any, date: str) -> Any:
    return client.get_training_status(date)


def parse_training_status(payload: Any, date: str) -> list[ParsedRow]:
    if not isinstance(payload, dict):
        return []
    status = acute_load = load_ratio = None
    device_map = _get(payload, "mostRecentTrainingStatus", "latestTrainingStatusData")
    if isinstance(device_map, dict) and device_map:
        device = device_map[next(iter(device_map))]  # keyed by deviceId; take the first
        status = _lower(_get(device, "trainingStatusFeedbackPhrase"))
        acute_load = _get(device, "acuteTrainingLoadDTO", "dailyTrainingLoadAcute")
        load_ratio = _get(device, "acuteTrainingLoadDTO", "dailyAcuteChronicWorkloadRatio")
    row = {
        "date": date,
        "status": status,
        "vo2max": _get(payload, "mostRecentVO2Max", "generic", "vo2MaxPreciseValue"),
        "acute_load": acute_load,
        "load_ratio": load_ratio,
    }
    if not _has_data(row):
        return []
    return [("training_status", row)]


# --- activities -> activities ------------------------------------------------


def fetch_activities(client: Any, date: str) -> Any:
    return client.get_activities_by_date(date, date)


def activity_date(item: dict, fallback: str | None = None) -> str | None:
    start = item.get("startTimeLocal")
    return start[:10] if isinstance(start, str) else fallback


def parse_activities(payload: Any, date: str) -> list[ParsedRow]:
    if not isinstance(payload, list):
        return []
    rows: list[ParsedRow] = []
    for item in payload:
        if not isinstance(item, dict) or item.get("activityId") is None:
            continue
        activity_id = item["activityId"]
        start = item.get("startTimeLocal")
        duration = item.get("duration")
        distance = item.get("distance")
        pace = duration / (distance / 1000) if duration is not None and distance else None
        rows.append(
            (
                "activities",
                {
                    "activity_id": activity_id,
                    "date": activity_date(item, fallback=date or None),
                    "start_ts": start.replace(" ", "T") if isinstance(start, str) else None,
                    "name": item.get("activityName"),
                    "type": _get(item, "activityType", "typeKey"),
                    "duration_s": duration,
                    "distance_m": distance,
                    "elevation_gain_m": item.get("elevationGain"),
                    "avg_hr": _int(item.get("averageHR")),
                    "max_hr": _int(item.get("maxHR")),
                    "calories": _int(item.get("calories")),
                    "avg_pace_s_per_km": pace,
                    "training_load": item.get("activityTrainingLoad"),
                    "aerobic_te": item.get("aerobicTrainingEffect"),
                    "anaerobic_te": item.get("anaerobicTrainingEffect"),
                    "raw_path": f"activities/{activity_id}.json",
                },
            )
        )
    return rows


# body_battery and rhr_day endpoints are deliberately absent: usersummary
# already carries their useful fields (body battery high/low, resting HR).
ENDPOINTS: dict[str, Endpoint] = {
    e.name: e
    for e in (
        Endpoint("usersummary", fetch_usersummary, parse_usersummary),
        Endpoint("sleep", fetch_sleep, parse_sleep),
        Endpoint("hrv", fetch_hrv, parse_hrv),
        Endpoint("training_status", fetch_training_status, parse_training_status),
        Endpoint("activities", fetch_activities, parse_activities),
    )
}
