"""MCP server tests: in-process, offline, against a tmp data dir.

Tools are called as plain functions (@mcp.tool() registers and returns the
function unchanged); the data dir is redirected via GARMIN_MCP_DATA_DIR and
the server's cached config is reset per test. No network anywhere.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from garmin_mcp import db, server

BASE = date(2026, 6, 1)

EXPECTED_TOOLS = {
    "auth_status", "sync", "sync_status", "get_day", "query_metrics", "correlate",
    "baselines", "anomalies", "list_activities", "get_activity", "gaps", "import_fit",
}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMIN_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "_config", None)  # drop any cached config
    return tmp_path


def seed_month(data_dir) -> None:
    conn = db.connect(data_dir / "garmin.db")
    for i in range(30):
        d = (BASE + timedelta(days=i)).isoformat()
        db.upsert(
            conn,
            "daily_wellness",
            {"date": d, "resting_hr": 55 + i % 5, "steps": 8000 + 100 * i, "source": "api"},
            ("date",),
        )
        db.upsert(conn, "sleep", {"date": d, "score": 80 + i % 3, "source": "api"}, ("date",))
        db.mark_sync(conn, "usersummary", d, "ok")
    conn.close()


def test_all_twelve_tools_registered():
    names = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert names == EXPECTED_TOOLS


def test_auth_status_without_tokens(data_dir):
    res = server.auth_status()
    assert res["logged_in"] is False
    assert "login" in res["hint"]


def test_sync_over_60_days_points_at_the_cli(data_dir):
    res = server.sync(start="2026-01-01", end="2026-06-30")
    assert "60" in res["error"] and "CLI" in res["error"]
    assert "garmin-local-mcp sync" in res["error"]


def test_sync_without_tokens_returns_structured_auth_error(data_dir):
    # default 30-day range passes the cap, then fails auth (no tokens, no network)
    res = server.sync()
    assert "error" in res and "login" in res["hint"]


def test_sync_rejects_inverted_range(data_dir):
    res = server.sync(start="2026-06-30", end="2026-06-01")
    assert "after" in res["error"]


def test_query_metrics_weekly_response_is_compact(data_dir):
    seed_month(data_dir)
    res = server.query_metrics(
        ["resting_hr", "sleep_score"], "2026-06-01", "2026-06-30",
        aggregate="weekly", stats=True,
    )
    payload = json.dumps(res)
    assert len(payload.encode()) < 2000
    assert res["cols"] == ["date", "resting_hr", "sleep_score"]
    assert res["n"] == 5  # June 2026 spans five ISO weeks


def test_query_metrics_bad_metric_is_a_structured_error(data_dir):
    res = server.query_metrics(["nope"], "2026-06-01", "2026-06-30")
    assert "Unknown metric" in res["error"]


def test_get_day_on_empty_store(data_dir):
    res = server.get_day("2026-06-01")
    assert res["date"] == "2026-06-01"
    assert res["wellness"] == {} and res["activities"] == []


def test_get_activity_unknown_id(data_dir):
    res = server.get_activity(999)
    assert res == {"error": "No activity with id 999"}


def test_gaps_defaults_to_first_synced_date(data_dir):
    seed_month(data_dir)
    res = server.gaps(end="2026-06-30")
    assert res["start"] == "2026-06-01"
    assert res["missing"]["daily_wellness"] == []
    assert res["missing"]["hrv"]  # hrv never seeded: every day is a gap


def test_gaps_without_any_sync(data_dir):
    res = server.gaps()
    assert "sync" in res["error"]


def test_baselines_uses_config_default_window(data_dir):
    seed_month(data_dir)
    res = server.baselines(metrics=["resting_hr"])
    assert res["window_days"] == 28  # config default
    assert res["metrics"]["resting_hr"]["n"] == 28


def test_sync_status_reports_coverage(data_dir):
    seed_month(data_dir)
    res = server.sync_status()
    assert res["coverage"]["daily_wellness"]["rows"] == 30
    assert res["coverage"]["daily_wellness"]["first"] == "2026-06-01"
    assert res["coverage"]["activities"]["rows"] == 0
    assert res["pending_errors"] == 0 and res["recent_errors"] == []
    assert res["last_sync"] is not None


def test_import_fit_bad_folder_is_a_structured_error(data_dir, tmp_path):
    res = server.import_fit(str(tmp_path / "no-such-bundle"))
    assert "error" in res


def test_responses_json_serialize(data_dir):
    seed_month(data_dir)
    for res in (
        server.sync_status(),
        server.get_day("2026-06-05"),
        server.list_activities(),
        server.anomalies(metrics=["resting_hr"], start="2026-06-01", end="2026-06-30"),
        server.correlate("resting_hr", "sleep_score", "2026-06-01", "2026-06-30"),
    ):
        json.dumps(res)  # must be plain JSON-able
