"""circadian tool tests over small synthetic stores built inline.

Each store is a run of consecutive nights from BASE (a Monday). A `timing`
function gives (onset, wake) in minutes relative to midnight of the wake date,
and an optional `outcome` function gives the night's Garmin score, written as
both sleep score and readiness unless readiness is switched off.
"""

from __future__ import annotations

import json
import random
import time as time_mod
from datetime import date, datetime, time, timedelta

import pytest

from garmin_mcp import analysis, circadian, db, demo

BASE = date(2025, 1, 6)  # Monday
END_OF = lambda days: (BASE + timedelta(days=days - 1)).isoformat()  # noqa: E731


def _ts(day: date, minutes: float) -> str:
    return (datetime.combine(day, time()) + timedelta(minutes=round(minutes))).isoformat()


def build(conn, days, timing, outcome=None, readiness=True):
    for i in range(days):
        d = BASE + timedelta(days=i)
        t = timing(i, d)
        if t is None:
            continue
        onset, wake = t
        row = {
            "date": d.isoformat(),
            "start_ts": _ts(d, onset),
            "end_ts": _ts(d, wake),
            "duration_min": float(wake - onset),
            "source": "api",
        }
        if outcome is not None:
            y = outcome(i, d, onset, wake)
            if y is not None:
                row["score"] = round(y)
                if readiness:
                    db.upsert(conn, "performance", {"date": d.isoformat(),
                                                    "readiness_score": round(y)}, ("date",))
        db.upsert(conn, "sleep", row, ("date",))


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "garmin.db")


def is_free(d: date) -> bool:
    return d.weekday() >= 5


def run(conn, days, **kwargs):
    kwargs.setdefault("tz_name", "UTC")
    return circadian.circadian(conn, BASE.isoformat(), END_OF(days), **kwargs)


def mctq_example(i, d):
    # Work 23:30-06:30 (7 h); free 00:00-08:30 (8.5 h).
    return (0, 510) if is_free(d) else (-30, 390)


# --- MCTQ core ------------------------------------------------------------------------


def test_mctq_worked_example(conn):
    build(conn, 56, mctq_example)
    out = run(conn, 56)
    assert out["chronotype"]["msf"] == "04:15"
    assert out["chronotype"]["msf_sc"] == "03:43"


def test_no_correction_when_free_nights_are_not_longer(conn):
    build(conn, 56, lambda i, d: (0, 420) if is_free(d) else (-60, 420))
    out = run(conn, 56)
    assert out["chronotype"]["msf_sc"] == out["chronotype"]["msf"] == "03:30"


def test_custom_free_days(conn):
    # Late wakes on Friday and Saturday mornings only.
    build(conn, 56, lambda i, d: (30, 510) if d.weekday() in (4, 5) else (-30, 420))
    default = run(conn, 56)
    custom = run(conn, 56, free_days=["fri", "sat"])
    assert custom["chronotype"]["msf"] == "04:30"
    assert default["chronotype"]["msf"] != custom["chronotype"]["msf"]


def test_identity_wake_equals_mean_free_wake(conn):
    # Equal windows, so no debt correction: circadian wake = mean free-night wake.
    build(conn, 56, lambda i, d: (0, 450) if is_free(d) else (-60, 390))
    out = run(conn, 56)
    assert out["wake_window"]["circadian_wake"] == "07:30"
    assert out["rhythm"]["free"]["wake"] == "07:30"


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(209, "early"), (210, "intermediate"), (270, "intermediate"), (271, "late")],
)
def test_chronotype_labels(minutes, expected):
    assert circadian.label(minutes) == expected


def test_median_msf_reported(conn):
    def timing(i, d):
        if i == 26:  # one free night 100 min late (inside the travel filter)
            return (100, 550)
        return (0, 450) if is_free(d) else (-60, 390)

    build(conn, 56, timing)
    ch = run(conn, 56)["chronotype"]
    assert ch["msf_median"] == "03:45"
    assert ch["msf"] != "03:45"


# --- sleep need ---------------------------------------------------------------------------


def test_sleep_need_priority(conn):
    build(conn, 56, mctq_example)
    assert run(conn, 56)["sleep_need"]["source"] == "free_night_observed"
    user = run(conn, 56, sleep_need_min=480)["sleep_need"]
    assert user["source"] == "user" and user["primary_min"] == 480


