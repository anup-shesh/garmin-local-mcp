"""Endpoint registry: fetch + parse for every Garmin Connect endpoint we sync.

Parse functions are pure (no I/O), tolerate missing keys (absent data becomes
None, never an exception), and return [] to mean "the API had nothing for this
day". The sync engine snapshots raw payloads verbatim before parsing, so a
parser bug never loses data - `reparse` re-runs these functions offline.
"""

from __future__ import annotations

import re
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
    "performance": ("date",),
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


def _round2(v: Any) -> float | None:
    return None if v is None else round(v, 2)


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


# --- fitnessage -> training_status (partial) --------------------------------


def fetch_fitnessage(client: Any, date: str) -> Any:
    return client.get_fitnessage_data(date)


def parse_fitnessage(payload: Any, date: str) -> list[ParsedRow]:
    if not isinstance(payload, dict):
        return []
    # Partial row: contributes two columns to training_status without touching
    # what the training_status endpoint wrote (or will write) - the same
    # mechanism sleep uses for skin_temp_dev_c in daily_wellness. Component
    # breakdowns (rhr, bmi, vigorous minutes) stay in the raw snapshot.
    row = {
        "date": date,
        "fitness_age": _round2(payload.get("fitnessAge")),
        "achievable_fitness_age": _round2(payload.get("achievableFitnessAge")),
    }
    if not _has_data(row):
        return []
    return [("training_status", row)]


# --- performance metrics -> performance --------------------------------------
#
# Four Garmin "how fit am I" scores that update on their own cadence rather than
# every day. They share one table because they are all per-day scalars and a
# reader almost always wants them together; `performance` is deliberately absent
# from analysis._DAILY_TABLES, because a day without a new endurance score is
# normal and must not be reported as a gap.
#
# KEY-NAME PROVENANCE: all four payload shapes are now VERIFIED against live
# metrics-service responses (2026-08-30). The alternate spellings each parser
# still accepts are kept as cheap insurance against firmware variation; a
# payload matching none of them yields an empty row rather than a fabricated
# one, and because raw payloads are snapshotted before parsing, `reparse`
# rebuilds every historical row offline if a shape ever changes.
#
# One shape is worth knowing: endurance score's `classification` is an opaque
# integer enum, so the tier label is derived from the `classificationLowerLimit*`
# ladder in the same payload instead - see _endurance_class.


def _first(obj: Any, *keys: str) -> Any:
    """First non-None value among `keys`; tolerates a non-dict payload."""
    if not isinstance(obj, dict):
        return None
    for key in keys:
        value = obj.get(key)
        if value is not None:
            return value
    return None


def _payload_matches_date(payload: Any, date: str) -> bool:
    """False only when the payload names a *different* calendar date.

    Several metrics-service endpoints ignore an out-of-range calendarDate and
    return the most recent reading instead. Writing that under the requested
    date would silently backdate a score, so those responses are dropped.
    """
    stamped = _first(payload, "calendarDate", "calendar_date")
    return not (isinstance(stamped, str) and stamped[:10] != date)


_TIER_PREFIX = "classificationLowerLimit"


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _endurance_class(payload: dict, score: int | None) -> str | None:
    """Name the tier from the ladder Garmin ships alongside the score.

    The response's own `classification` field is an opaque integer enum, but the
    same payload carries one `classificationLowerLimit<Tier>` key per tier
    (Intermediate/Trained/WellTrained/Expert/Superior/Elite), so the label can be
    derived from the score rather than guessing what the enum means. A score
    under the lowest rung is reported as `below_<lowest tier>` instead of being
    silently dropped.
    """
    if score is None:
        return None
    tiers = sorted(
        (limit, key[len(_TIER_PREFIX) :])
        for key, limit in payload.items()
        if key.startswith(_TIER_PREFIX) and isinstance(limit, int | float)
    )
    if not tiers:
        # Some firmware may send a plain string; fall back to it.
        return _lower(payload.get("classification"))
    label = None
    for limit, name in tiers:
        if score < limit:
            break
        label = name
    return _snake(label) if label else f"below_{_snake(tiers[0][1])}"


