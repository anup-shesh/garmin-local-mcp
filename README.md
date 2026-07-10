<!-- mcp-name: io.github.anup-shesh/garmin-local-mcp -->

# garmin-local-mcp

**Local-first Garmin data warehouse with an analysis-grade MCP server.**
Sync once, analyze forever, even when the API is down.

## Why another Garmin MCP?

Every existing Garmin MCP server follows the same design: a thin live wrapper
around Garmin's rate-limited, unofficial API. Each question your AI assistant
asks becomes one or more live API calls that return huge raw JSON blobs (a
single raw sleep response runs around 230 KB). Multi-month questions like "how
does my sleep correlate with training load?" are impractical, and when Garmin
changes its auth (as it did in March 2026, breaking the whole ecosystem), those
servers go completely dark, even for data they already fetched yesterday.

This project inverts the architecture:

- **Sync once, analyze forever.** Incremental sync into a local warehouse:
  immutable raw JSON snapshots plus a SQLite database, in a directory you own.
- **Server-side analysis, compact responses.** Trends, correlations, personal
  baselines, and anomaly detection are computed locally and returned as small
  columnar tables in a single tool call. Typical responses are under 2 KB, so
  nothing floods the model's context.
- **Offline resilience.** An API breakage pauses new syncs only. Every query
  over already-synced history keeps working.
- **A zero-auth fallback.** A standalone decoder for Garmin's undocumented
  wellness FIT messages (sleep score, HRV, skin temperature, sleep stages,
  naps) ingests manually exported bundles with no login at all. No other
  Garmin MCP ships this.
- **Curated tools.** 12 composable tools, not 110.

| | garmin-local-mcp | Typical API-wrapper Garmin MCPs |
|---|---|---|
| Local data store you own | Yes (raw JSON + SQLite) | No |
| Works offline after an API breakage | Yes (analysis over synced history) | No |
| Server-side analysis (trends, correlations, baselines, anomalies) | Yes | No (raw JSON pass-through) |
| Response size discipline | Compact columnar tables, typically < 2 KB | Raw payloads, up to hundreds of KB |
| Zero-auth ingest path | Yes (FIT bundle import) | No |
| Tool count | 12 curated | Often 20 to 110+ |

## Quickstart

Requires Python 3.12+.

```
pip install garmin-local-mcp
```