def test_sleep_need_falls_back_to_outcome_without_chronotype(conn):
    rng = random.Random(3)

    def timing(i, d):
        if is_free(d):
            return None  # no free nights, so no chronotype
        w = rng.choice([390, 420, 450, 480])
        return (420 - w, 420)

    build(conn, 120, timing, outcome=lambda i, d, o, w: 70 + (10 if 450 <= w - o < 480 else 0))
    out = run(conn, 120)
    assert out["chronotype"] is None
    assert out["sleep_need"]["source"] == "outcome_estimate"
    assert out["sleep_need"]["primary_min"] == 465


def test_sleep_need_agreement_yes_and_no(tmp_path):
    rng = random.Random(4)

    def timing_for(free_window):
        def timing(i, d):
            if is_free(d):
                return (480 - free_window, 480)
            w = rng.choice([420, 450, 480])
            return (390 - w, 390)
        return timing

    def peak(o, w):
        return 85 if 450 <= w - o < 480 else 75

    yes = db.connect(tmp_path / "yes.db")
    build(yes, 84, timing_for(465), outcome=lambda i, d, o, w: peak(o, w))
    assert run(yes, 84)["sleep_need"]["agreement"] == "yes"
    no = db.connect(tmp_path / "no.db")
    build(no, 84, timing_for(560), outcome=lambda i, d, o, w: peak(o, w))
    assert run(no, 84)["sleep_need"]["agreement"] == "no"


def test_catch_up_flag(conn):
    build(conn, 56, lambda i, d: (0, 495) if is_free(d) else (-30, 390))  # 75 min longer
    sn = run(conn, 56)["sleep_need"]
    assert sn["catch_up_inflated"] is True
    assert "sd_week_min" in sn


# --- uncertainty and sensitivity ----------------------------------------------------------


def noisy(seed):
    rng = random.Random(seed)

    def timing(i, d):
        base = 480 if is_free(d) else 390
        wake = base + rng.gauss(0, 25)
        return (wake - 450 + rng.gauss(0, 20), wake)

    return timing


def test_bootstrap_is_deterministic(conn):
    build(conn, 84, noisy(1))
    assert run(conn, 84)["chronotype"]["interval_80"] == run(conn, 84)["chronotype"]["interval_80"]


def _width(interval):
    lo, hi = (int(t[:2]) * 60 + int(t[3:]) for t in interval)
    return hi - lo


def test_interval_contains_estimate_and_narrows(tmp_path):
    short = db.connect(tmp_path / "s.db")
    build(short, 56, noisy(2))
    long = db.connect(tmp_path / "l.db")
    build(long, 224, noisy(2))
    a, b = run(short, 56)["chronotype"], run(long, 224)["chronotype"]
    for ch in (a, b):
        assert ch["interval_80"][0] <= ch["msf_sc"] <= ch["interval_80"][1]
    assert _width(b["interval_80"]) < _width(a["interval_80"])


def test_week_blocks_keep_free_share(conn):
    build(conn, 84, noisy(3))
    raw = circadian.load_nights(conn, END_OF(84))
    kept, _, _ = circadian.clean(raw)
    boot = circadian.bootstrap(kept, frozenset({5, 6}), None)
    assert boot["skipped"] == 0
    fs = boot["free_share"]
    assert abs(fs["resampled_mean"] - fs["original"]) < 0.03


def test_sensitivity_flags_a_schedule_change(conn):
    def timing(i, d):
        shift = 90 if i >= 200 else 0
        base = (480 if is_free(d) else 390) + shift
        return (base - 450, base)

    build(conn, 365, timing)
    out = run(conn, 365)
    assert out["stability"]["level"] == "low"
    assert out["stability"]["most_sensitive_to"] == "window"
    assert any("set start to a date after the change" in n for n in out["notes"])


def test_sensitivity_stable_store(conn):
    build(conn, 365, lambda i, d: (30, 480) if is_free(d) else (-60, 390))
    st = run(conn, 365)["stability"]
    assert st["level"] == "high" and st["label_stable"] is True


# --- evidence -----------------------------------------------------------------------------


def evidence_store(conn, seed, outcome_fn, days=300, timing=None):
    rng = random.Random(seed)
    nights = {}

    def default_timing(i, d):
        base = 480 if is_free(d) else 400
        wake = base + rng.gauss(0, 30)
        return (wake - 450 + rng.gauss(0, 20), wake)

    def t(i, d):
        nights[i] = (timing or default_timing)(i, d)
        return nights[i]

    build(conn, days, t, outcome=lambda i, d, o, w: outcome_fn(i, d, o, w, rng))
    return days