def fetch_endurance_score(client: Any, date: str) -> Any:
    # Single date (no enddate) returns that day's precise values; a range would
    # return weekly aggregates, which do not belong on a per-day row.
    return client.get_endurance_score(date)


def parse_endurance_score(payload: Any, date: str) -> list[ParsedRow]:
    if not isinstance(payload, dict) or not _payload_matches_date(payload, date):
        return []
    score = _int(_first(payload, "overallScore", "enduranceScore", "score"))
    row = {
        "date": date,
        "endurance_score": score,
        "endurance_class": _endurance_class(payload, score),
    }
    if not _has_data(row):
        return []
    return [("performance", row)]


def fetch_hill_score(client: Any, date: str) -> Any:
    return client.get_hill_score(date)


def parse_hill_score(payload: Any, date: str) -> list[ParsedRow]:
    if not isinstance(payload, dict) or not _payload_matches_date(payload, date):
        return []
    row = {
        "date": date,
        "hill_score": _int(_first(payload, "overallScore", "hillScore", "score")),
        "hill_endurance_score": _int(_first(payload, "enduranceScore", "hillEnduranceScore")),
        "hill_strength_score": _int(_first(payload, "strengthScore", "hillStrengthScore")),
    }
    if not _has_data(row):
        return []
    return [("performance", row)]


def fetch_training_readiness(client: Any, date: str) -> Any:
    return client.get_training_readiness(date)


def parse_training_readiness(payload: Any, date: str) -> list[ParsedRow]:
    # The endpoint returns a list of snapshots (one per wake-up/scheduled
    # update); a few responses hand back a bare dict. Prefer the post-wake
    # reading, which is the score Garmin shows in the Morning Report, and fall
    # back to the first entry - mirroring get_morning_training_readiness.
    if isinstance(payload, dict):
        snapshot = payload
    elif isinstance(payload, list):
        entries = [item for item in payload if isinstance(item, dict)]
        if not entries:
            return []
        snapshot = next(
            (e for e in entries if e.get("inputContext") == "AFTER_WAKEUP_RESET"),
            entries[0],
        )
    else:
        return []
    if not _payload_matches_date(snapshot, date):
        return []
    row = {
        "date": date,
        "readiness_score": _int(snapshot.get("score")),
        "readiness_level": _lower(snapshot.get("level")),
        "recovery_time_min": _int(snapshot.get("recoveryTime")),
    }
    if not _has_data(row):
        return []
    return [("performance", row)]


def fetch_race_predictions(client: Any, date: str) -> Any:
    # 'daily' over a single-day range keeps one row per calendar date; the
    # no-argument form would return "latest", which is undated and would be
    # written under whatever day happened to be syncing.
    return client.get_race_predictions(date, date, _type="daily")


def parse_race_predictions(payload: Any, date: str) -> list[ParsedRow]:
    # Range form returns a list; be tolerant of a single dict.
    if isinstance(payload, dict):
        entry: Any = payload
    elif isinstance(payload, list):
        entries = [item for item in payload if isinstance(item, dict)]
        entry = next(
            (e for e in entries if str(_first(e, "calendarDate") or "")[:10] == date),
            entries[0] if len(entries) == 1 else None,
        )
    else:
        return []
    if entry is None or not _payload_matches_date(entry, date):
        return []
    row = {
        "date": date,
        "race_5k_s": _int(_first(entry, "time5K", "raceTime5K")),
        "race_10k_s": _int(_first(entry, "time10K", "raceTime10K")),
        "race_half_s": _int(_first(entry, "timeHalfMarathon", "raceTimeHalfMarathon")),
        "race_marathon_s": _int(_first(entry, "timeMarathon", "raceTimeMarathon")),
    }
    if not _has_data(row):
        return []
    return [("performance", row)]


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
        Endpoint("fitnessage", fetch_fitnessage, parse_fitnessage),
        Endpoint("endurance_score", fetch_endurance_score, parse_endurance_score),
        Endpoint("hill_score", fetch_hill_score, parse_hill_score),
        Endpoint("training_readiness", fetch_training_readiness, parse_training_readiness),
        Endpoint("race_predictions", fetch_race_predictions, parse_race_predictions),
        Endpoint("activities", fetch_activities, parse_activities),
    )
}
