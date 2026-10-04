# Plan: `circadian` tool (garmin-local-mcp v0.1.8)

**Status:** implemented for 0.1.8 (implementation notes in section 16). Public copy: personal health data removed
**Author:** Anup Shesh (with Claude)
**Date:** 2026-10-04
**Target release:** 0.1.8

---

## 1. Summary

Add a 13th MCP tool, `circadian`. It produces three separate quantities from the sleep timestamps already in the local store, each with its own method and uncertainty:

1. **Chronotype** (`MSFsc`): the Munich Chronotype Questionnaire (MCTQ) formula, mid-sleep on free days corrected for sleep debt. This is the validated, published output.
2. **Sleep need**: a separate estimate with its own source and interval.
3. **Circadian-compatible wake window**: derived from the first two. It is clearly labelled as an *inference*, not an MCTQ output.

A fourth block, **evidence**, tests whether that inference is associated with higher **Garmin outcome scores** (readiness or sleep score). It checks on *unseen* nights (rolling holdout), against simpler baseline models, and controls for sleep duration and sleep regularity. The result is an explicit evidence level. What it can show is an association between wake timing and Garmin's scores after those controls. It cannot show that wake timing changes physiology, because Garmin's scores are not independent physiological ground truth (section 5.6). Stability under reasonable changes of assumption is reported too.

The tool also adds three sleep-timing metrics to the registry, so the existing tools can work with bedtime and wake time.

No schema change, no new sync endpoint, standard library only.

## 2. Motivation

- **Users ask this.** "When should I wake up?" and "Am I a night owl?" are common questions that can't currently be answered. `sleep.start_ts` and `sleep.end_ts` are stored but not exposed, and `query_metrics` rejects them.
- **It fits the project's position.** The project's edge is server-side analysis over long local history. A chronotype estimate needs months of nights, and an out-of-sample check needs more. A live API wrapper has neither.
- **Rigour is the differentiator, not more signals.** Consumer sleep apps give a point estimate with no uncertainty and no validation. This tool's value is that it says how sure it is, whether the answer holds under different assumptions, and whether it predicted anything on nights it hadn't seen.
- **Checked on real data.** Validated against a private maintainer dataset spanning 400+ nights. Personal health metrics are intentionally omitted from the public repository.

## 3. Data available

| Column | Source | Notes |
|---|---|---|
| `sleep.date` | API + FIT | the **wake date** |
| `sleep.start_ts` | API (`sleepStartTimestampLocal`), FIT (tz-corrected) | local wall-clock, ISO, no offset |
| `sleep.end_ts` | same | local wall-clock |
| `sleep.duration_min` | both | time asleep, excluding awake minutes |
| `sleep.awake_min` | both | |
| `sleep.score`, `performance.readiness_score`, `daily_wellness.body_battery_high` | various | outcome candidates (all 0–100) |

Because timestamps are local wall-clock, daylight-saving changes and travel show up as real shifts in clock time. That is correct for a "what time should I set my alarm" question. Step 1b covers DST and step 0.3 covers travel.

## 4. Definitions

All times are **minutes relative to midnight of the wake date**, so a 23:00 onset is `-60` and a 06:30 wake is `390`. This avoids wrap-around arithmetic.

| Term | Definition |
|---|---|
| Onset | `start_ts` |
| Wake | `end_ts` |
| Sleep window (SD) | `end_ts - start_ts`, in minutes. **The MCTQ defines sleep duration as onset to wake**, so all MCTQ math and the sleep-need estimate use the window, not `duration_min` |
| Mid-sleep (MS) | `onset + SD / 2` |
| Free night | a night whose **wake date** falls on a free day. Default free days are Saturday and Sunday, so the free nights are Friday and Saturday nights, which matches the MCTQ's "sleep before a free day" |
| Work night | every other night |
| Week block | the ISO week of the wake date; the resampling unit for all bootstraps (section 5.3, step 5) |

## 5. Design

### 5.1 New registry metrics (`metrics.py`)

One line each, computed in SQL over the `sleep` table:

| Metric | Expression |
|---|---|
| `sleep_onset_min` | `(julianday(start_ts) - julianday(date)) * 1440` |
| `wake_time_min` | `(julianday(end_ts) - julianday(date)) * 1440` |
| `mid_sleep_min` | average of the two |

All three are numeric, so `correlate(wake_time_min, readiness_score)`, `baselines(["sleep_onset_min"])` and `anomalies(["mid_sleep_min"])` work with no further code.

### 5.2 Tool signature (`server.py`)

```python
@mcp.tool()
@_tool_errors
def circadian(
    start: str | None = None,            # default: end minus 179 days
    end: str | None = None,              # default: yesterday
    free_days: list[str] | None = None,  # default ["sat", "sun"]
    outcome: str | None = None,          # default readiness_score, else sleep_score
    sleep_need_min: int | None = None,   # override the estimated sleep need
    detail: bool = False,                # add bins, sensitivity grid, fold table
) -> dict:
    """Chronotype (MCTQ MSFsc), sleep need, and an inferred circadian-compatible
    wake window, each with an 80% interval; plus out-of-sample evidence on
    whether waking near that window is associated with higher Garmin outcome
    scores (readiness or sleep score), beyond sleep duration and regularity.
    Evidence levels describe consistency on unseen nights, not statistical
    significance. Default: last 180 days.
    """
```

The logic lives in a new module, `circadian.py`, as pure functions on a `sqlite3.Connection`, like `analysis.py`. `analysis.py` is already 560 lines, and this feature brings its own helpers: bootstrap, small least-squares fits, rolling folds.

Results are **deterministic**. Every random draw comes from `random.Random(0)`, so the same store gives the same answer every call, which matters for an assistant comparing two calls and for tests.