def test_evidence_true_positive(conn):
    # Score falls with distance from 08:00 (the store's circadian wake), not duration.
    days = evidence_store(conn, 5, lambda i, d, o, w, rng: 85 - 0.15 * abs(w - 480)
                          + rng.gauss(0, 3))
    ev = run(conn, days)["evidence"]
    assert ev["status"] == "ok"
    assert ev["level"] in ("supportive", "consistent")
    assert ev["distance_survives_regularity"] is True


def test_evidence_duration_only_confound(conn):
    # Later wakes are longer nights; the score depends on duration only.
    def timing_fn(rng):
        def timing(i, d):
            wake = (480 if is_free(d) else 400) + rng.gauss(0, 30)
            return (-60 + rng.gauss(0, 10), wake)
        return timing

    rng = random.Random(6)
    build(conn, 300, timing_fn(rng),
          outcome=lambda i, d, o, w: 60 + 0.08 * (w - o) + rng.gauss(0, 3))
    ev = run(conn, 300)["evidence"]
    assert ev["level"] in ("unsupported", "suggestive")


def test_evidence_false_positive_rate(tmp_path):
    consistent = 0
    for seed in range(20):
        conn = db.connect(tmp_path / f"null{seed}.db")
        evidence_store(conn, 100 + seed, lambda i, d, o, w, rng: 80 + rng.gauss(0, 5))
        consistent += run(conn, 300)["evidence"]["level"] == "consistent"
    assert consistent <= 1


def test_fold_uses_training_nights_only(conn, monkeypatch):
    evidence_store(conn, 7, lambda i, d, o, w, rng: 85 - 0.1 * abs(w - 480) + rng.gauss(0, 3))
    history, _, _ = circadian.clean(circadian.load_nights(conn, END_OF(300)))
    circadian.assign_drift(history)
    t0 = history[0].day
    t1 = t0 + timedelta(days=circadian.TRAIN_DAYS)
    t2 = t1 + timedelta(days=circadian.TEST_DAYS)
    train = [n for n in history if t0 <= n.day < t1]
    test = [n for n in history if t1 <= n.day < t2]
    free = frozenset({5, 6})

    seen = []
    real = circadian.chronotype
    monkeypatch.setattr(circadian, "chronotype",
                        lambda ns, f, use_median=False: (seen.append(ns), real(ns, f))[1])
    fold = circadian._run_fold(train, test, free, None, "readiness", t0, t1)
    assert all(n.day < t1 for ns in seen for n in ns)

    # Perturbing test nights changes nothing that was fitted (scaler, coefficients).
    for n in test:
        n.wake += 45
        n.outcomes["readiness"] = 0
    again = circadian._run_fold(train, test, free, None, "readiness", t0, t1)
    assert again.dist_coef_d == fold.dist_coef_d
    assert again.dist_coef_f == fold.dist_coef_f
    # Every model is scored on the same nights.
    assert len({len(v) for v in fold.sq_err.values()}) == 1


def test_drift_uses_only_earlier_nights(conn):
    build(conn, 60, noisy(8))
    history, _, _ = circadian.clean(circadian.load_nights(conn, END_OF(60)))
    circadian.assign_drift(history)
    before = [n.drift for n in history[:40]]
    history[45].wake += 100
    history[45].onset += 100
    circadian.assign_drift(history)
    assert [n.drift for n in history[:40]] == before


def test_insufficient_history(conn):
    build(conn, 150, noisy(9), outcome=lambda i, d, o, w: 80)
    ev = run(conn, 150)["evidence"]
    assert ev["status"] == "insufficient_history"


def test_in_sample_never_sets_the_level(conn, monkeypatch):
    evidence_store(conn, 10, lambda i, d, o, w, rng: 80 + rng.gauss(0, 5))
    plain = run(conn, 300)["evidence"]["level"]
    fake = {"best_lo": 390, "confidence": "clear", "weekend_confounded": False,
            "latest_good_wake": None, "rows": [], "typical_window": [400, 500]}
    monkeypatch.setattr(circadian, "in_sample", lambda *a, **k: fake)
    out = run(conn, 300)
    assert out["evidence"]["level"] == plain
    assert "level" not in out["evidence"]["in_sample"]


