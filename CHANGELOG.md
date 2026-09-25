# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.7] - 2026-09-25

### Changed

- `correlate(scan_lags=True)` now judges the strongest lag against all 15 lags
  it tested. `best_lag` carries its `n` and a Bonferroni-adjusted
  `p_adjusted`, and when that is 0.05 or higher, `note` says the lag may be
  chance and suggests a wider range. Previously a scan over a short window
  could report a spurious lag with no warning: on the demo store's default
  30-day window, 14 paired days produced lag -7, r = -0.59 (p_adjusted 0.38).
- Demo store: the planted next-day training-load effect on resting HR is
  stronger (0.018 to 0.026 bpm per load unit), so the lag scan's lag-1 result
  is significant after correction (r = 0.47 over 93 training days) instead of
  borderline (r = 0.30, p_adjusted 0.053).
- `demo` now prints the store's full date range and notes that the analysis
  tools default to the last 30 days, which miss the illness window.

### Fixed

- README demo table: the lag-1 figure said +0.45 while the store produced
  +0.30, and the anomaly row listed SpO2 (not flagged in the illness window)
  while omitting stress. Both now match the generated store, and the table
  says it describes the full 180-day range.

## [0.1.6] - 2026-09-02

### Added

- Performance scores: four new sync endpoints — `endurance_score`,
  `hill_score`, `training_readiness` and `race_predictions` — feeding a new
  `performance` table (schema migration v3) with `endurance_score`,
  `endurance_class`, `hill_score`, `hill_endurance_score`,
  `hill_strength_score`, `readiness_score`, `readiness_level`,
  `recovery_time_min` and `race_5k_s` / `race_10k_s` / `race_half_s` /
  `race_marathon_s`. All twelve are registered as query metrics, surfaced by
  `get_day` under a `performance` key, and counted by `sync_status`.

  The table is deliberately absent from the `gaps` daily-table set: these
  scores update on Garmin's own cadence, so a day without a new endurance
  score is normal rather than a hole.

  `training_readiness` prefers the post-wake (`AFTER_WAKEUP_RESET`) snapshot —
  the reading Garmin shows in the Morning Report — and falls back to the first
  entry on firmware that leaves `inputContext` unset. Every parser drops a
  payload stamped with a different `calendarDate`, because metrics-service can
  answer an out-of-range date with the latest reading instead of an empty one.

  All four payload shapes are verified against live metrics-service responses.
  Endurance score's `classification` field turned out to be an opaque integer
  enum, so the tier label is instead derived from the
  `classificationLowerLimit<Tier>` ladder carried in the same payload —
  yielding `intermediate`, `trained`, `well_trained`, `expert`, `superior`,
  `elite`, or `below_intermediate` for a score under the lowest rung. Each
  parser still accepts a couple of alternate spellings as insurance against
  firmware variation, and writes nothing when none match, so an unexpected
  shape yields an empty row rather than a fabricated one.

### Fixed

- `.mcpbignore` now excludes `.tools/`, the local `mcp-publisher` binary. It
  was ignored by git but not by `mcpb pack`, so the 0.1.3 and 0.1.4 extension
  bundles shipped a 20 MB Go binary the server never uses: 15 MB where 9 kB
  was needed. The 0.1.5 bundle is built with the fix.

## [0.1.5] - 2026-08-26

### Added

- `demo` command: seeds a synthetic 180-day store so the analysis tools can be
  evaluated without a Garmin account, a login, or a network connection. The
  data is generated but internally coherent — a latent recovery factor drives
  HRV and resting heart rate in opposite directions, training load raises the
  following day's resting heart rate, and a six-day illness window plus a few
  missing sleep nights give `anomalies` and `gaps` something real to find.
  Deterministic per `--seed`; `--days` sets the range. Refuses to overwrite a
  database it did not generate, and `sync_status` reports `demo_store: true`
  so an assistant cannot present generated values as real measurements.
- README demo GIF, rendered by `scripts/render_demo_gif.py`.

## [0.1.4] - 2026-08-02

### Added

- Fitness age: new `fitnessage` sync endpoint (Garmin's
  `/fitnessage-service/fitnessage/<date>`) contributing `fitness_age` and
  `achievable_fitness_age` columns to the `training_status` table via partial
  upsert, plus matching `fitness_age` and `achievable_fitness_age` metrics in
  the query registry. Schema migration v2 adds the two columns; existing
  databases migrate automatically on next open, and component breakdowns
  (RHR, BMI, vigorous activity) remain recoverable from the raw snapshots.

