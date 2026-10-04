"""Chronotype, sleep need and an inferred wake window, with out-of-sample evidence.

Three separate quantities, each with an 80% interval:

1. Chronotype: MCTQ MSFsc, mid-sleep on free days corrected for sleep debt
   (Roenneberg). The published, validated output.
2. Sleep need: user-supplied, else the mean free-night sleep window.
3. Circadian-compatible wake window: MSFsc + need / 2. An inference, not an
   MCTQ output. With need taken from free nights and no debt correction this is
   exactly the mean free-night wake time.

An evidence block then asks, on nights the estimate never saw (rolling
holdout), whether waking near that window goes with higher Garmin outcome
scores beyond what sleep duration and sleep regularity predict.

Wording rule: outcomes are "Garmin scores", effects are "associated with".
Garmin's scores are composites, not physiological ground truth, so nothing
here may claim that wake timing changes physiology.

All times are minutes relative to midnight of the wake date (23:00 is -60,
06:30 is 390). Pure functions over a sqlite3.Connection, stdlib only, and
deterministic: every random draw comes from a fixed-seed generator.
"""

from __future__ import annotations

import math
import random
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .analysis import _parse_date, _pearson

MIN_NIGHTS = 28
MIN_FREE_NIGHTS = 8
MIN_OUTCOME_VALUES = 28
TRAVEL_FILTER_MIN = 180
PRIOR_NIGHTS = 7  # nights in the trailing median for travel filter and drift
PRIOR_MIN = 4  # fewer prior nights than this: no filter, no drift
PRIOR_LOOKBACK_DAYS = 14  # prior nights must be this recent; keeps a 9-night trip filtered
BOOTSTRAP_N = 500
SEED = 0
RIDGE = 1e-6  # identical for every model, on standardised predictors
TRAIN_DAYS, TEST_DAYS = 120, 30
MIN_HISTORY_DAYS = 180
MIN_TRAIN_NIGHTS = 20
MIN_TEST_NIGHTS = 10
MIN_BIN_NIGHTS = 8
BIN_MIN = 30
NEAR_MIN = 30  # "near the window" and "agree" tolerance
WINDOW_HALF_MIN = 15
LIGHTS_OUT_MIN = 15
COLLINEAR_R = 0.8
SIMILAR_RMSE = 0.02
SHIFT_WORK = (480, 1200)  # median mid-sleep in 08:00..20:00
SENS_WINDOWS = (90, 180, 365)
SENS_FILTERS = (120, 180, 240)

OUTCOMES = {
    "readiness_score": "readiness",
    "sleep_score": "score",
    "body_battery_high": "bb_high",
}
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_FREE_DAYS = ("sat", "sun")

MODELS: dict[str, tuple[str, ...]] = {
    "A": ("dur", "dur2"),
    "B": ("wake", "wake2"),
    "C": ("dist",),
    "D": ("dur", "dur2", "dist"),
    "E": ("dur", "dur2", "wake", "wake2"),
    "G": ("dur", "dur2", "drift"),
    "F": ("dur", "dur2", "dist", "drift"),
}

# Never allowed in generated text (test-enforced): see the module docstring.
BANNED_WORDS = ("recovery", "healthy", "unhealthy", "significant", "causes", "improves")


# --- data ---------------------------------------------------------------------


@dataclass(slots=True)
class Night:
    day: date_type  # wake date
    onset: float
    wake: float
    asleep: float | None  # sleep.duration_min
    outcomes: dict[str, float | None]
    drift: float | None = None

    @property
    def window(self) -> float:
        return self.wake - self.onset

    @property
    def mid(self) -> float:
        return (self.onset + self.wake) / 2

    @property
    def week(self) -> tuple[int, int]:
        iso = self.day.isocalendar()
        return iso[0], iso[1]

    @property
    def duration(self) -> float:
        return self.asleep if self.asleep is not None else self.window


@dataclass
class Chrono:
    msf: float
    msf_sc: float
    msw: float
    sd_free: float
    sd_work: float
    sd_week: float
    n_free: int
    n_work: int


def _minutes(ts: str, day: date_type) -> float:
    t = datetime.fromisoformat(ts)
    return (t - datetime.combine(day, time())).total_seconds() / 60


def load_nights(conn: sqlite3.Connection, end: str) -> list[Night]:
    """Every main-sleep night with both timestamps, up to `end`, oldest first."""
    sql = (
        "SELECT s.date, s.start_ts, s.end_ts, s.duration_min, s.score, "
        "p.readiness_score, w.body_battery_high "
        "FROM sleep s "
        "LEFT JOIN performance p ON p.date = s.date "
        "LEFT JOIN daily_wellness w ON w.date = s.date "
        "WHERE s.start_ts IS NOT NULL AND s.end_ts IS NOT NULL AND s.date <= ? "
        "ORDER BY s.date"
    )
    nights = []
    for row in conn.execute(sql, (end,)):
        day = date_type.fromisoformat(row["date"])
        try:
            onset, wake = _minutes(row["start_ts"], day), _minutes(row["end_ts"], day)
        except ValueError:
            continue  # unparseable timestamp: counted nowhere, like a missing row
        nights.append(
            Night(
                day=day,
                onset=onset,
                wake=wake,
                asleep=row["duration_min"],
                outcomes={
                    "readiness": row["readiness_score"],
                    "score": row["score"],
                    "bb_high": row["body_battery_high"],
                },
            )
        )
    return nights