def _disrupted(seed, outcome):
    """Regular nights at 08:00 plus random disrupted nights shifted by 60-150 min."""
    rng = random.Random(seed)
    jumps = {}

    def timing(i, d):
        jump = rng.choice([-1, 1]) * rng.uniform(60, 150) if rng.random() < 0.3 else 0.0
        jumps[i] = jump
        wake = 480 + jump + rng.gauss(0, 10)
        return (wake - 450, wake)

    return timing, lambda i, d, o, w: outcome(i, jumps[i], w, rng)


def test_regularity_only_confound(conn):
    # Disrupted nights are both irregular and far from the window; only drift matters.
    timing, outcome = _disrupted(11, lambda i, jump, w, rng: 85 - 0.12 * abs(jump)
                                 + rng.gauss(0, 3))
    build(conn, 300, timing, outcome=outcome)
    ev = run(conn, 300)["evidence"]
    assert ev["distance_survives_regularity"] in (False, "indeterminate")
    assert ev["level"] != "consistent"


def test_collinear_distance_and_drift_is_indeterminate(conn):
    timing, outcome = _disrupted(12, lambda i, jump, w, rng: 80 + rng.gauss(0, 4))
    build(conn, 300, timing, outcome=outcome)
    ev = run(conn, 300)["evidence"]
    assert ev["distance_drift_r"] > circadian.COLLINEAR_R
    assert ev["distance_survives_regularity"] == "indeterminate"


def test_distance_survives_regularity(conn):
    # Drift and distance both matter, and vary independently.
    rng = random.Random(13)
    state = {}

    def timing(i, d):
        base = 480 if is_free(d) else 420
        wake = base + rng.gauss(0, 35)
        state[i] = wake
        return (wake - 450, wake)

    build(conn, 300, timing,
          outcome=lambda i, d, o, w: 90 - 0.15 * abs(w - 480) + rng.gauss(0, 3))
    ev = run(conn, 300)["evidence"]
    assert ev["distance_survives_regularity"] is True


# --- reconcile and guards ---------------------------------------------------------------


def test_reconcile_formula_only_without_outcomes(conn):
    build(conn, 56, mctq_example)
    rec = run(conn, 56)["recommendation"]
    assert rec["agreement"] == "formula_only"
    assert rec["wake_window"] == ["07:43", "08:13"]  # 03:43 + 510 / 2 = 07:58, +- 15


def test_reconcile_none(conn):
    build(conn, 60, lambda i, d: None if is_free(d) else (-60, 390))
    assert run(conn, 60)["recommendation"]["agreement"] == "none"


def test_reconcile_data_only(conn):
    rng = random.Random(14)

    def timing(i, d):
        if is_free(d):
            return None
        wake = rng.choice([360, 390, 420, 450, 480])
        return (wake - 450, wake)

    build(conn, 140, timing, outcome=lambda i, d, o, w: 90 if 390 <= w < 420 else 70)
    rec = run(conn, 140)["recommendation"]
    assert rec["agreement"] == "data_only"
    assert rec["wake_window"] == ["06:30", "07:00"]


def test_reconcile_disagree_keeps_both(conn):
    # Body clock says 08:30; the scores clearly favour 06:30-07:00.
    rng = random.Random(15)

    def timing(i, d):
        wake = 510 + rng.gauss(0, 15) if is_free(d) else rng.choice([390, 420, 450, 480])
        return (wake - 450, wake)

    build(conn, 140, timing, outcome=lambda i, d, o, w: 90 - 0.3 * abs(w - 390)
          + rng.gauss(0, 1))
    out = run(conn, 140)
    rec = out["recommendation"]
    assert rec["agreement"] == "disagree"
    cw = out["wake_window"]["circadian_wake"]
    # The window stays on the inference; the experiment sits between the two.
    centre = int(cw[:2]) * 60 + int(cw[3:])
    assert rec["wake_window"] == [circadian._hhmm(centre - 15), circadian._hhmm(centre + 15)]
    assert rec["recommended_experiment"]["window"][0] < cw