### 5.3 Algorithm

**Step 0: load and clean**
1. Load nights in range that have both timestamps.
2. Drop implausible rows: window under 120 min or over 900 min, or onset outside −12 h to +12 h of midnight. Count them in `excluded.implausible`.
3. **Travel and disruption filter:** drop any night whose mid-sleep is more than **180 min** from the median mid-sleep of the previous 7 kept nights (only when at least 4 exist). Count them in `excluded.shifted`. The threshold is a parameter of the internal function so step 6 can vary it.
4. If fewer than **28** nights remain, return `{"error": "need at least 28 nights with sleep times; found N"}`.

**Step 1: describe the rhythm**
- Median onset, wake, mid-sleep and window, overall and split into work and free nights.
- Spread is reported as MAD (median absolute deviation) in minutes.
- The median free-night mid-sleep is reported alongside the mean-based MSF, so a reader can see when outlier nights pull the mean.

**Step 1b: daylight-saving check**
- The store has local wall-clock times with no UTC offset, so transitions can't be read from the data. Instead, use the host machine's timezone rules through one helper, `_host_utc_offset(date)`, which wraps `datetime(y, m, d, 12).astimezone().utcoffset()` and is patchable in tests. Record any date where the offset changes.
- Assumption, stated in `notes`: the server's machine is in the watch's home timezone. Where there's no DST, nothing is reported.
- If transitions fall in range: `dst_transitions: [...]` plus a note. The note says nights either side are an hour apart in clock time, so estimates can blend by up to 30 min, and that answers are in the clock in force at `end`.
- No adjustment is applied. After a clock change, alarms and routines move with the clock, so shifting earlier nights would misstate behaviour. The step 0.3 filter deliberately doesn't catch a 60-min DST step.

**Step 2: shift-work guard**
- If the overall median mid-sleep falls between **08:00 and 20:00**, return the rhythm and chronotype with `wake_window: null`, `evidence: null`, and a note that the method assumes nighttime main sleep.

**Step 3: chronotype (MCTQ), output 1**

```
MSF      = mean mid-sleep over free nights
MSW      = mean mid-sleep over work nights
SD_f     = mean window over free nights
SD_w     = mean window over work nights
SD_week  = (n_work_days * SD_w + n_free_days * SD_f) / 7
           where n_free_days = len(free_days), n_work_days = 7 - n_free_days

MSFsc    = MSF - (SD_f - SD_week) / 2    if SD_f > SD_w
         = MSF                           otherwise   (MCTQ rule)

social_jetlag_min = |MSF - MSW|
```

- **Free-night minimum (both required):** at least **8 free nights**, and at least **half the expected share**, `n_free >= 0.5 * (len(free_days) / 7) * n_kept` (about 14% for a Saturday and Sunday weekend). The share rule catches uneven missing data, such as a watch left off at weekends. If either fails: `chronotype: null` with a note naming the failed rule and the counts.
- **Label:** `early` before 03:30, `intermediate` 03:30 to 04:30, `late` after 04:30. These are approximate population bands with no age or sex adjustment, and the note says so. The numeric `msf_sc` is the main value.
- Means, not medians, because the sleep-debt correction is linear arithmetic on means. The median MSF is reported for comparison and used as one arm of the sensitivity grid.

**Step 4: sleep need, output 2**

The estimates are kept separate, and the outcome-based one only corroborates. In priority order for `primary`:

1. **`user`**: `sleep_need_min`, if the caller passes it.
2. **`free_night_observed`**: `SD_f`, the mean free-night window. This is the MCTQ's own stand-in for need: sleep when no alarm constrains it.
3. **`outcome_estimate`**: only when there's no chronotype (too few free nights). This is the plateau method below.

Always reported alongside, when computable:
- **`outcome_estimate`** as a *range*: bin nights by window in 30-min bins (at least 8 nights each). Report the span of bins whose mean outcome is within 1 point of the best bin. A range rather than a point, because a single "shortest bin within 1 point" overfits noise and Garmin's own scoring.
- **`agreement`**: `yes` if `primary` falls inside the outcome range widened by 15 min on each side, otherwise `no`. A `no` gets a note.
- **`catch_up_inflated`**: `true` when `SD_f - SD_w > 60 min`. Free nights then include catch-up sleep, so `SD_f` overstates need. The note suggests passing `sleep_need_min`, and `SD_week` is reported for reference.
- An **80% interval** on `primary` (from step 5).

**Step 4b: wake window, output 3 (inference)**

```
circadian_wake   = MSFsc + sleep_need / 2
circadian_onset  = MSFsc - sleep_need / 2
lights_out       = circadian_onset - 15 min   (stated assumption)
```

Marked `inferred: true` with the note: *"Derived from MSFsc and estimated sleep need; not a validated MCTQ output."*

**A useful identity, documented in the README:** when sleep need is `free_night_observed` and there's no sleep-debt correction, `circadian_wake = MSF + SD_f / 2` is exactly the **mean free-night wake time**. With the correction, it is that wake time moved earlier by half the catch-up sleep. So the formula's answer is "when you wake on free days, net of catch-up sleep". This makes the alarm-on-free-days caveat concrete: if weekend alarms are set, this number is wrong in exactly that direction. It also gives a direct unit test (test 4).

**Step 5: uncertainty (80% intervals)**

