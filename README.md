# garmin-local-mcp

**Local-first Garmin data warehouse with an analysis-grade MCP server.**
Sync once, analyze forever — even when the API is down.

> ⚠️ Work in progress. Not yet published to PyPI.

## Why another Garmin MCP?

Every existing Garmin MCP server is a live-API thin wrapper: each question your
AI assistant asks becomes one or more calls to Garmin's rate-limited, unofficial
API, returning huge raw JSON blobs. Multi-month questions ("how does my sleep
correlate with training load?") are impractical, and when Garmin changes its
auth (as it did in March 2026, breaking the entire ecosystem), those servers go
completely dark — even for data you already fetched.

This project inverts the architecture:

- **Sync once, analyze forever.** Incremental sync into a local warehouse:
  immutable raw JSON snapshots + SQLite, in a directory you own.
- **Server-side analysis, compact responses.** Trends, correlations, personal
  baselines, and anomaly detection are computed locally and returned as small
  tables in a single tool call — no context-blowing JSON dumps.
- **Offline resilience.** An API breakage pauses new syncs only; every query
  over already-synced history keeps working.
- **A zero-auth fallback.** A standalone decoder for Garmin's undocumented
  wellness FIT messages (sleep score, HRV, skin temperature, sleep stages,
  naps) lets you ingest manually exported bundles with no login at all.
- **Curated tools.** ~12 composable tools, not 110.

## Status

Under active development. Roadmap: auth → local store → sync engine →
analysis tools + MCP server → FIT fallback → PyPI launch.

## Disclaimer

This project is not affiliated with, endorsed by, or supported by Garmin Ltd.
It uses the community [python-garminconnect](https://github.com/cyberjunky/python-garminconnect)
library with your own credentials to access your own data. Unofficial APIs can
break at any time. All data stays on your machine; nothing phones home.

## License

MIT
