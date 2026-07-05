# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/anup-shesh/garmin-local-mcp/releases/tag/v0.1.0