- **Block bootstrap by ISO week**, 500 resamples. Weeks are drawn with replacement until the resample has as many weeks as the original.
- Why weeks and not single nights: consecutive nights are correlated, and the work/free mix is a weekly structure. Resampling single nights would understate uncertainty and could produce resamples with almost no free nights.
- Each resample recomputes MSF, SD_f, SD_w, MSFsc, sleep need, and `circadian_wake` *jointly*. So the wake interval carries the uncertainty from both its inputs.
- Report the 10th and 90th percentiles as `interval_80` for `msf_sc`, `sleep_need.primary` and `circadian_wake`.
- Resamples failing the free-night minimum are skipped and counted. If more than 10% are skipped, the note says the interval is unreliable.
- When `sleep_need_min` is user-supplied, it is held fixed, so only MSFsc uncertainty flows into the wake interval.

**Step 6: sensitivity analysis**

Recompute MSFsc and `circadian_wake` over a grid of reasonable assumptions, all windows anchored at `end`:

| Dimension | Values |
|---|---|
| Window | 90, 180, 365 days (only those with at least 28 nights available; may reach before `start`) |
| MSF estimator | mean, median |
| Travel filter | 120, 180, 240 min |

That's up to 18 runs. Report:
- `msf_sc_range` and `wake_range` (min to max across runs)
- `label_stable`: the same chronotype label in every run
- **`stability`**:
  - `high`: MSFsc range ≤ 20 min, wake range ≤ 30 min, and a stable label;
  - `moderate`: within 45 and 60 min;
  - `low`: otherwise.
- **`most_sensitive_to`**: the dimension whose change, with the others at default, moves the wake estimate most. When `stability` is `low`, a note says for example *"Estimate shifts 70 min between the 90-day and 365-day windows; your schedule may have changed. Consider `start` = a date after the change."*

**Step 7: evidence (out-of-sample model comparison)**

This tests whether the inferred wake window carries information about Garmin outcome scores *beyond sleep duration and sleep regularity*, on nights the estimate never saw.

*Models.* Ordinary least squares, fitted with a small stdlib normal-equations solver (at most 5 predictors, standardised, tiny ridge term for numerical safety). Predictors:

| Model | Predictors | Question it answers |
|---|---|---|
| A | duration, duration² | baseline: does just sleeping longer explain it? (squared term so A can plateau) |
| B | wake, wake² | does wake time alone explain it? (squared term so B can find its own optimum; a linear-only B would be an unfair, easily beaten competitor) |
| C | distance = \|wake − circadian_wake\| | does closeness to the inferred window explain it? |
| D | duration, duration², distance | **the key model:** does the circadian component add to duration? |
| E | duration, duration², wake, wake² | the toughest competitor: an outcome-*learned* best wake time plus duration |
| G | duration, duration², drift | regularity baseline: does keeping a regular schedule, plus duration, explain it? |
| F | duration, duration², distance, drift | **the regularity control:** does circadian distance still add once regularity is in the model? |

`drift` is the step 9 quantity: the absolute difference between the night's mid-sleep and the median of the previous 7 nights. It uses only earlier nights' *timing*, never outcomes, so it is available at prediction time and doesn't leak test outcomes. Nights without 4 prior nights are dropped from every model's fit and pooled RMSE, so all models are compared on the same nights.

D vs A asks whether the circadian anchor adds anything. D vs E asks whether the anchor, which is derived from timing alone with no outcomes, does as well as an optimum learned from outcomes. If D is as good as E with fewer free parameters, that supports the method rather than curve-fitting. **F vs G** asks whether circadian distance is more than a stand-in for regularity. Nights far from the circadian wake are often also irregular nights, and irregular nights tend to score lower.

*Rolling holdout.* Over **all history up to `end`** (not just the analysis range, which only needs to be long enough for a chronotype):
- Train on 120 days, then test on the next 30 days; step forward 30 days; repeat.
- In each fold, **re-estimate MSFsc and sleep need on the training window only**, compute `distance` for the test nights from that training-window estimate, fit all models on training nights, and predict test nights.
- Needs at least **180 days** of history (2 folds). Otherwise `evidence: {"status": "insufficient_history", "days": N}`.

*Reported:*
- Pooled out-of-sample RMSE per model, plus `d_beats_a_folds` and `d_vs_e` ("better", "similar" (within 2% RMSE) or "worse").
- **`distance_survives_regularity`**: `true` when F beats G on pooled RMSE **and** in a majority of folds, **and** the fitted distance coefficient in F has the same sign as in D (scores lower further from the window) in at least 70% of folds. Otherwise `false`, with the note: *"Waking near your window goes with higher Garmin scores, but that may mostly reflect keeping a regular schedule."* `f_beats_g_folds` and `distance_sign_consistent_folds` are reported alongside.
- **`effect_within_window`**: on test nights, the mean residual from model A for nights waking within ±30 min of the fold's `circadian_wake`, minus the mean residual for the others. In words: *"on unseen nights, waking near your window was associated with X more points than sleep duration alone predicts."* It carries an 80% interval from a week-block bootstrap over test nights. The output says this is an association with Garmin's scores, not a measured change in physiology.
- **`evidence_level`**. The labels deliberately avoid hypothesis-testing vocabulary. An 80% interval excluding zero is a permissive bar, and a user must not read the top level as "statistically significant". The levels describe how *consistently* the pattern held on unseen nights:

| Level | Rule |
|---|---|
| `consistent` | at least 4 folds; D beats A on pooled RMSE; D beats A in ≥ 70% of folds; effect 80% interval excludes 0; **and `distance_survives_regularity` is true** |
| `supportive` | D beats A on pooled RMSE and in a majority of folds; effect > 0. Also the ceiling when everything for `consistent` holds except `distance_survives_regularity`, in which case the regularity note is added |
| `suggestive` | D beats A on pooled RMSE only |
| `unsupported` | D does not beat A on pooled RMSE |

