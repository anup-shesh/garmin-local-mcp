"""Decoder for Garmin Connect "Export Wellness Data" daily FIT bundles.

Garmin's wellness exports keep the interesting nightly metrics in FIT message
types that the public FIT profile does not name, so ``fitparse`` surfaces them
as ``unknown_<N>``. Their layouts are stable and decode deterministically with
standard FIT scaling:

- 370 / 371 (``*_HRV_STATUS.fit``): HRV summary — weekly avg, last-night avg,
  5-min high, baseline band, status enum — plus per-5-min overnight readings.
  All ms values are stored x128.
- 275 (``*_SLEEP_DATA.fit``): sleep stage records — ``unknown_253`` timestamp
  (seconds since FIT epoch), ``unknown_0`` stage enum (1=awake, 2=light,
  3=deep, 4=rem).
- 521 (``*_SLEEP_DATA.fit``): sleep score in ``unknown_1`` (0-100).
- 398 (``*_SKIN_TEMP.fit``): ``unknown_1`` nightly deviation (C),
  ``unknown_2`` 7-day avg deviation, ``unknown_4`` nightly absolute value.
- 281 (``*_METRICS.fit``): ``unknown_9`` on-device resting-HR estimate. This
  is a preliminary value that can diverge from the finalized Garmin Connect
  number on poor-sampling nights — see :func:`quality_flags`.
- 412 (``*_NAP.fit``): daytime naps — ``unknown_0`` start, ``unknown_2`` end
  (FIT timestamps), ``unknown_1`` the nap's own UTC offset in minutes.
- event 74 (``*_SLEEP_DATA.fit``): sleep window start/stop events.

All-day heart rate, steps/distance and stress come from the standard
``monitoring`` and ``stress_level`` messages in ``*_WELLNESS.fit`` files.
Every file is parsed exactly once and messages are dispatched by type, so the
decoder does not depend on Garmin's file-naming convention.

Missing files or messages leave the corresponding output keys as ``None`` —
nothing here raises on an incomplete bundle.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from fitparse import FitFile
from fitparse.utils import FitParseError

from .db import utcnow

FIT_EPOCH = dt.datetime(1989, 12, 31)
SLEEP_STAGES = {1: "awake", 2: "light", 3: "deep", 4: "rem"}
HRV_STATUS = {0: "none", 1: "poor", 2: "low", 3: "unbalanced", 4: "balanced"}

# An on-device RHR more than this far above the overnight HR floor is suspect:
# the watch never actually observed a heart rate near its own "resting" claim.
RHR_HR_FLOOR_MARGIN_BPM = 10
# A gap this long between consecutive sleep-stage records means the stage
# split is interpolated across the gap and should be treated as approximate.
SPARSE_STAGE_GAP_MIN = 45

_PLAUSIBLE_HR = range(30, 121)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _iso(ts: dt.datetime) -> str:
    return ts.replace(microsecond=0).isoformat()


def _hrv_ms(values: dict[str, Any], key: str) -> int | None:
    """HRV fields are milliseconds stored x128."""
    v = values.get(key)
    return round(v / 128) if isinstance(v, int | float) else None


def decode_bundle(folder: Path) -> dict[str, Any]:
    """Decode every ``*.fit`` file in one daily wellness export folder.

    Returns a dict whose keys are always present; sections with no data in the
    bundle are ``None`` (or an empty list for ``naps``). Timestamps are ISO
    strings in the device's local time, derived from the bundle's
    ``timestamp_correlation`` message (UTC if none is present).
    """
    folder = Path(folder)

    hr_samples: list[int] = []
    # Monitoring steps/distance are cumulative-to-date counters kept SEPARATELY
    # per activity_type (walking vs running, etc.). The daily total is the sum
    # of each type's max — a single last/max record undercounts whenever the
    # day spans more than one activity_type.
    step_max: dict[Any, int] = defaultdict(int)
    dist_max: dict[Any, float] = defaultdict(float)
    stress_vals: list[int] = []
    sleep_starts: list[dt.datetime] = []
    sleep_stops: list[dt.datetime] = []
    stage_records: list[tuple[int, int]] = []
    sleep_scores: Counter[int] = Counter()
    rhr_votes: Counter[int] = Counter()
    hrv_summary: dict[str, Any] | None = None
    skin_raw: dict[str, Any] | None = None
    naps_raw: list[tuple[int, int, int | None]] = []
    tz_offset: dt.timedelta | None = None
    last_seen: dt.datetime | None = None

    for path in sorted(folder.glob("*.fit")):
        try:
            for msg in FitFile(str(path)).get_messages():
                name = msg.name
                if name == "monitoring":
                    d = msg.get_values()
                    hr = d.get("heart_rate")
                    if isinstance(hr, int) and hr > 0:
                        hr_samples.append(hr)
                    activity = d.get("activity_type")
                    steps = d.get("steps")
                    if isinstance(steps, int):
                        step_max[activity] = max(step_max[activity], steps)
                    dist = d.get("distance")
                    if isinstance(dist, int | float):
                        dist_max[activity] = max(dist_max[activity], dist)
                    ts = d.get("timestamp")
                    if isinstance(ts, dt.datetime) and (last_seen is None or ts > last_seen):
                        last_seen = ts
                elif name == "stress_level":
                    v = msg.get_values().get("stress_level_value")
                    if isinstance(v, int) and 0 <= v <= 100:
                        stress_vals.append(v)
                elif name == "event":
                    d = msg.get_values()
                    # Event 74 is the (undocumented) sleep window marker.
                    if d.get("event") == 74 and isinstance(d.get("timestamp"), dt.datetime):
                        dest = sleep_starts if d.get("event_type") == "start" else sleep_stops
                        dest.append(d["timestamp"])
                elif name == "timestamp_correlation" and tz_offset is None:
                    d = msg.get_values()
                    local, utc = d.get("local_timestamp"), d.get("timestamp")
                    if isinstance(local, dt.datetime) and isinstance(utc, dt.datetime):
                        tz_offset = dt.timedelta(
                            minutes=round((local - utc).total_seconds() / 60)
                        )
                elif name == "unknown_275":
                    d = msg.get_values()
                    ts, stage = d.get("unknown_253"), d.get("unknown_0")
                    if isinstance(ts, int) and isinstance(stage, int):
                        stage_records.append((ts, stage))
                elif name == "unknown_281":
                    v = msg.get_values().get("unknown_9")
                    if isinstance(v, int) and v in _PLAUSIBLE_HR:
                        rhr_votes[v] += 1
                elif name == "unknown_370":
                    hrv_summary = {k: v for k, v in msg.get_values().items() if v is not None}
                elif name == "unknown_398":
                    skin_raw = {k: v for k, v in msg.get_values().items() if v is not None}
                elif name == "unknown_412":
                    d = msg.get_values()
                    start, end, off_min = d.get("unknown_0"), d.get("unknown_2"), d.get("unknown_1")
                    if isinstance(start, int) and isinstance(end, int) and end > start:
                        naps_raw.append((start, end, off_min if isinstance(off_min, int) else None))
                elif name == "unknown_521":
                    v = msg.get_values().get("unknown_1")
                    if isinstance(v, int) and 0 <= v <= 100:
                        sleep_scores[v] += 1
        except FitParseError:
            continue  # skip unreadable files, keep whatever was already decoded

    offset = tz_offset if tz_offset is not None else dt.timedelta(0)

    heart_rate = None
    if hr_samples:
        heart_rate = {
            "min": min(hr_samples),
            "avg": round(sum(hr_samples) / len(hr_samples)),
            "max": max(hr_samples),
            "samples": len(hr_samples),
        }

    stress = None
    if stress_vals:
        stress = {"avg": round(sum(stress_vals) / len(stress_vals)), "max": max(stress_vals)}

    sleep = None
    if sleep_starts and sleep_stops:
        start, end = min(sleep_starts), max(sleep_stops)
        if end > start:
            sleep = {
                "start": _iso(start + offset),
                "end": _iso(end + offset),
                "duration_min": round((end - start).total_seconds() / 60),
            }

    sleep_stages = None
    if len(stage_records) >= 2:
        stage_records.sort()
        durations: dict[str, int] = defaultdict(int)
        max_gap = 0
        for (ts, stage), (next_ts, _) in zip(stage_records, stage_records[1:], strict=False):
            segment = next_ts - ts  # each record holds its stage until the next one
            durations[SLEEP_STAGES.get(stage, "other")] += segment
            max_gap = max(max_gap, segment)
        sleep_stages = {
            "deep_min": durations.get("deep", 0) // 60,
            "light_min": durations.get("light", 0) // 60,
            "rem_min": durations.get("rem", 0) // 60,
            "awake_min": durations.get("awake", 0) // 60,
            "max_logging_gap_min": round(max_gap / 60),
        }

    hrv = None
    if hrv_summary:
        raw_status = hrv_summary.get("unknown_6")
        status = HRV_STATUS.get(raw_status, str(raw_status) if raw_status is not None else None)
        hrv = {
            "weekly_avg": _hrv_ms(hrv_summary, "unknown_0"),
            "last_night_avg": _hrv_ms(hrv_summary, "unknown_1"),
            "high_5min": _hrv_ms(hrv_summary, "unknown_2"),
            "baseline_low": _hrv_ms(hrv_summary, "unknown_4"),
            "baseline_high": _hrv_ms(hrv_summary, "unknown_5"),
            "status": status,
        }

    skin_temp = None
    if skin_raw:
        nightly, dev, dev7 = (skin_raw.get(k) for k in ("unknown_4", "unknown_1", "unknown_2"))
        skin_temp = {
            "nightly_c": round(nightly, 1) if isinstance(nightly, int | float) else None,
            "deviation_c": round(dev, 2) if isinstance(dev, int | float) else None,
            "avg_dev_7d": round(dev7, 3) if isinstance(dev7, int | float) else None,
        }

    naps = []
    for start_s, end_s, off_min in sorted(naps_raw):
        nap_offset = dt.timedelta(minutes=off_min) if off_min is not None else offset
        naps.append(
            {
                "start": _iso(FIT_EPOCH + dt.timedelta(seconds=start_s) + nap_offset),
                "end": _iso(FIT_EPOCH + dt.timedelta(seconds=end_s) + nap_offset),
                "duration_min": round((end_s - start_s) / 60),
            }
        )

    date = None
    if m := _DATE_RE.search(folder.name):
        date = m.group(0)
    elif sleep is not None:
        date = sleep["end"][:10]
    elif last_seen is not None:
        date = (last_seen + offset).date().isoformat()

    total_dist = sum(dist_max.values())
    return {
        "date": date,
        "heart_rate": heart_rate,
        "steps": sum(step_max.values()) if step_max else None,
        "distance_m": round(total_dist, 1) if dist_max else None,
        "stress": stress,
        "sleep": sleep,
        "sleep_stages": sleep_stages,
        "sleep_score": sleep_scores.most_common(1)[0][0] if sleep_scores else None,
        "hrv": hrv,
        "skin_temp": skin_temp,
        "rhr_on_device": rhr_votes.most_common(1)[0][0] if rhr_votes else None,
        "naps": naps,
    }


def quality_flags(decoded: dict[str, Any]) -> list[str]:
    """Generic data-quality flags for one decoded bundle.

    - ``rhr_far_above_hr_floor``: the on-device resting-HR estimate sits more
      than :data:`RHR_HR_FLOOR_MARGIN_BPM` above the lowest overnight HR
      sample, i.e. the watch claims a resting rate it never observed. Treat
      the RHR as unsettled and prefer the finalized Garmin Connect value.
    - ``sparse_sleep_stage_logging``: the largest gap between sleep-stage
      records is at least :data:`SPARSE_STAGE_GAP_MIN` minutes, so the stage
      split (and anything derived from it) is low-confidence.
    """
    flags: list[str] = []
    rhr = decoded.get("rhr_on_device")
    hr_min = (decoded.get("heart_rate") or {}).get("min")
    if rhr is not None and hr_min is not None and rhr > hr_min + RHR_HR_FLOOR_MARGIN_BPM:
        flags.append("rhr_far_above_hr_floor")
    gap = (decoded.get("sleep_stages") or {}).get("max_logging_gap_min")
    if gap is not None and gap >= SPARSE_STAGE_GAP_MIN:
        flags.append("sparse_sleep_stage_logging")
    return flags


def to_rows(decoded: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map one decoded bundle to row dicts for the ``sleep``, ``hrv`` and
    ``daily_wellness`` tables (see ``db.MIGRATIONS``).

    The ``daily_wellness`` row is partial: only the columns a FIT bundle can
    provide are included, so an upsert never clobbers API-sourced columns.
    ``resting_hr`` carries the on-device estimate only when no quality flag
    disputes it — a flagged value is left ``None`` for the API to backfill.
    """
    flags = quality_flags(decoded)
    fetched_at = utcnow()
    date = decoded.get("date")

    sleep = decoded.get("sleep") or {}
    stages = decoded.get("sleep_stages") or {}
    naps = decoded.get("naps") or []
    sleep_row = {
        "date": date,
        "score": decoded.get("sleep_score"),
        "duration_min": sleep.get("duration_min"),
        "deep_min": stages.get("deep_min"),
        "light_min": stages.get("light_min"),
        "rem_min": stages.get("rem_min"),
        "awake_min": stages.get("awake_min"),
        "start_ts": sleep.get("start"),
        "end_ts": sleep.get("end"),
        "nap_min": sum(n["duration_min"] for n in naps) if naps else None,
        "quality_flags": json.dumps(flags) if flags else None,
        "source": "fit",
        "fetched_at": fetched_at,
    }

    hrv = decoded.get("hrv") or {}
    hrv_row = {
        "date": date,
        "last_night_avg": hrv.get("last_night_avg"),
        "weekly_avg": hrv.get("weekly_avg"),
        "high_5min": hrv.get("high_5min"),
        "status": hrv.get("status"),
        "baseline_low": hrv.get("baseline_low"),
        "baseline_high": hrv.get("baseline_high"),
        "source": "fit",
        "fetched_at": fetched_at,
    }

    heart_rate = decoded.get("heart_rate") or {}
    stress = decoded.get("stress") or {}
    skin_temp = decoded.get("skin_temp") or {}
    resting_hr = decoded.get("rhr_on_device")
    if "rhr_far_above_hr_floor" in flags:
        resting_hr = None
    wellness_row = {
        "date": date,
        "resting_hr": resting_hr,
        "min_hr": heart_rate.get("min"),
        "max_hr": heart_rate.get("max"),
        "steps": decoded.get("steps"),
        "distance_m": decoded.get("distance_m"),
        "stress_avg": stress.get("avg"),
        "stress_max": stress.get("max"),
        "skin_temp_dev_c": skin_temp.get("deviation_c"),
        "source": "fit",
        "fetched_at": fetched_at,
    }

    return {"sleep": sleep_row, "hrv": hrv_row, "daily_wellness": wellness_row}
