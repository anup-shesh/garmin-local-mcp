"""Configuration loading.

Precedence: environment variables > config.toml in the data dir > defaults.
The data dir itself resolves as GARMIN_MCP_DATA_DIR > default ~/.garmin-mcp.
No path in this project is ever hardcoded to a machine-specific location.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA_DIR = Path.home() / ".garmin-mcp"


def _resolve_path(raw: str | os.PathLike) -> Path | None:
    """Resolve a configured path that may contain host template variables.

    MCP hosts are supposed to expand ``${HOME}``-style variables in extension
    config before launching the server, but some pass them through literally
    (Claude Desktop on Windows, where HOME is also typically unset — the server
    then tries to mkdir a directory literally named ``${HOME}`` and fails with
    Access is denied). Returns None for empty values or values that still
    contain an unexpanded ``${...}`` placeholder, so callers fall back to the
    default instead.
    """
    text = os.fspath(raw).strip()
    if not text:
        return None
    text = text.replace("${HOME}", str(Path.home()))
    text = os.path.expandvars(text)
    if "${" in text:
        return None
    return Path(text).expanduser()


def _resolve_path_or_default(raw: str | os.PathLike, source: str) -> Path:
    resolved = _resolve_path(raw)
    if resolved is None:
        # stderr only: stdout is the MCP transport.
        print(
            f"garmin-mcp: ignoring {source}={os.fspath(raw)!r} "
            f"(empty or unexpanded template variable); using {DEFAULT_DATA_DIR}",
            file=sys.stderr,
        )
        return DEFAULT_DATA_DIR
    return resolved

VALID_UNITS = ("metric", "statute")


@dataclass(frozen=True)
class Config:
    data_dir: Path
    timezone: str | None  # None = use system timezone
    units: str
    request_delay_seconds: float
    baseline_window_days: int

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.toml"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "garmin.db"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def tokens_dir(self) -> Path:
        env = os.environ.get("GARMINTOKENS")
        if env:
            resolved = _resolve_path(env)
            if resolved is not None:
                return resolved
        return self.data_dir / "tokens"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)


def _read_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def load(data_dir: str | os.PathLike | None = None) -> Config:
    """Load config. `data_dir` argument (e.g. from --data-dir) beats the env var."""
    if data_dir is not None:
        base = _resolve_path_or_default(data_dir, "--data-dir")
    else:
        env = os.environ.get("GARMIN_MCP_DATA_DIR")
        base = _resolve_path_or_default(env, "GARMIN_MCP_DATA_DIR") if env else DEFAULT_DATA_DIR

    file_cfg = _read_toml(base / "config.toml")

    units = str(file_cfg.get("units", "metric")).lower()
    if units not in VALID_UNITS:
        raise ValueError(f"config units must be one of {VALID_UNITS}, got {units!r}")

    return Config(
        data_dir=base,
        timezone=file_cfg.get("timezone"),
        units=units,
        request_delay_seconds=float(file_cfg.get("request_delay_seconds", 1.0)),
        baseline_window_days=int(file_cfg.get("baseline_window_days", 28)),
    )