*Also kept, labelled in-sample:* the descriptive wake-time bins from the earlier design (nights in the user's middle 50% of durations, 30-min wake bins, at least 8 nights each, mean ± SE), with `observed_best_wake`, its `clear`/`weak` confidence (one combined SE; a heuristic, not significance), the work-nights-only `weekend_confounded` check, and `latest_good_wake`. These are useful for showing a person their own pattern, but they are marked `in_sample: true` and **never** set `evidence_level`.

**Step 8: reconcile and interpret**

Disagreement is reported, never averaged away.

| Condition | `agreement` | Output |
|---|---|---|
| `observed_best_wake` midpoint within 30 min of `circadian_wake` | `agree` | `wake_window` = `circadian_wake` ± 15 min |
| more than 30 min apart, and in-sample `clear` or evidence ≥ `supportive` | `disagree` | `wake_window` = the circadian window (it is still the inference); **plus `recommended_experiment`** (below). No merged span |
| in-sample `weak` and evidence ≤ `suggestive` | `formula_only` | `wake_window` = `circadian_wake` ± 15 min |
| no chronotype; in-sample `clear` | `data_only` | `wake_window` = the observed bin, labelled in-sample |
| neither | `none` | `null` plus a note |

**`recommended_experiment`** (disagree only): a 30-min window centred on the midpoint of `circadian_wake` and the `observed_best_wake` midpoint. It comes with a protocol: hold it for 21 nights, then compare `readiness_score` (or the chosen outcome) for those nights against the 21 before. This uses existing tools, e.g. `query_metrics([...], start=..., stats=True)`, so no new machinery is needed.

`latest_good_wake`, when present, caps every window. `bedtime` is always `wake_window_start − sleep_need`.

**`interpretation`**: 2 or 3 sentences generated from the above. On the demo store: *"Your chronotype suggests waking about 07:44. Your past Garmin scores were highest when waking 07:00-07:30. These agree within 30 minutes. On unseen nights, waking within 30 minutes of the window went with sleep scores 7.9 points higher than sleep duration alone predicts, and the pattern held after accounting for schedule regularity (evidence: supportive)."*

**Wording rule (applies to `interpretation`, `notes`, the docstring and the README):** outcomes are always called *Garmin scores* or *Garmin outcome scores*, never "recovery", "health" or "better sleep". Effects are *associated with* or *went with*, never *caused* or *improves*. Test 31 scans generated text for the banned words.

**Step 9: regularity**
- For each kept night, the absolute difference between its mid-sleep and the median of the previous 7 nights.
- Report the median drift and mean outcome by drift bucket (under 30, 30 to 60, over 60 min; at least 8 nights each). Descriptive and in-sample.

### 5.4 Output shape

Default output targets **under 2.5 KB**. `detail=true` adds the bins, the sensitivity grid, the per-fold table and per-model RMSE, targeting under 6 KB.

```json
{
  "range": {
    "start": "2026-02-04",
    "end": "2026-08-02",
    "nights": 177,
    "excluded": {
      "implausible": 0,
      "shifted": 0
    }
  },
  "outcome": "sleep_score",
  "rhythm": {
    "all": {
      "onset": "23:51",
      "wake": "06:57",
      "mid": "03:26",
      "window_min": 430,
      "n": 177,
      "mad_min": [
        33,
        49
      ]
    },
    "work": {
      "onset": "23:37",
      "wake": "06:28",
      "mid": "03:02",
      "window_min": 414,
      "n": 126
    },
    "free": {
      "onset": "00:15",
      "wake": "08:05",
      "mid": "04:10",
      "window_min": 482,
      "n": 51
    }
  },
  "dst_transitions": [
    "2026-03-08"
  ],
  "chronotype": {
    "msf_sc": "03:46",
    "interval_80": [
      "03:39",
      "03:52"
    ],
    "msf": "04:08",
    "msf_median": "04:10",
    "label": "intermediate",
    "social_jetlag_min": 63
  },
  "sleep_need": {
    "primary_min": 478,
    "source": "free_night_observed",
    "interval_80": [
      472,
      483
    ],
    "outcome_estimate": [
      450,
      510
    ],
    "agreement": "yes",
    "catch_up_inflated": true,
    "sd_week_min": 432
  },
  "wake_window": {
    "inferred": true,
    "circadian_wake": "07:44",
    "interval_80": [
      "07:39",
      "07:50"
    ]
  },
  "stability": {
    "level": "high",
    "msf_sc_range": [
      "03:37",
      "03:48"
    ],
    "wake_range": [
      "07:37",
      "07:47"
    ],
    "label_stable": true,
    "most_sensitive_to": "filter"
  },
  "evidence": {
    "status": "ok",
    "folds": 2,
    "test_nights": 59,
    "n_test_range": [
      29,
      30
    ],
    "d_beats_a_folds": "2/2",
    "d_vs_e": "better",
    "effect_within_window": 7.9,
    "effect_interval_80": [
      5.3,
      10.8
    ],
    "distance_survives_regularity": true,
    "f_beats_g_folds": "2/2",
    "distance_drift_r": 0.5,
    "level": "supportive",
    "in_sample": {
      "observed_best_wake": "07:00-07:30",
      "confidence": "weak",
      "weekend_confounded": false
    }
  },
  "recommendation": {
    "agreement": "agree",
    "wake_window": [
      "07:29",
      "07:59"
    ],
    "bedtime": "23:32",
    "lights_out": "23:17"
  },
  "regularity": {
    "median_drift_min": 42,
    "by_drift": [
      [
        "<30",
        64,
        70.8
      ],
      [
        "30-60",
        53,
        70.1
      ],
      [
        ">60",
        56,
        66.6
      ]
    ]
  },
  "interpretation": "Your chronotype suggests waking about 07:44. Your past Garmin scores were highest when waking 07:00-07:30. These agree within 30 minutes. On unseen nights, waking within 30 minutes of the window went with sleep scores 7.9 points higher than sleep duration alone predicts, and the pattern held after accounting for schedule regularity (evidence: supportive).",
  "notes": [
    "Range crosses clock changes (2026-03-08); estimates may blend by up to 30 min. Times use the clock in force at the end date.",
    "Chronotype bands are approximate.",
    "Free nights run over an hour longer than work nights, so they likely overstate need; consider passing sleep_need_min.",
    "The wake window is inferred from MSFsc and sleep need, not an MCTQ output. Alarms on free days would make it earlier than your body clock.",
    "Evidence is an association with Garmin's scores, not physiology; levels describe consistency on unseen nights, not significance."
  ]
}
```

(Actual output on the 180-day demo store ending 2026-08-02, timezone America/New_York. The demo has only two holdout folds, which caps evidence at `supportive`.)

### 5.5 Performance budget

Approximate per-call work on 365 nights: 500 bootstrap resamples × O(n), up to 18 sensitivity runs, about 11 folds × 7 small fits, and 500 resamples for the effect interval. All pure Python. **Target under 1.5 s**. A test asserts under 5 s on the 365-day demo, a loose bound so CI isn't flaky.

### 5.6 What the evidence can and cannot show

At best, the evidence block establishes this: **on nights it did not see, waking near the inferred circadian window was consistently associated with higher Garmin outcome scores, beyond what sleep duration and sleep regularity predict.**

It does not establish that wake timing changes physiology. Readiness and sleep score are Garmin's own composites. They already weight duration, and may weight timing, so part of any effect can be the tool rediscovering Garmin's scoring rules. A Garmin store holds no independent physiological ground truth (no DLMO, core temperature or light exposure). That gap can't be closed with this data, and section 7 keeps such signals out of scope on purpose. `body_battery_high` is offered as an alternative outcome, but it is also a Garmin composite.

## 6. Generalising for other users

| Concern | Handling |
|---|---|
| Weekends that aren't Saturday and Sunday | `free_days` setting; `SD_week` weights by `len(free_days)` |
| Devices without readiness (most older watches) | `outcome` falls back to `sleep_score`; output names the one used |
| Devices without a sleep score | if no 0–100 outcome has at least 28 values, `evidence` is skipped and the result is `formula_only` |
| Travel and jet lag | 180-min mid-sleep jump filter (step 0.3); varied 120 to 240 in the sensitivity grid |
| Shift and night workers | daytime mid-sleep guard (step 2) |
| Short history | under 28 nights is an error; under 180 days gives `evidence: insufficient_history` |
| Watch left off on free days | free-night share rule (step 3) |
| Daylight saving | `dst_transitions` and a note; no time adjustment (step 1b) |
| Recent schedule change (new job, baby, move) | caught by the sensitivity grid's 90 vs 365-day window; `stability: low` with a note suggesting `start` |
| Alarm-driven free days | standing note; the step 4b identity explains exactly how it biases the answer |
| Heavy weekend catch-up sleep | `catch_up_inflated` flag; suggests passing `sleep_need_min` |
| Confounders (illness, alcohol, late events) | duration control, holdout validation, D vs E comparison, standing association note |
| Outcome is Garmin's own composite score | wording rule (step 8), standing note, and section 5.6 |
| Regular sleepers look "well-timed" just by being regular | model F vs G and `distance_survives_regularity` (step 7) |
| Medical framing | no "healthy" or "unhealthy" language; describes the user relative to their own history only |
| Naps | main sleep only; naps not counted |

## 7. Explicit non-goals

- **No new physiological signals** (DLMO, melatonin, light sensors, temperature-based phase). The opportunity is rigour on existing data, not more data.
- **No 90-minute sleep-cycle advice.** Cycle length varies, and Garmin stage data can't time cycle boundaries.
- **HRV is not an outcome.** In real data the nightly HRV average rose on late, short nights, more likely a measurement quirk than better recovery. It stays available through `correlate`.
- **No light, caffeine or melatonin guidance.**
- **No schema change and no new sync endpoint.**
- **No built-in experiment tracker.** `recommended_experiment` gives a protocol that uses existing tools. A follow-up evaluation tool is future work.

## 8. Demo store changes (`demo.py`)

Today's demo sleep timestamps are independent of duration and score, so `circadian()` would find nothing in them. There is also a latent bug: `bed_hour = 22 + random() * 2.4` can reach 24.x, which writes `T24:MM:00`, an invalid ISO time.

Changes:
1. A planted chronotype: **MSFsc around 03:45**, sleep need around 450 min.
2. **Work nights:** wake alarm-pinned around 06:30 (sd 15 min), with a slightly short window.
3. **Free nights:** wake drifts later to around 07:45 with a longer window, so the sleep-debt correction does real work.
4. `start_ts` and `end_ts` computed from onset plus window, consistent with `duration_min + awake_min` and correctly wrapped past midnight. This fixes `T24`.
5. A sleep-score and readiness penalty that grows with distance from the planted natural wake time, **independent of duration and of drift**, so models D and F have something real to find on holdout. The existing random noise on timing gives drift and distance enough separation for F to tell them apart.
6. The README demo table gains a row: `circadian()` recovers MSFsc within ±15 min of 03:45, with `evidence_level` of at least `supportive`.

Determinism tests still hold. Assertions on specific seeded values get updated.

## 9. Validation

**Real data.** Validated against a private maintainer dataset spanning 400+ nights. Personal health metrics are intentionally omitted from the public repository. The pre-implementation analysis and the acceptance run are kept in the
maintainer's private notes as a regression benchmark.

**Demo store.** The synthetic store plants a chronotype (MSFsc 03:45) and a score penalty for
waking far from the natural wake time (section 8). Measured over 14 end dates and 2 seeds:

| Check | Result |
|---|---|
| MSFsc recovered | 03:39 to 03:56 (planted 03:45) |
| Circadian wake | about 07:45 |
| Stability | high |
| Evidence | `supportive` on every run (two folds caps it there) |

**Acceptance criteria used on the private dataset** (qualitative): MSFsc and circadian wake land
where a hand calculation of the MCTQ formula puts them; the step 4b identity holds to within a
few minutes; `dst_transitions` lists the clock changes in range; and the evidence level is
**recorded, not required**, because requiring a particular result would defeat the purpose of the
holdout.

## 10. Tests

New tests in `tests/test_circadian.py` use small synthetic stores built inline, following the existing pattern.

*MCTQ core*
1. **Worked example:** work nights 23:30–06:30, free nights 00:00–08:30, so MSF = 04:15 and **MSFsc = 03:43**.
2. **No correction** when free nights aren't longer: MSFsc equals MSF.
3. **Custom `free_days`** of Friday and Saturday changes which nights count as free.
4. **Identity:** no correction and need from free nights gives `circadian_wake` equal to the mean free-night wake, to the minute.
5. **Chronotype labels:** 03:29, 03:30, 04:30 and 04:31 map to early, intermediate, intermediate, late.
6. **Median MSF reported:** one extreme free night moves `msf` but not `msf_median`.

*Sleep need*

7. **Priority:** user, then free-night observed, then outcome estimate only when there's no chronotype. `source` asserted.
8. **Outcome range and agreement:** both the `yes` and `no` cases are built on purpose.
9. **Catch-up flag:** SD_f − SD_w = 75 min sets `catch_up_inflated`.

*Uncertainty and sensitivity*

10. **Bootstrap determinism:** two calls give identical intervals.
11. **Interval behaviour:** each interval contains its point estimate, and it narrows when the same pattern spans 4× as many weeks.
12. **Week blocks:** no resample has zero free nights when the source has at least 8.
13. **Sensitivity catches a schedule change:** a store whose wake time moves 90 min at day 200 of 365 gives `stability: low`, `most_sensitive_to: window`, and a note suggesting `start`.
14. **Stable store:** a store with constant timing gives `stability: high` and `label_stable: true`.

*Evidence*

15. **True positive:** a store with a planted circadian-distance effect, independent of duration and drift, gives D beating A, `distance_survives_regularity: true`, and evidence ≥ `supportive`.
16. **Duration-only confound:** a store where outcome depends only on duration, and late wakes are simply longer nights, gives evidence `unsupported` or `suggestive`. Model A must absorb it.
17. **False-positive rate:** across 20 seeds of a store with *no* timing effect, `consistent` appears at most once.
18. **No leakage:** a fold's `circadian_wake` is computed from training nights only (asserted by perturbing test nights and checking the fold's estimate doesn't change).
19. **Insufficient history:** 150 days gives `evidence.status == "insufficient_history"`.
20. **In-sample bins never set the level:** bins marked `in_sample: true`; a store with a clear in-sample bin but no holdout effect gives evidence `unsupported` or `suggestive`.
20a. **Regularity-only confound:** a store where outcome depends only on drift, and irregular nights also tend to sit far from the circadian wake, gives `distance_survives_regularity: false`, the regularity note, and evidence no higher than `supportive`.
20b. **Distance survives regularity:** a store with a planted drift effect *and* a separate planted distance effect gives `distance_survives_regularity: true`.

*Reconcile and guards*

21. **Reconcile paths:** `agree`, `disagree` (with `recommended_experiment` and **no merged span**), `formula_only`, `data_only` and `none`.
22. **Travel filter:** a 9-hour shifted block is excluded and counted.
23. **Shift-work guard:** daytime mid-sleep gives no wake window and no evidence.
24. **Outcome fallback:** no readiness rows gives `outcome == "sleep_score"`.
25. **Too few nights:** 20 nights returns the error.
26. **Free-night rules:** under 8 free nights, and separately 12 of 120 (10%), each give `chronotype: null` with the right note.
27. **DST detection:** with `_host_utc_offset` patched to a DST zone, a range across a transition lists it; a range without one omits the field.

*Plumbing*

28. **Registry metrics:** `query_metrics(["wake_time_min"])` returns 390 for a 06:30 wake; onset before midnight is negative.
29. **Output size:** default under 2.5 KB and `detail` under 6 KB on the 365-day demo.
30. **Performance:** under 5 s on the 365-day demo.
31. **Wording rule:** across every reconcile path and evidence level, `interpretation` and `notes` never contain "recovery", "healthy", "unhealthy", "significant", "causes" or "improves".

Plus `tests/test_server.py` (the tool is registered and returns a dict) and `tests/test_demo.py` (planted chronotype recovered; evidence ≥ `supportive`; no `T24` timestamps).

## 11. Files touched

| File | Change |
|---|---|
| `src/garmin_mcp/metrics.py` | 3 registry entries |
| `src/garmin_mcp/circadian.py` | **new**: loading and cleaning, MCTQ, sleep need, week-block bootstrap, sensitivity grid, stdlib OLS, rolling folds, reconcile, interpretation |
| `src/garmin_mcp/analysis.py` | none, or only exporting existing helpers (`_mean`, `_pstdev`, `_parse_date`) for reuse |
| `src/garmin_mcp/server.py` | `circadian` tool; docstring "12 compact tools" becomes 13 |
| `src/garmin_mcp/demo.py` | planted chronotype and circadian effect; consistent timestamps; `T24` fix |
| `tests/test_circadian.py` | **new**, as section 10 |
| `tests/test_server.py`, `tests/test_demo.py` | as section 10 |
| `README.md` | "Sleep timing and chronotype" section (three outputs, evidence levels and what they don't mean, the step 4b identity, section 5.6 limits, wording rule), demo table row, tool count |
| `manifest.json` | tool list entry; version; uvx pin `>=0.1.8` |
| `server.json` | version (both places) |
| `pyproject.toml`, `uv.lock` | version |
| `CHANGELOG.md` | 0.1.8 entry |

All new code is standard-library only.

## 12. Release (0.1.8)

The usual order:
1. PyPI: commit, tag `v0.1.8`, push; `publish.yml` publishes.
2. mcpb: `npx --yes @anthropic-ai/mcpb pack . garmin-local-mcp-0.1.8.mcpb`; check the size is about 9 kB; `gh release create`.
3. MCP Registry: `mcp-publisher login github`, then `publish`, back to back.
4. Glama: sync the server, confirm the commit matches HEAD, then Build & Release.

The maintainer confirms before step 1.

## 13. Open questions

Resolved in review 1: mean vs median, band cut-offs, "clear" threshold, window vs time asleep. Resolved in review 2: sleep-need priority, disagreement handling. Resolved in review 3: regularity confound (model F), Garmin-outcome wording, interval level and evidence labels. Resolved before release: personal health data removed from this public copy.

Still open:

1. **Default range of 180 days.** It's enough for about 50 free nights; the sensitivity grid now flags when older data disagrees. *Proposal:* keep.
2. **DST host-timezone assumption.** Wrong for a user whose server runs in another zone; the only effect is a wrong or missing note. *Proposal:* accept for v1. A later version could store UTC offsets (the FIT importer already computes them), at the cost of a schema change.

## 14. Review log

### Review 1 (2026-10-04)

The reviewer confirmed: the sleep window (not time asleep) for MCTQ math, minutes relative to the wake date's midnight, the 7-night median travel filter, and duration control.

| Item | Recommendation | Decision | Where |
|---|---|---|---|
| Mean vs median MSF | Means after the travel filter; also report the median | **Accepted** | Steps 1, 3 |
| Chronotype labels | Narrow to early < 03:30, late > 04:30 | **Accepted** | Step 3 |
| "Clear" threshold | 1 SE is fine if worded as a trend | **Accepted** | Step 7 (in-sample) |
| Window vs net sleep | Keep the window | **Accepted** | Section 4 |
| DST handling | Note DST transitions in range | **Accepted, extended:** host-timezone detection, no time adjustment | Step 1b |
| Free-night share | Require ≥ 15% of kept nights | **Accepted, reframed** as "half the expected share", which targets uneven missing data | Step 3 |

### Review 2 (2026-10-04)

Theme: make the inference chain scientifically cleaner and harder to fool, rather than adding features.

| Item | Recommendation | Decision | Where |
|---|---|---|---|
| Three separate outputs | Chronotype, sleep need, wake window as distinct quantities; label the window as inference | **Accepted.** Output restructured; `inferred: true` plus a note | Sections 1, 5.3 steps 3, 4, 4b; 5.4 |
| Uncertainty | Bootstrap nights, 500–1,000 iterations, 80% intervals | **Accepted, refined:** bootstrap by **ISO week**, not by night. Nights are autocorrelated and the work/free mix is weekly, so night-level resampling understates uncertainty. 500 resamples, joint over MSFsc and need, deterministic seed | Step 5 |
| Sleep-need priority | user > free-day observed > outcome; report all; outcome corroborates | **Accepted.** Outcome estimate is now a *range* and an agreement flag. Added `catch_up_inflated`, since heavy catch-up makes free-day duration overstate need | Step 4 |
| Sensitivity analysis | 90/180/365 days × mean/median × filter 120/180/240 | **Accepted** as specified, plus `most_sensitive_to` and stability thresholds | Step 6 |
| Baseline models | A duration, B wake, C distance, D duration + distance | **Accepted, extended:** quadratic terms in A and B so the baselines can plateau or find an optimum (a linear B would be a straw man), and **model E** (duration + learned wake optimum) as the toughest competitor | Step 7 |
| Holdout | Rolling 120-day train, 30-day test | **Accepted.** MSFsc and need are re-estimated per training window to prevent leakage; uses all history up to `end`; at least 180 days required; test 18 checks for leakage | Step 7 |
| Disagreement | Don't merge into a wider window; return both plus an experiment window | **Accepted.** `recommended_experiment` with a 21-night protocol using existing tools | Step 8 |
| No new signals (DLMO, light) | Rigour over more data | **Accepted** | Section 7 |

**Added beyond review 2, in its spirit:**
- **The step 4b identity:** circadian wake = mean free-night wake net of catch-up. This makes the method transparent and gives an exact unit test.
- **False-positive tests:** a duration-only confound store and a 20-seed null store. The evidence step must stay quiet when there is nothing to find.
- **In-sample results kept but fenced off:** the wake-time bins are labelled `in_sample` and can never set the evidence level (renamed `evidence_level` in review 3).
- **Expected-result change recorded** (privately): on real data the new sleep-need priority moved the result from `disagree` to `agree`.
- **No required evidence strength in acceptance:** the holdout result is recorded, not targeted.

### Review 3 (2026-10-04)

The reviewer rated the plan 9.4 to 9.6 and called it ready to implement once the three changes below are made. They singled out model E and the step 4b identity as the strongest parts of the design.

| Item | Recommendation | Decision | Where |
|---|---|---|---|
| Regularity control | Add model F = duration + duration² + distance + drift; report whether distance survives | **Accepted, extended:** added the matching baseline **G** (duration + drift), so the test is F vs G, a like-for-like comparison. `distance_survives_regularity` needs F to beat G pooled and in a majority of folds, plus a consistent sign on distance. `consistent` now *requires* it; without it the ceiling is `supportive`, with a note. Two new tests | Step 7; section 6; tests 15, 20a, 20b |
| Wording | "Higher Garmin recovery scores" or "Garmin outcomes", not "better recovery" | **Accepted, made a rule:** docstring, interpretation, notes and README call outcomes *Garmin scores*; effects are *associated with*; a test scans generated text for banned words. New section 5.6 states what the evidence can and cannot show | Sections 1, 5.2, 5.6; step 8; test 31 |
| Evidence labels | 90% interval, or keep 80% and rename to consistent / supportive / suggestive / unsupported | **Accepted the rename** (the reviewer's preference). 80% intervals kept everywhere for one convention; the docstring and output say the levels are not significance. The old 80% vs 90% open question is closed | Step 7; sections 5.2, 5.4 |

**Algorithm frozen** with these changes. Further comments should target implementation detail, not method.

### Review 4 (2026-10-04)

The reviewer confirmed the freeze (method rated 9.6) and said the remaining risk is implementation quality, not method. The next review should be of the code and the first real `circadian()` output, not another design pass. Their seven implementation points are in section 15. One of them changes an output field: `distance_survives_regularity` gains an `indeterminate` value for when distance and drift are too correlated to separate.

## 15. Implementation checklist (from review 4)

Each item has a test or a reported field, so it can be checked rather than trusted.

| # | Requirement | How it is enforced |
|---|---|---|
| 1 | Every model in a fold is fitted and scored on **exactly the same eligible nights** (outcome present, duration present, at least 4 prior nights for drift) | One eligibility mask per fold, built once and passed to every model. Test: per-fold `n_train` and `n_test` are identical across A–G |
| 2 | Predictors are **standardised with training-fold mean and sd only**; test nights reuse those values | The scaler is fitted inside the fold on training rows. Test: perturbing test-night predictors leaves the fold's scaler unchanged |
| 3 | The **same ridge term** (λ = 1e-6 on standardised predictors, intercept unpenalised) for every model | One module constant `_RIDGE`, with no per-model override. Asserted in the fit helper |
| 4 | Test-night `drift` uses **only timing from earlier nights** | Drift is computed once over the whole history in date order from the previous 7 nights, so it never looks forward. Test: changing a later night's timing doesn't change an earlier night's drift |
| 5 | The week-block bootstrap **keeps the weekly structure and the free/work mix** | Resampling unit is the ISO week. Reported in `detail`: free-night share in the original data vs the mean and 10th–90th percentile across resamples. Test: mean resampled share within 3 percentage points of the original on the demo |
| 6 | **Collinearity between `distance` and `drift`** is checked | Pearson r between them on each training fold, reported as `distance_drift_r` (median across folds). If the median `|r| > 0.8`, F can't separate the two: `distance_survives_regularity` becomes `"indeterminate"` (not `false`), the evidence ceiling is `supportive`, and a note says so. Test: a store with distance ≈ drift gives `indeterminate` |
| 7 | **Report fold counts and per-fold sample sizes** when data gaps make folds uneven | `detail` adds a fold table (`train_start`, `test_start`, `n_train`, `n_test`); the default output shows `folds` and the min/max `n_test`. Folds with fewer than 10 test nights are dropped and counted in `folds_skipped` |

New tests for items 1, 2, 4, 5 and 6 are added to section 10 at implementation time (bringing the total to 38). The algorithm is otherwise unchanged.

## 16. Implementation notes (2026-10-04)

Built as specified, in `src/garmin_mcp/circadian.py` (stdlib only), with 47 new tests (44 in `tests/test_circadian.py`, 2 in `test_demo.py`, 1 in `test_server.py`). The full suite passes: 197 passed, 1 skipped. Ruff is clean.

### Deviations from the plan

| Plan said | Built | Why |
|---|---|---|
| Default output under 2.5 KB | Under **3 KB** (test-enforced; about 2.6 to 2.95 KB in practice) | The notes and the experiment block are needed for honest output. Trimmed what could go (nested flags, null fields, sign-consistency moved to `detail`) |
| Travel filter: prior nights within the previous week or so | Prior nights must fall in the last **14 days** | With 10 days, a 9-night trip was only partly filtered (nights 8 and 9 slipped through). 14 days filters a 9-night trip completely; a permanent move is accepted after about 11 days |
| DST from host timezone | Uses the configured `timezone` when set, the host's otherwise | `config.timezone` already exists, which resolves most of open question 2 |
| `latest_good_wake` caps every window | Caps only `agree` and `data_only` windows; always reported | Capping a `formula_only` or `disagree` window dragged the inference toward an in-sample bin, which is the merging the plan forbids. Found by test |
| Demo work-wake sd 15 min | 50 min, score penalty 0.15 per min | At lower spread, distance was confounded with work vs free (and so with duration), and the demo's evidence level flipped with the end date. Measured over 14 end dates and 2 seeds: `supportive` every time (2 folds caps it there) |
| `d_vs_e` as an RMSE ratio | Same, plus a guard when E fits the test nights exactly | Degenerate stores divided by zero (found by the wording test) |

### Acceptance on the private dataset

All section 9 criteria were checked and recorded privately. The chronotype, wake estimate and
agreement matched the hand calculation; stability came out `low`, which traced to a genuine recent
schedule shift (the case the sensitivity grid exists to catch); the evidence level was recorded
rather than required. Runtime was 0.06 s for 180 days and 0.16 s for the full history.