def test_reconcile_agree(conn):
    rng = random.Random(16)

    def timing(i, d):
        wake = (480 if is_free(d) else 420) + rng.gauss(0, 30)
        return (wake - 450, wake)

    build(conn, 140, timing, outcome=lambda i, d, o, w: 90 - 0.2 * abs(w - 480)
          + rng.gauss(0, 1))
    assert run(conn, 140)["recommendation"]["agreement"] == "agree"


def test_travel_filter(conn):
    build(conn, 60, lambda i, d: (-60 + 540, 390 + 540) if 30 <= i < 39 else (-60, 390))
    out = run(conn, 60)
    assert out["range"]["excluded"]["shifted"] == 9
    assert out["range"]["nights"] == 51


def test_shift_work_guard(conn):
    build(conn, 56, lambda i, d: (480, 900))  # asleep 08:00-15:00
    out = run(conn, 56)
    assert out["wake_window"] is None and out["evidence"] is None
    assert any("nighttime main sleep" in n for n in out["notes"])


def test_outcome_falls_back_to_sleep_score(conn):
    build(conn, 56, mctq_example, outcome=lambda i, d, o, w: 80, readiness=False)
    assert run(conn, 56)["outcome"] == "sleep_score"


def test_too_few_nights(conn):
    build(conn, 20, mctq_example)
    with pytest.raises(ValueError, match="at least 28 nights"):
        run(conn, 20)


def test_too_few_free_nights(conn):
    keep_free = {5, 6, 12}  # three free nights only
    build(conn, 60, lambda i, d: None if is_free(d) and i not in keep_free else (-60, 390))
    out = run(conn, 60)
    assert out["chronotype"] is None
    assert any("only 3 free nights" in n for n in out["notes"])


def test_low_free_night_share(conn):
    # 12 free nights among ~120: above the 8-night floor, under half the expected share.
    free_seen = []

    def timing(i, d):
        if is_free(d):
            if len(free_seen) >= 12:
                return None
            free_seen.append(i)
        return (-60, 390)

    build(conn, 140, timing)
    out = run(conn, 140)
    assert out["chronotype"] is None
    assert any("under half the expected" in n for n in out["notes"])


def test_dst_transitions(conn):
    def timing(i, d):
        return (-60, 390)

    build(conn, 120, timing)
    crossing = circadian.circadian(conn, "2025-02-01", "2025-05-05", tz_name="America/New_York")
    assert crossing["dst_transitions"] == ["2025-03-09"]
    inside = circadian.circadian(conn, "2025-03-15", "2025-05-05", tz_name="America/New_York")
    assert "dst_transitions" not in inside


# --- plumbing -----------------------------------------------------------------------------


def test_registry_timing_metrics(conn):
    build(conn, 3, lambda i, d: (-60, 390))
    out = analysis.query_metrics(conn, ["sleep_onset_min", "wake_time_min", "mid_sleep_min"],
                                 BASE.isoformat(), END_OF(3))
    assert out["rows"][0][1:] == [-60.0, 390.0, 165.0]


@pytest.fixture(scope="module")
def demo_year(tmp_path_factory):
    conn = db.connect(tmp_path_factory.mktemp("demo") / "garmin.db")
    report = demo.generate(conn, days=365, end="2026-08-02")
    return conn, report


def test_output_size(demo_year):
    conn, report = demo_year
    start = "2026-02-04"
    plain = circadian.circadian(conn, start, report["end"], tz_name="UTC")
    full = circadian.circadian(conn, start, report["end"], detail=True, tz_name="UTC")
    assert len(json.dumps(plain)) < 3072
    assert len(json.dumps(full)) < 6144


def test_performance(demo_year):
    conn, report = demo_year
    t = time_mod.perf_counter()
    circadian.circadian(conn, "2026-02-04", report["end"], detail=True, tz_name="UTC")
    assert time_mod.perf_counter() - t < 5


def test_wording_rule(tmp_path, demo_year):
    outputs = [circadian.circadian(demo_year[0], "2026-02-04", "2026-08-02")]
    for name, fn in [("mctq", mctq_example), ("noisy", noisy(20))]:
        conn = db.connect(tmp_path / f"{name}.db")
        build(conn, 300, fn, outcome=lambda i, d, o, w: 80 - 0.1 * abs(w - 450))
        outputs.append(run(conn, 300))
    for out in outputs:
        text = " ".join([out.get("interpretation") or "", *out["notes"]]).lower()
        for word in circadian.BANNED_WORDS:
            assert word not in text, f"{word!r} in generated text"