Or run it without installing, via [uv](https://docs.astral.sh/uv/):

```
uvx garmin-local-mcp --help
```

**1. Log in once** (MFA supported; tokens persist locally, so future runs never
ask for a password):

```
garmin-local-mcp login
```

**2. Backfill your history.** The sync is resumable, safe to interrupt, and
throttled to be polite to Garmin's servers. A year of history is roughly 1,800
requests; for long backfills, start it and let it run (overnight works well).
If it gets rate limited or interrupted, re-run the same command and it resumes
where it left off.

```
garmin-local-mcp sync --from 2026-01-01
```

**3. Register the MCP server with your client** (see [Client setup](#client-setup)
for Claude Desktop, Cursor, and other clients):

```
claude mcp add --scope user garmin -- garmin-local-mcp serve
```

**4. Ask questions.** Examples of what Claude can now answer from your local
warehouse in one or two tool calls:

- "How does my sleep score correlate with next-day resting HR?"
- "What were my anomalous HRV days this quarter?"
- "Show weekly training load vs sleep for the last 3 months."

## Client setup

The server speaks stdio, so any MCP client works. `pip install garmin-local-mcp`
first (or use the `uvx` variants below, which need nothing installed beyond
[uv](https://docs.astral.sh/uv/)).

**Claude Code**

```
claude mcp add --scope user garmin -- garmin-local-mcp serve
```

**Claude Desktop, one-click:** download `garmin-local-mcp-x.y.z.mcpb` from the
[latest release](https://github.com/anup-shesh/garmin-local-mcp/releases/latest)
and double-click it (or drag it into Claude Desktop's Settings > Extensions).
No Python setup needed; Claude Desktop's bundled uv runtime handles it.

**Claude Desktop, manual** (Settings, then Developer, then Edit Config; add to
`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "garmin": {
      "command": "garmin-local-mcp",
      "args": ["serve"]
    }
  }
}
```

**Cursor** (`~/.cursor/mcp.json`, or `.cursor/mcp.json` in a project):

```json
{
  "mcpServers": {
    "garmin": {
      "command": "garmin-local-mcp",
      "args": ["serve"]
    }
  }
}
```

**Any other stdio client / no local install** (requires uv):

```json
{
  "mcpServers": {
    "garmin": {
      "command": "uvx",
      "args": ["garmin-local-mcp", "serve"]
    }
  }
}
```

Note: `login` and the initial backfill `sync` are CLI steps (see
[Quickstart](#quickstart)); the MCP server itself never prompts for
credentials.

## The 12 tools

| Tool | What it does |
|---|---|
| `auth_status` | Check whether stored Garmin Connect tokens exist (use before sync, or after an auth error). |
| `sync` | Fetch up to 60 days from Garmin Connect into the local store (default: last 30 days ending yesterday; big backfills belong in the CLI). |
| `sync_status` | Local data coverage per table, last sync time, and pending sync errors. |
| `get_day` | One merged view of a single day: wellness, sleep, HRV, training status, activities, and data-quality flags. |
| `query_metrics` | Columnar time series for one or more metrics between two dates, with daily/weekly/monthly aggregation and optional stats. |
| `correlate` | Pearson/Spearman correlation between two metrics, with day-lag support and an optional scan over lags -7..+7. |
| `baselines` | Personal mean +/- sd band per metric over a trailing window (default 28 days), to judge what is normal for this user. |
| `anomalies` | Outlier days (z-score deviations) and sustained streaks (5+ consecutive days on one side of the mean). |
| `list_activities` | Recent activities newest-first as a compact table, filterable by type, date range, and minimum distance. |
| `get_activity` | Full stored summary row for one activity (summary fields only, no GPS or sample streams). |
| `gaps` | Missing days per table plus unresolved sync errors, to find holes worth re-syncing before drawing conclusions. |
| `import_fit` | Zero-auth offline ingest of a manually exported Garmin wellness FIT bundle. |

Only `sync` and `import_fit` write anything, and only inside the data
directory. The server never prompts: auth problems come back as structured
errors with a hint pointing at the login CLI.

Available metric names include `resting_hr`, `sleep_score`, `hrv`, `steps`,
`stress_avg`, `body_battery_high`, `skin_temp_dev_c`, `vo2max`,
`training_load`, and about 25 more; any tool given an unknown name returns the
full list.

## Data layout and ownership

Everything lives in one directory you own (default `~/.garmin-mcp`, override
with the `GARMIN_MCP_DATA_DIR` environment variable or `--data-dir`):

```
~/.garmin-mcp/
├── config.toml                                  # optional settings
├── tokens/                                      # Garmin Connect session tokens
├── raw/daily/YYYY/YYYY-MM-DD/<endpoint>.json    # immutable raw API snapshots
├── raw/activities/<activity_id>.json            # one snapshot per activity
└── garmin.db                                    # SQLite warehouse
```

The raw JSON snapshots are the source of truth and are never overwritten. The
SQLite database is a derived, rebuildable index: `garmin-local-mcp reparse`
rebuilds it from the raw snapshots entirely offline, which is the universal
escape hatch for schema evolution and parser fixes. Your data never leaves
your machine.

## Data quality note

Garmin watches report a provisional on-device resting heart rate that can
diverge sharply from Garmin Connect's finalized value on nights with sparse
sampling. A real observed case: the watch reported 69 bpm on-device while
Garmin Connect later finalized the same night at 56 bpm.

This project handles that in two ways:

- The API sync stores Garmin Connect's finalized value.
- The FIT importer cross-checks the provisional on-device value against the
  overnight heart-rate floor. A resting HR sitting more than 10 bpm above the
  lowest overnight sample is a rate the watch never actually observed; it gets
  flagged (`rhr_far_above_hr_floor`) and withheld, leaving the field for the
  API to backfill rather than storing a misleading number.

Sparse sleep-stage logging is flagged the same way
(`sparse_sleep_stage_logging`), and flags surface in `get_day` so the analysis
layer knows which numbers to trust.

## Offline / fallback runbook

If Garmin breaks the unofficial API again (it has before):

1. **Everything analytical keeps working.** All query, correlation, baseline,
   anomaly, and gap tools run on your already-synced local history. Only new
   syncs pause.
2. **Keep ingesting without auth.** Download a daily FIT bundle from the
   Garmin Connect website and import it locally (exact steps below).
   `garmin-local-mcp import-fit <folder>` decodes the bundle with zero
   authentication and fills the gap days. FIT-sourced rows never overwrite
   API-sourced rows (unless you pass `--force`).
3. **Resume when the community catches up.** Watch the
   [python-garminconnect](https://github.com/cyberjunky/python-garminconnect)
   project for a fix, upgrade, and run `garmin-local-mcp sync` again. Thanks
   to resumable sync state, it picks up exactly where it stopped.

### Downloading a wellness bundle, step by step

1. Sign in at [connect.garmin.com](https://connect.garmin.com) in any
   browser.
2. Go directly to
   **<https://connect.garmin.com/app/settings/accountInformation>**
   (or click your avatar in the top-right corner, then **Settings**, then
   **Account Information** in the left sidebar).
3. Scroll to the bottom of the page, to the section titled
   **Export Wellness Data** ("Download your wellness FIT files from a
   specific day. This includes data such as steps, sleep, stress, HRV and
   more.").
4. Pick a date in the **Date** field and click **Export**. Your browser
   downloads a small zip for that one day, containing roughly 12 to 15
   binary `.fit` files (`*_WELLNESS.fit`, `*_SLEEP_DATA.fit`,
   `*_HRV_STATUS.fit`, `*_SKIN_TEMP.fit`, `*_METRICS.fit`, and similar).
5. Unzip it into a folder and run:

   ```
   garmin-local-mcp import-fit "path/to/unzipped/folder"
   ```

6. Repeat for each missing day (one bundle per date). The `gaps` tool or
   `garmin-local-mcp status` tells you which days need filling.

Two things worth knowing:

- **Overnight sleep belongs to the wake date.** To get last night's sleep,
  export yesterday's date if you slept into this morning, i.e. the date you
  woke up on.
- This per-day export is instant and separate from Garmin's full account
  export (the "Data Management" link on the same page), which is a bulk
  archive that can take days to arrive by email and is not what
  `import-fit` expects.

## Configuration

Optional `config.toml` in the data directory:

| Key | Default | Meaning |
|---|---|---|
| `timezone` | system timezone | IANA name (e.g. `America/Denver`) used to compute "yesterday" for sync ranges |
| `units` | `metric` | `metric` or `statute` |
| `request_delay_seconds` | `1.0` | Delay between API requests during sync |
| `baseline_window_days` | `28` | Default trailing window for the `baselines` tool |

Environment variables:

| Variable | Meaning |
|---|---|
| `GARMIN_MCP_DATA_DIR` | Override the data directory (default `~/.garmin-mcp`) |
| `GARMINTOKENS` | Override the token store location (default `<data_dir>/tokens`) |
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | Optional, for non-interactive re-login; when set, `garmin-local-mcp login` skips the prompts (MFA may still prompt if your account requires it) |

## Development

```
python -m venv .venv
.venv/bin/pip install -e .[dev]     # Windows: .venv\Scripts\pip install -e .[dev]
pytest
ruff check .
```

The test suite runs fully offline against sanitized JSON fixtures and small
FIT samples; CI never touches the live API.

## Disclaimer

This project is not affiliated with, endorsed by, or supported by Garmin Ltd.
It uses the community [python-garminconnect](https://github.com/cyberjunky/python-garminconnect)
library with your own credentials to access your own data. Garmin's APIs are
unofficial and can change or break at any time; when that happens, your synced
history remains fully usable and the FIT import path keeps working.

All data stays on your machine. Nothing phones home: no telemetry, no
third-party services, no cloud. Treat your data directory like the personal
health record it is, and never commit it to a repository.

## License

MIT