## [0.1.3] - 2026-08-01

### Fixed

- Pin the MCP SDK to `mcp>=1.0,<2`. mcp 2.0.0 (released 2026-07-28) is a major
  rework that removed the `mcp.server.fastmcp` module, so fresh installs (for
  example the Claude Desktop extension running `uvx garmin-local-mcp serve`)
  crashed on startup with `ModuleNotFoundError: No module named
  'mcp.server.fastmcp'` and the client reported "Server disconnected".
  Environments that already had mcp 1.x cached were unaffected. Porting to the
  v2 API is tracked separately.

## [0.1.2] - 2026-07-10

### Fixed

- Data-dir resolution no longer fails with `[WinError 5] Access is denied: '${HOME}'`
  when the MCP host passes manifest template variables through unexpanded
  (observed with the Claude Desktop extension on Windows, where `HOME` is
  typically unset). The server now expands `${HOME}` and other environment
  variables in `GARMIN_MCP_DATA_DIR`, `--data-dir`, and `GARMINTOKENS` itself,
  and falls back to the default (`~/.garmin-mcp`) with a stderr warning if a
  value is empty or still contains an unexpanded `${...}` placeholder.

## [0.1.1] - 2026-07-05

### Added

- MCP Registry name marker in the README (`mcp-name: io.github.anup-shesh/garmin-local-mcp`)
  so the PyPI package can be verified for the official MCP Registry listing.

## [0.1.0] - 2026-07-05

Initial release.

### Added

- Local-first warehouse: immutable raw JSON snapshots plus a SQLite database
  in a user-owned data directory (`~/.garmin-mcp` by default, overridable via
  `GARMIN_MCP_DATA_DIR` or `--data-dir`).
- Incremental, resumable, rate-limit-aware sync engine over five curated
  Garmin Connect endpoints (daily wellness summary, sleep, HRV, training
  status, activities), with per-(endpoint, date) sync state, exponential
  backoff, and clean resumable aborts.
- CLI: `garmin-local-mcp serve | login | sync | status | import-fit | reparse`.
  Login supports MFA and persists tokens locally; `reparse` rebuilds the
  database from raw snapshots entirely offline.
- Stdio MCP server (FastMCP) with 12 compact tools: `auth_status`, `sync`,
  `sync_status`, `get_day`, `query_metrics`, `correlate`, `baselines`,
  `anomalies`, `list_activities`, `get_activity`, `gaps`, `import_fit`.
  Responses are columnar and typically under 2 KB.
- Server-side analysis: daily/weekly/monthly aggregation, Pearson/Spearman
  correlation with lag scanning, personal baseline bands, z-score anomaly and
  streak detection, and coverage-gap reporting, all computed locally.
- Canonical metric registry (about 35 metrics across wellness, sleep, HRV,
  training status, and activities) shared by every query tool.
- Zero-auth FIT fallback: a decoder for Garmin's undocumented wellness FIT
  messages (HRV summary, sleep stages, sleep score, skin temperature,
  on-device resting HR, naps, sleep window events) and an `import-fit` command
  that ingests manually exported "Export Wellness Data" bundles without any
  login. FIT-sourced rows never overwrite API-sourced rows unless forced.
- Data-quality flags: provisional on-device resting HR values sitting more
  than 10 bpm above the overnight HR floor are flagged and withheld; sparse
  sleep-stage logging is flagged as low-confidence.
- Offline test suite (sanitized JSON fixtures and small FIT samples) and CI
  across Python 3.12/3.13 on Ubuntu and Windows; no live API calls in CI.

[0.1.7]: https://github.com/anup-shesh/garmin-local-mcp/releases/tag/v0.1.7
[0.1.6]: https://github.com/anup-shesh/garmin-local-mcp/releases/tag/v0.1.6
[0.1.5]: https://github.com/anup-shesh/garmin-local-mcp/releases/tag/v0.1.5
[0.1.4]: https://github.com/anup-shesh/garmin-local-mcp/releases/tag/v0.1.4
[0.1.3]: https://github.com/anup-shesh/garmin-local-mcp/releases/tag/v0.1.3
[0.1.2]: https://github.com/anup-shesh/garmin-local-mcp/releases/tag/v0.1.2
[0.1.1]: https://github.com/anup-shesh/garmin-local-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/anup-shesh/garmin-local-mcp/releases/tag/v0.1.0
