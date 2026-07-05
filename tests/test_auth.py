from garmin_mcp import auth


def test_no_tokens_status(tmp_path):
    info = auth.status(tmp_path / "tokens")
    assert info["logged_in"] is False
    assert info["hint"] == auth.LOGIN_HINT


def test_tokens_present_without_live_check(tmp_path):
    tok = tmp_path / "tokens"
    tok.mkdir()
    (tok / "oauth2_token.json").write_text("{}")
    info = auth.status(tok)
    assert info["logged_in"] is True
    assert "not validated" in info["note"]


def test_get_client_without_tokens_raises(tmp_path):
    try:
        auth.get_client(tmp_path / "tokens")
        raised = False
    except auth.AuthError as e:
        raised = True
        assert e.hint == auth.LOGIN_HINT
        assert "No stored Garmin tokens" in str(e)
    assert raised
