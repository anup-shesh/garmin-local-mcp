from pathlib import Path

from garmin_mcp.config import DEFAULT_DATA_DIR, load


def test_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("GARMIN_MCP_DATA_DIR", raising=False)
    monkeypatch.delenv("GARMINTOKENS", raising=False)
    cfg = load(tmp_path)
    assert cfg.data_dir == tmp_path
    assert cfg.units == "metric"
    assert cfg.request_delay_seconds == 1.0
    assert cfg.baseline_window_days == 28
    assert cfg.timezone is None
    assert cfg.db_path == tmp_path / "garmin.db"
    assert cfg.raw_dir == tmp_path / "raw"
    assert cfg.tokens_dir == tmp_path / "tokens"


def test_env_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMIN_MCP_DATA_DIR", str(tmp_path / "elsewhere"))
    cfg = load()
    assert cfg.data_dir == tmp_path / "elsewhere"


def test_default_data_dir_is_home():
    assert DEFAULT_DATA_DIR == Path.home() / ".garmin-mcp"


def test_env_data_dir_literal_home_template(monkeypatch):
    # Some MCP hosts pass manifest template variables through unexpanded
    # (Claude Desktop on Windows): expand ${HOME} ourselves.
    monkeypatch.setenv("GARMIN_MCP_DATA_DIR", "${HOME}/.garmin-mcp")
    cfg = load()
    assert cfg.data_dir == Path.home() / ".garmin-mcp"


def test_env_data_dir_unknown_template_falls_back(monkeypatch):
    monkeypatch.delenv("SOME_UNSET_VAR_XYZ", raising=False)
    monkeypatch.setenv("GARMIN_MCP_DATA_DIR", "${SOME_UNSET_VAR_XYZ}/data")
    cfg = load()
    assert cfg.data_dir == DEFAULT_DATA_DIR


def test_env_data_dir_empty_falls_back(monkeypatch):
    monkeypatch.setenv("GARMIN_MCP_DATA_DIR", "  ")
    cfg = load()
    assert cfg.data_dir == DEFAULT_DATA_DIR


def test_tokens_env_literal_template_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMINTOKENS", "${HOME_UNSET_XYZ}/tokens")
    cfg = load(tmp_path)
    assert cfg.tokens_dir == tmp_path / "tokens"


def test_config_toml(tmp_path, monkeypatch):
    monkeypatch.delenv("GARMINTOKENS", raising=False)
    (tmp_path / "config.toml").write_text(
        'timezone = "America/New_York"\nunits = "statute"\n'
        "request_delay_seconds = 2.5\nbaseline_window_days = 14\n"
    )
    cfg = load(tmp_path)
    assert cfg.timezone == "America/New_York"
    assert cfg.units == "statute"
    assert cfg.request_delay_seconds == 2.5
    assert cfg.baseline_window_days == 14


def test_tokens_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMINTOKENS", str(tmp_path / "tok"))
    cfg = load(tmp_path)
    assert cfg.tokens_dir == tmp_path / "tok"