# --- small helpers --------------------------------------------------------------


def _hhmm(minutes: float) -> str:
    m = round(minutes) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _mean(values) -> float:
    values = list(values)
    return sum(values) / len(values)


def _r1(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _percentile(values: list[float], q: float) -> float:
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _interval(values: list[float]) -> tuple[float, float] | None:
    return (_percentile(values, 0.1), _percentile(values, 0.9)) if values else None


def _mad(values: list[float]) -> float:
    med = statistics.median(values)
    return statistics.median(abs(v - med) for v in values)


def _se(values: list[float]) -> float:
    return statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else 0.0


def parse_free_days(free_days: list[str] | None) -> frozenset[int]:
    names = list(free_days) if free_days else list(DEFAULT_FREE_DAYS)
    out = set()
    for name in names:
        key = str(name).strip().lower()[:3]
        if key not in DAY_NAMES:
            raise ValueError(f"Unknown free day {name!r}; use mon, tue, wed, thu, fri, sat, sun")
        out.add(DAY_NAMES.index(key))
    if not 1 <= len(out) <= 6:
        raise ValueError("free_days must name between 1 and 6 days of the week")
    return frozenset(out)


# --- cleaning, drift ----------------------------------------------------------------


def _prior_mids(kept: list[Night], day: date_type) -> list[float]:
    cutoff = day - timedelta(days=PRIOR_LOOKBACK_DAYS)
    prior = [n.mid for n in kept[-PRIOR_NIGHTS:] if n.day >= cutoff]
    return prior if len(prior) >= PRIOR_MIN else []


def clean(nights: list[Night], travel_min: float = TRAVEL_FILTER_MIN):
    """Drop implausible nights and travel-like jumps; returns (kept, implausible, shifted)."""
    kept: list[Night] = []
    implausible: list[Night] = []
    shifted: list[Night] = []
    for n in nights:
        if not (120 <= n.window <= 900) or not (-720 <= n.onset <= 720):
            implausible.append(n)
            continue
        prior = _prior_mids(kept, n.day)
        if prior and abs(n.mid - statistics.median(prior)) > travel_min:
            shifted.append(n)
            continue
        kept.append(n)
    return kept, implausible, shifted


def assign_drift(kept: list[Night]) -> None:
    """Drift = |mid-sleep - median of the previous nights|, from earlier timing only."""
    for i, n in enumerate(kept):
        prior = _prior_mids(kept[:i], n.day)
        n.drift = abs(n.mid - statistics.median(prior)) if prior else None


# --- MCTQ ---------------------------------------------------------------------------


def chronotype(
    nights: list[Night], free: frozenset[int], use_median: bool = False
) -> tuple[Chrono | None, str | None]:
    fr = [n for n in nights if n.day.weekday() in free]
    wk = [n for n in nights if n.day.weekday() not in free]
    expected_share = len(free) / 7
    if len(fr) < MIN_FREE_NIGHTS:
        return None, f"only {len(fr)} free nights; need at least {MIN_FREE_NIGHTS}"
    if len(fr) < 0.5 * expected_share * len(nights):
        return None, (
            f"free nights are {len(fr)} of {len(nights)} ({len(fr) / len(nights):.0%}), under "
            f"half the expected {expected_share:.0%}; missing free nights would bias the estimate"
        )
    if not wk:
        return None, "no work nights in range"
    agg = statistics.median if use_median else _mean
    msf = agg([n.mid for n in fr])
    sd_f = _mean(n.window for n in fr)
    sd_w = _mean(n.window for n in wk)
    sd_week = ((7 - len(free)) * sd_w + len(free) * sd_f) / 7
    msf_sc = msf - (sd_f - sd_week) / 2 if sd_f > sd_w else msf
    return Chrono(
        msf=msf,
        msf_sc=msf_sc,
        msw=_mean(n.mid for n in wk),
        sd_free=sd_f,
        sd_work=sd_w,
        sd_week=sd_week,
        n_free=len(fr),
        n_work=len(wk),
    ), None


def label(msf_sc: float) -> str:
    """Approximate population bands for working adults; no age or sex adjustment."""
    if msf_sc < 210:
        return "early"
    if msf_sc <= 270:
        return "intermediate"
    return "late"


def need_from_outcome(nights: list[Night], key: str) -> dict | None:
    """Range of 30-min window bins whose mean outcome is within 1 point of the best."""
    bins: dict[int, list[float]] = {}
    for n in nights:
        y = n.outcomes[key]
        if y is not None:
            bins.setdefault(int(n.window // BIN_MIN) * BIN_MIN, []).append(y)
    means = {lo: _mean(v) for lo, v in bins.items() if len(v) >= MIN_BIN_NIGHTS}
    if len(means) < 2:
        return None
    best = max(means.values())
    good = sorted(lo for lo, m in means.items() if m >= best - 1)
    return {"range": [good[0], good[-1] + BIN_MIN], "shortest_mid": good[0] + BIN_MIN / 2}


def _wake_and_need(c: Chrono, need_user: int | None) -> tuple[float, float]:
    need = float(need_user) if need_user is not None else c.sd_free
    return c.msf_sc + need / 2, need


# --- bootstrap, sensitivity -------------------------------------------------------


def _week_blocks(nights: list[Night]) -> list[list[Night]]:
    blocks: dict[tuple[int, int], list[Night]] = {}
    for n in nights:
        blocks.setdefault(n.week, []).append(n)
    return list(blocks.values())


def _resample(blocks: list[list[Night]], rng: random.Random) -> list[Night]:
    out: list[Night] = []
    for _ in range(len(blocks)):
        out.extend(rng.choice(blocks))
    return out


def bootstrap(nights: list[Night], free: frozenset[int], need_user: int | None) -> dict:
    """Week-block bootstrap of MSFsc, need and wake, computed jointly per resample."""
    rng = random.Random(SEED)
    blocks = _week_blocks(nights)
    msfs, needs, wakes, shares = [], [], [], []
    skipped = 0
    for _ in range(BOOTSTRAP_N):
        sample = _resample(blocks, rng)
        shares.append(sum(n.day.weekday() in free for n in sample) / len(sample))
        c, _reason = chronotype(sample, free)
        if c is None:
            skipped += 1
            continue
        wake, need = _wake_and_need(c, need_user)
        msfs.append(c.msf_sc)
        needs.append(need)
        wakes.append(wake)
    return {
        "msf_sc": _interval(msfs),
        "need": _interval(needs),
        "wake": _interval(wakes),
        "skipped": skipped,
        "free_share": {
            "original": sum(n.day.weekday() in free for n in nights) / len(nights),
            "resampled_mean": _mean(shares),
            "resampled_80": _interval(shares),
        },
    }


def sensitivity(
    history: list[Night], end: date_type, free: frozenset[int], need_user: int | None
) -> dict | None:
    runs = []
    for w in SENS_WINDOWS:
        first = end - timedelta(days=w - 1)
        raw = [n for n in history if n.day >= first]
        for thr in SENS_FILTERS:
            kept, _, _ = clean(raw, thr)
            if len(kept) < MIN_NIGHTS:
                continue
            for est in ("mean", "median"):
                c, _reason = chronotype(kept, free, use_median=est == "median")
                if c is None:
                    continue
                wake, _need = _wake_and_need(c, need_user)
                runs.append({"window": w, "filter": thr, "msf": est,
                             "msf_sc": c.msf_sc, "wake": wake, "label": label(c.msf_sc)})
    if not runs:
        return None
    msf_range = max(r["msf_sc"] for r in runs) - min(r["msf_sc"] for r in runs)
    wake_range = max(r["wake"] for r in runs) - min(r["wake"] for r in runs)
    label_stable = len({r["label"] for r in runs}) == 1
    if msf_range <= 20 and wake_range <= 30 and label_stable:
        level = "high"
    elif msf_range <= 45 and wake_range <= 60:
        level = "moderate"
    else:
        level = "low"

    defaults = {"window": 180, "filter": TRAVEL_FILTER_MIN, "msf": "mean"}
    spread = {}
    for dim in defaults:
        others = {k: v for k, v in defaults.items() if k != dim}
        wakes = [r["wake"] for r in runs if all(r[k] == v for k, v in others.items())]
        spread[dim] = max(wakes) - min(wakes) if len(wakes) > 1 else 0.0
    driver = max(spread, key=spread.get)
    return {
        "level": level,
        "msf_sc_range": [_hhmm(min(r["msf_sc"] for r in runs)),
                         _hhmm(max(r["msf_sc"] for r in runs))],
        "wake_range": [_hhmm(min(r["wake"] for r in runs)), _hhmm(max(r["wake"] for r in runs))],
        "label_stable": label_stable,
        "most_sensitive_to": driver,
        "driver_spread_min": round(spread[driver]),
        "runs": runs,
    }


# --- least squares --------------------------------------------------------------------


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (k <= 4)."""
    k = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(k):
        piv = max(range(col, k), key=lambda r: abs(m[r][col]))
        m[col], m[piv] = m[piv], m[col]
        if m[col][col] == 0:
            continue
        for r in range(col + 1, k):
            f = m[r][col] / m[col][col]
            for c in range(col, k + 1):
                m[r][c] -= f * m[col][c]
    x = [0.0] * k
    for r in range(k - 1, -1, -1):
        s = m[r][k] - sum(m[r][c] * x[c] for c in range(r + 1, k))
        x[r] = s / m[r][r] if m[r][r] else 0.0
    return x


def fit(xs: list[list[float]], ys: list[float]) -> tuple[float, list[float]]:
    """Ridge OLS on standardised predictors; intercept (= mean y) unpenalised."""
    k = len(xs[0])
    ybar = _mean(ys)
    a = [[sum(r[i] * r[j] for r in xs) + (RIDGE if i == j else 0.0) for j in range(k)]
         for i in range(k)]
    b = [sum(r[i] * (y - ybar) for r, y in zip(xs, ys, strict=True)) for i in range(k)]
    return ybar, _solve(a, b)


def _features(n: Night, cw: float, centre: dict[str, float]) -> dict[str, float]:
    dc = n.duration - centre["dur"]
    wc = n.wake - centre["wake"]
    return {"dur": dc, "dur2": dc * dc, "wake": wc, "wake2": wc * wc,
            "dist": abs(n.wake - cw), "drift": n.drift}


# --- evidence ------------------------------------------------------------------------------


@dataclass
class Fold:
    train_start: date_type
    test_start: date_type
    n_train: int
    n_test: int
    rmse: dict[str, float]
    dist_coef_d: float
    dist_coef_f: float
    dist_drift_r: float | None
    test_rows: list[tuple] = field(default_factory=list)  # (week, near, resid_A)
    sq_err: dict[str, list[float]] = field(default_factory=dict)


def _run_fold(train_nights, test_nights, free, need_user, key, t0, t1) -> Fold | str:
    c, reason = chronotype(train_nights, free)
    if c is None:
        return f"no chronotype in training window ({reason})"
    cw, _need = _wake_and_need(c, need_user)
    # One eligibility mask per fold, shared by every model.
    tr = [n for n in train_nights if n.outcomes[key] is not None and n.drift is not None]
    te = [n for n in test_nights if n.outcomes[key] is not None and n.drift is not None]
    if len(tr) < MIN_TRAIN_NIGHTS:
        return f"only {len(tr)} eligible training nights"
    if len(te) < MIN_TEST_NIGHTS:
        return f"only {len(te)} eligible test nights"

    centre = {"dur": _mean(n.duration for n in tr), "wake": _mean(n.wake for n in tr)}
    ftr = [_features(n, cw, centre) for n in tr]
    fte = [_features(n, cw, centre) for n in te]
    # Standardise with training-fold statistics only.
    scale = {}
    for name in ftr[0]:
        vals = [f[name] for f in ftr]
        mu = _mean(vals)
        sd = statistics.pstdev(vals) or 1.0
        scale[name] = (mu, sd)

    def z(f: dict, names: tuple[str, ...]) -> list[float]:
        return [(f[nm] - scale[nm][0]) / scale[nm][1] for nm in names]

    ytr = [n.outcomes[key] for n in tr]
    yte = [n.outcomes[key] for n in te]
    rmse, sq_err, coefs, resid_a = {}, {}, {}, []
    for model, names in MODELS.items():
        b0, beta = fit([z(f, names) for f in ftr], ytr)
        errs = []
        for f, y in zip(fte, yte, strict=True):
            pred = b0 + sum(bi * xi for bi, xi in zip(beta, z(f, names), strict=True))
            errs.append(y - pred)
        sq_err[model] = [e * e for e in errs]
        rmse[model] = math.sqrt(_mean(sq_err[model]))
        coefs[model] = dict(zip(names, beta, strict=True))
        if model == "A":
            resid_a = errs
    return Fold(
        train_start=t0,
        test_start=t1,
        n_train=len(tr),
        n_test=len(te),
        rmse=rmse,
        dist_coef_d=coefs["D"]["dist"],
        dist_coef_f=coefs["F"]["dist"],
        dist_drift_r=_pearson([f["dist"] for f in ftr], [f["drift"] for f in ftr]),
        test_rows=[(n.week, abs(n.wake - cw) <= NEAR_MIN, r)
                   for n, r in zip(te, resid_a, strict=True)],
        sq_err=sq_err,
    )


def _effect(rows: list[tuple]) -> float | None:
    near = [r for _, is_near, r in rows if is_near]
    far = [r for _, is_near, r in rows if not is_near]
    if len(near) < 3 or len(far) < 3:
        return None
    return _mean(near) - _mean(far)


def evidence(
    history: list[Night], end: date_type, free: frozenset[int], need_user: int | None, key: str
) -> dict:
    if not history:
        return {"status": "insufficient_history", "days": 0}
    days = (end - history[0].day).days + 1
    if days < MIN_HISTORY_DAYS:
        return {"status": "insufficient_history", "days": days,
                "needs_days": MIN_HISTORY_DAYS}

    folds: list[Fold] = []
    skipped: list[str] = []
    t0 = history[0].day
    while True:
        t1 = t0 + timedelta(days=TRAIN_DAYS)
        t2 = t1 + timedelta(days=TEST_DAYS)
        if t2 - timedelta(days=1) > end:
            break
        train = [n for n in history if t0 <= n.day < t1]
        test = [n for n in history if t1 <= n.day < t2]
        result = _run_fold(train, test, free, need_user, key, t0, t1)
        if isinstance(result, Fold):
            folds.append(result)
        else:
            skipped.append(f"{t1.isoformat()}: {result}")
        t0 += timedelta(days=TEST_DAYS)

    if len(folds) < 2:
        return {"status": "insufficient_folds", "folds": len(folds),
                "folds_skipped": len(skipped), "skipped": skipped[:3]}

    n = len(folds)
    pooled = {m: math.sqrt(_mean([e for f in folds for e in f.sq_err[m]])) for m in MODELS}
    d_beats_a = sum(f.rmse["D"] < f.rmse["A"] for f in folds)
    f_beats_g = sum(f.rmse["F"] < f.rmse["G"] for f in folds)
    sign_ok = sum(f.dist_coef_f < 0 for f in folds)
    rs = [abs(f.dist_drift_r) for f in folds if f.dist_drift_r is not None]
    median_r = statistics.median(rs) if rs else 0.0
    if pooled["E"] == 0:  # E fits the test nights exactly (degenerate store)
        d_vs_e = "similar" if pooled["D"] == 0 else "worse"
    else:
        ratio = pooled["D"] / pooled["E"]
        d_vs_e = ("similar" if abs(ratio - 1) <= SIMILAR_RMSE
                  else "better" if ratio < 1 else "worse")

    if median_r > COLLINEAR_R:
        survives: bool | str = "indeterminate"
    else:
        survives = (pooled["F"] < pooled["G"] and f_beats_g > n / 2 and sign_ok >= 0.7 * n)

    rows = [row for f in folds for row in f.test_rows]
    effect = _effect(rows)
    rng = random.Random(SEED)
    weeks: dict[tuple, list[tuple]] = {}
    for row in rows:
        weeks.setdefault(row[0], []).append(row)
    blocks = list(weeks.values())
    samples = []
    for _ in range(BOOTSTRAP_N):
        sample = [row for _ in range(len(blocks)) for row in rng.choice(blocks)]
        e = _effect(sample)
        if e is not None:
            samples.append(e)
    effect_80 = _interval(samples)

    d_better = pooled["D"] < pooled["A"]
    if (n >= 4 and d_better and d_beats_a >= 0.7 * n and effect_80 is not None
            and effect_80[0] > 0 and survives is True):
        level = "consistent"
    elif d_better and d_beats_a > n / 2 and effect is not None and effect > 0:
        level = "supportive"
    elif d_better:
        level = "suggestive"
    else:
        level = "unsupported"

    return {
        "status": "ok",
        "folds": n,
        "test_nights": len(rows),
        "n_test_range": [min(f.n_test for f in folds), max(f.n_test for f in folds)],
        "d_beats_a_folds": f"{d_beats_a}/{n}",
        "d_vs_e": d_vs_e,
        "effect_within_window": _r1(effect),
        "effect_interval_80": None if effect_80 is None else [_r1(v) for v in effect_80],
        "distance_survives_regularity": survives,
        "f_beats_g_folds": f"{f_beats_g}/{n}",
        "_distance_sign_consistent_folds": f"{sign_ok}/{n}",
        "distance_drift_r": round(median_r, 2),
        "level": level,
        "_pooled_rmse": {m: round(v, 3) for m, v in pooled.items()},
        "_folds": folds,
        "_skipped": skipped,
    }


# --- in-sample bins, regularity ------------------------------------------------------------


def _bins(nights: list[Night], key: str) -> dict[int, list[float]]:
    out: dict[int, list[float]] = {}
    for n in nights:
        y = n.outcomes[key]
        if y is not None:
            out.setdefault(int(n.wake // BIN_MIN) * BIN_MIN, []).append(y)
    return {lo: v for lo, v in out.items() if len(v) >= MIN_BIN_NIGHTS}


def in_sample(nights: list[Night], free: frozenset[int], key: str) -> dict | None:
    windows = sorted(n.window for n in nights)
    q1, q3 = _percentile(windows, 0.25), _percentile(windows, 0.75)
    typical = [n for n in nights if q1 <= n.window <= q3]
    bins = _bins(typical, key)
    if len(bins) < 2:
        return None
    stats = {lo: (_mean(v), _se(v), len(v)) for lo, v in bins.items()}
    ranked = sorted(stats, key=lambda lo: stats[lo][0], reverse=True)
    best, runner = ranked[0], ranked[1]
    gap = stats[best][0] - stats[runner][0]
    clear = gap > math.sqrt(stats[best][1] ** 2 + stats[runner][1] ** 2)

    work_bins = _bins([n for n in typical if n.day.weekday() not in free], key)
    work_best = max(work_bins, key=lambda lo: _mean(work_bins[lo])) if work_bins else None

    latest_good = None
    for lo in sorted(stats):
        if lo <= best:
            continue
        combined = math.sqrt(stats[best][1] ** 2 + stats[lo][1] ** 2)
        if stats[lo][0] < stats[best][0] - 2 * combined:
            latest_good = lo
            break
    return {
        "best_lo": best,
        "confidence": "clear" if clear else "weak",
        "weekend_confounded": work_best is not None and work_best != best,
        "latest_good_wake": latest_good,
        "rows": [[_hhmm(lo), stats[lo][2], _r1(stats[lo][0]), _r1(stats[lo][1])]
                 for lo in sorted(stats)],
        "typical_window": [round(q1), round(q3)],
    }


def regularity(nights: list[Night], key: str | None) -> dict | None:
    drifts = [n for n in nights if n.drift is not None]
    if not drifts:
        return None
    out: dict = {"median_drift_min": round(statistics.median(n.drift for n in drifts))}
    if key:
        buckets = (("<30", 0, 30), ("30-60", 30, 60), (">60", 60, 10_000))
        rows = []
        for name, lo, hi in buckets:
            ys = [n.outcomes[key] for n in drifts
                  if lo <= n.drift < hi and n.outcomes[key] is not None]
            if len(ys) >= MIN_BIN_NIGHTS:
                rows.append([name, len(ys), _r1(_mean(ys))])
        out["by_drift"] = rows
    return out


# --- DST -----------------------------------------------------------------------------------


def utc_offset(day: date_type, tz_name: str | None) -> timedelta | None:
    """UTC offset at local noon on `day` (configured zone, else the host's)."""
    noon = datetime.combine(day, time(12))
    if tz_name:
        return noon.replace(tzinfo=ZoneInfo(tz_name)).utcoffset()
    return noon.astimezone().utcoffset()


def dst_transitions(start: date_type, end: date_type, tz_name: str | None) -> list[str]:
    out = []
    prev = utc_offset(start, tz_name)
    d = start + timedelta(days=1)
    while d <= end:
        cur = utc_offset(d, tz_name)
        if cur != prev:
            out.append(d.isoformat())
        prev = cur
        d += timedelta(days=1)
    return out


# --- top level -----------------------------------------------------------------------------


def _pick_outcome(nights: list[Night], outcome: str | None) -> tuple[str | None, str | None]:
    if outcome is not None:
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}, got {outcome!r}")
        candidates = [outcome]
    else:
        candidates = ["readiness_score", "sleep_score"]
    for name in candidates:
        key = OUTCOMES[name]
        if sum(n.outcomes[key] is not None for n in nights) >= MIN_OUTCOME_VALUES:
            return name, key
    return None, None


def _rhythm(nights: list[Night], free: frozenset[int]) -> dict:
    def block(ns: list[Night]) -> dict:
        return {
            "onset": _hhmm(statistics.median(n.onset for n in ns)),
            "wake": _hhmm(statistics.median(n.wake for n in ns)),
            "mid": _hhmm(statistics.median(n.mid for n in ns)),
            "window_min": round(statistics.median(n.window for n in ns)),
            "n": len(ns),
        }

    out = {"all": block(nights)}
    out["all"]["mad_min"] = [round(_mad([n.onset for n in nights])),
                             round(_mad([n.wake for n in nights]))]
    work = [n for n in nights if n.day.weekday() not in free]
    fr = [n for n in nights if n.day.weekday() in free]
    if work:
        out["work"] = block(work)
    if fr:
        out["free"] = block(fr)
    return out


def circadian(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    free_days: list[str] | None = None,
    outcome: str | None = None,
    sleep_need_min: int | None = None,
    detail: bool = False,
    tz_name: str | None = None,
) -> dict:
    d0, d1 = _parse_date(start, "start"), _parse_date(end, "end")
    if d0 > d1:
        raise ValueError(f"start {start} is after end {end}")
    if sleep_need_min is not None and not 240 <= sleep_need_min <= 720:
        raise ValueError("sleep_need_min must be between 240 and 720")
    free = parse_free_days(free_days)

    raw = load_nights(conn, end)
    history, implausible, shifted = clean(raw)
    assign_drift(history)
    nights = [n for n in history if d0 <= n.day <= d1]
    n_impl = sum(d0 <= n.day <= d1 for n in implausible)
    n_shift = sum(d0 <= n.day <= d1 for n in shifted)
    if len(nights) < MIN_NIGHTS:
        raise ValueError(
            f"need at least {MIN_NIGHTS} nights with sleep times in range; found {len(nights)}"
        )

    notes: list[str] = []
    out: dict = {
        "range": {"start": start, "end": end, "nights": len(nights),
                  "excluded": {"implausible": n_impl, "shifted": n_shift}},
    }
    outcome_name, key = _pick_outcome(nights, outcome)
    out["outcome"] = outcome_name
    out["rhythm"] = _rhythm(nights, free)
    dst = dst_transitions(d0, d1, tz_name)
    if dst:
        out["dst_transitions"] = dst
        notes.append(f"Range crosses clock changes ({', '.join(dst)}); estimates may blend by "
                     "up to 30 min. Times use the clock in force at the end date.")

    median_mid = statistics.median(n.mid for n in nights) % 1440
    shift_work = SHIFT_WORK[0] <= median_mid <= SHIFT_WORK[1]

    c, reason = chronotype(nights, free)
    boot = bootstrap(nights, free, sleep_need_min) if c is not None else None
    if c is not None:
        fr = [n for n in nights if n.day.weekday() in free]
        out["chronotype"] = {
            "msf_sc": _hhmm(c.msf_sc),
            "interval_80": [_hhmm(v) for v in boot["msf_sc"]] if boot["msf_sc"] else None,
            "msf": _hhmm(c.msf),
            "msf_median": _hhmm(statistics.median(n.mid for n in fr)),
            "label": label(c.msf_sc),
            "social_jetlag_min": round(abs(c.msf - c.msw)),
        }
        notes.append("Chronotype bands are approximate.")
        if boot["skipped"] > BOOTSTRAP_N * 0.1:
            notes.append(f"{boot['skipped']} of {BOOTSTRAP_N} resamples had too few free "
                         "nights; intervals are unreliable.")
    else:
        out["chronotype"] = None
        notes.append(f"No chronotype: {reason}.")

    # --- sleep need -----------------------------------------------------------
    outcome_need = need_from_outcome(nights, key) if key else None
    need: float | None
    if sleep_need_min is not None:
        need, source = float(sleep_need_min), "user"
    elif c is not None:
        need, source = c.sd_free, "free_night_observed"
    elif outcome_need is not None:
        need, source = outcome_need["shortest_mid"], "outcome_estimate"
    else:
        need, source = None, None
    if need is not None:
        sn: dict = {"primary_min": round(need), "source": source}
        if boot and source != "user" and boot["need"]:
            sn["interval_80"] = [round(v) for v in boot["need"]]
        if outcome_need is not None:
            lo, hi = outcome_need["range"]
            sn["outcome_estimate"] = [lo, hi]
            sn["agreement"] = "yes" if lo - 15 <= need <= hi + 15 else "no"
            if sn["agreement"] == "no":
                notes.append("Free-night sleep falls outside the range where your Garmin scores "
                             "level off; consider passing sleep_need_min.")
        if c is not None:
            sn["catch_up_inflated"] = c.sd_free - c.sd_work > 60
            if sn["catch_up_inflated"]:
                sn["sd_week_min"] = round(c.sd_week)
                notes.append("Free nights run over an hour longer than work nights, so they likely "
                             "overstate need; consider passing sleep_need_min.")
        out["sleep_need"] = sn
    else:
        out["sleep_need"] = None

    if shift_work:
        out["wake_window"] = None
        out["evidence"] = None
        out["recommendation"] = None
        notes.append("Median mid-sleep falls in the daytime; this method assumes nighttime main "
                     "sleep, so no wake window is given.")
        out["notes"] = notes
        return out

    # --- wake window (inference) ------------------------------------------------
    cw = None
    if c is not None and need is not None:
        cw = c.msf_sc + need / 2
        out["wake_window"] = {
            "inferred": True,
            "circadian_wake": _hhmm(cw),
            "interval_80": [_hhmm(v) for v in boot["wake"]] if boot["wake"] else None,
        }
        notes.append("The wake window is inferred from MSFsc and sleep need, not an MCTQ "
                     "output. Alarms on free days would make it earlier than your body clock.")
    else:
        out["wake_window"] = None

    sens = sensitivity(raw, d1, free, sleep_need_min) if c is not None else None
    if sens is not None:
        out["stability"] = {k: sens[k] for k in ("level", "msf_sc_range", "wake_range",
                                                  "label_stable", "most_sensitive_to")}
        if sens["level"] == "low":
            lo, hi = sens["wake_range"]
            notes.append(f"Under other reasonable settings the wake estimate ranges {lo} to {hi}, "
                         f"most affected by the {sens['most_sensitive_to']} choice. If your "
                         "schedule changed, set start to a date after the change.")

    # --- evidence -----------------------------------------------------------------
    ins = in_sample(nights, free, key) if key else None
    ev = None
    if key and cw is not None:
        ev = evidence(history, d1, free, sleep_need_min, key)
    ev_out = None
    if ev is not None:
        ev_out = {k: v for k, v in ev.items() if not k.startswith("_")}
        if ev.get("status") == "ok":
            notes.append("Evidence is an association with Garmin's scores, not physiology; "
                         "levels describe consistency on unseen nights, not significance.")
            if (ev["distance_survives_regularity"] is False
                    and ev["level"] in ("supportive", "consistent")):
                notes.append("Waking near your window goes with higher Garmin scores, but that "
                             "may mostly reflect keeping a regular schedule.")
            elif ev["distance_survives_regularity"] == "indeterminate":
                notes.append("Distance from your window and schedule regularity move together "
                             "too closely to separate.")
                if ev["level"] == "consistent":
                    ev_out["level"] = ev["level"] = "supportive"
    if ins is not None:
        ins_out = {
            "observed_best_wake": f"{_hhmm(ins['best_lo'])}-{_hhmm(ins['best_lo'] + BIN_MIN)}",
            "confidence": ins["confidence"],
            "weekend_confounded": ins["weekend_confounded"],
        }
        if ins["latest_good_wake"] is not None:
            ins_out["latest_good_wake"] = _hhmm(ins["latest_good_wake"])
        ev_out = {**(ev_out or {}), "in_sample": ins_out}
    out["evidence"] = ev_out

    # --- reconcile -------------------------------------------------------------------
    level = ev.get("level") if ev and ev.get("status") == "ok" else None
    obs_mid = ins["best_lo"] + BIN_MIN / 2 if ins else None
    lgw = ins["latest_good_wake"] if ins else None
    experiment = None
    window = None
    if cw is not None and obs_mid is not None and abs(obs_mid - cw) <= NEAR_MIN:
        agreement = "agree"
    elif cw is not None and obs_mid is not None and (
        ins["confidence"] == "clear" or level in ("supportive", "consistent")
    ):
        agreement = "disagree"
        centre = (cw + obs_mid) / 2
        experiment = {
            "window": [_hhmm(centre - WINDOW_HALF_MIN), _hhmm(centre + WINDOW_HALF_MIN)],
            "nights": 21,
            "how": f"Wake inside this window for 21 nights, then compare {outcome_name} for "
                   "those nights with the 21 before (query_metrics with stats=True).",
        }
    elif cw is not None:
        agreement = "formula_only"
    elif obs_mid is not None and ins["confidence"] == "clear":
        agreement = "data_only"
    else:
        agreement = "none"

    if agreement in ("agree", "disagree", "formula_only"):
        window = [cw - WINDOW_HALF_MIN, cw + WINDOW_HALF_MIN]
    elif agreement == "data_only":
        window = [float(ins["best_lo"]), float(ins["best_lo"] + BIN_MIN)]
    # The in-sample late-wake limit only trims a window the data already backs;
    # it never drags the inference toward the data (that is what `disagree` is for).
    if agreement in ("agree", "data_only") and lgw is not None and window[1] > lgw:
        window[1] = float(lgw)
        window[0] = min(window[0], window[1] - 2 * WINDOW_HALF_MIN)

    rec: dict = {"agreement": agreement}
    if window is not None:
        rec["wake_window"] = [_hhmm(window[0]), _hhmm(window[1])]
        if need is not None:
            bedtime = window[0] - need
            rec["bedtime"] = _hhmm(bedtime)
            rec["lights_out"] = _hhmm(bedtime - LIGHTS_OUT_MIN)
    if lgw is not None:
        rec["latest_good_wake"] = _hhmm(lgw)
    if experiment is not None:
        rec["recommended_experiment"] = experiment
    out["recommendation"] = rec

    reg = regularity(nights, key)
    if reg is not None:
        out["regularity"] = reg

    out["interpretation"] = interpretation(out, cw, obs_mid, ev, outcome_name)
    out["notes"] = notes

    if detail:
        out["detail"] = _detail(boot, sens, ev, ins)
    return out


def interpretation(out: dict, cw, obs_mid, ev, outcome_name) -> str:
    parts = []
    agreement = out["recommendation"]["agreement"]
    if cw is not None:
        parts.append(f"Your chronotype suggests waking about {_hhmm(cw)}.")
    if obs_mid is not None:
        best = out["evidence"]["in_sample"]["observed_best_wake"]
        parts.append(f"Your past Garmin scores were highest when waking {best}.")
    if agreement == "agree":
        parts.append("These agree within 30 minutes.")
    elif agreement == "disagree":
        parts.append("These differ by more than 30 minutes; the suggested experiment tests "
                     "the difference.")
    if ev and ev.get("status") == "ok":
        effect = ev["effect_within_window"]
        if effect is not None:
            direction = "higher" if effect >= 0 else "lower"
            label_txt = (outcome_name or "").replace("_score", "").replace("_", " ")
            text = (f"On unseen nights, waking within 30 minutes of the window went with "
                    f"{label_txt} scores {abs(effect):.1f} points {direction} than sleep duration "
                    "alone predicts")
            survives = ev["distance_survives_regularity"]
            if effect > 0 and survives is True:
                text += ", and the pattern held after accounting for schedule regularity"
            elif effect > 0 and survives is False:
                text += ", though schedule regularity may explain much of it"
            parts.append(f"{text} (evidence: {ev['level']}).")
    elif ev and ev.get("status") == "insufficient_history":
        parts.append(f"Out-of-sample evidence needs {MIN_HISTORY_DAYS} days of history.")
    return " ".join(parts)


def _detail(boot, sens, ev, ins) -> dict:
    out: dict = {}
    if boot:
        fs = boot["free_share"]
        out["bootstrap"] = {
            "resamples": BOOTSTRAP_N,
            "skipped": boot["skipped"],
            "free_share": {"original": round(fs["original"], 3),
                           "resampled_mean": round(fs["resampled_mean"], 3),
                           "resampled_80": [round(v, 3) for v in fs["resampled_80"]]},
        }
    if sens:
        out["sensitivity"] = {
            "cols": ["window", "filter", "msf", "msf_sc", "wake", "label"],
            "rows": [[r["window"], r["filter"], r["msf"], _hhmm(r["msf_sc"]), _hhmm(r["wake"]),
                      r["label"]] for r in sens["runs"]],
        }
    if ev and ev.get("status") == "ok":
        out["models_rmse"] = ev["_pooled_rmse"]
        out["distance_sign_consistent_folds"] = ev["_distance_sign_consistent_folds"]
        out["folds"] = {
            "cols": ["train_start", "test_start", "n_train", "n_test", "rmse_a", "rmse_d",
                     "rmse_f", "rmse_g", "dist_drift_r"],
            "rows": [[f.train_start.isoformat(), f.test_start.isoformat(), f.n_train, f.n_test,
                      round(f.rmse["A"], 2), round(f.rmse["D"], 2), round(f.rmse["F"], 2),
                      round(f.rmse["G"], 2),
                      None if f.dist_drift_r is None else round(f.dist_drift_r, 2)]
                     for f in ev["_folds"]],
        }
        if ev["_skipped"]:
            out["folds_skipped"] = ev["_skipped"]
        out["folds_skipped_n"] = len(ev["_skipped"])
    if ins:
        out["wake_bins"] = {"cols": ["wake_bin", "n", "mean", "se"], "rows": ins["rows"],
                            "typical_window_min": ins["typical_window"]}
    return out
