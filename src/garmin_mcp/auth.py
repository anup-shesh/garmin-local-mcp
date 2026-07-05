"""Garmin Connect client factory.

The MCP server and sync engine only ever resume from stored tokens — they never
prompt. Interactive credential/MFA login lives in the `login` CLI subcommand.
Auth failures surface as AuthError with an actionable hint.
"""

from __future__ import annotations

from pathlib import Path

LOGIN_HINT = "run: garmin-local-mcp login"


class AuthError(Exception):
    def __init__(self, message: str, hint: str = LOGIN_HINT):
        super().__init__(message)
        self.hint = hint

    def to_dict(self) -> dict:
        return {"error": str(self), "hint": self.hint}


def has_tokens(tokens_dir: Path) -> bool:
    return tokens_dir.is_dir() and any(tokens_dir.iterdir())


def get_client(tokens_dir: Path):
    """Return a logged-in Garmin client resumed from stored tokens.

    Raises AuthError (never prompts) when tokens are absent, stale, or rejected.
    """
    from garminconnect import (
        Garmin,
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
    )

    if not has_tokens(tokens_dir):
        raise AuthError(f"No stored Garmin tokens in {tokens_dir}.")

    client = Garmin()  # no credentials: token resume only
    try:
        client.login(str(tokens_dir))
    except GarminConnectAuthenticationError as e:
        raise AuthError(f"Stored tokens were rejected: {e}") from e
    except GarminConnectTooManyRequestsError as e:
        raise AuthError(
            f"Rate limited by Garmin while resuming session: {e}",
            hint="wait a while and retry; reduce sync frequency",
        ) from e
    except GarminConnectConnectionError as e:
        raise AuthError(
            f"Could not reach Garmin Connect: {e}",
            hint="check network; Garmin may be down or blocking - retry later",
        ) from e
    return client


def status(tokens_dir: Path, live_check: bool = False) -> dict:
    """Token/auth status as a plain dict (shared by the CLI and the MCP tool)."""
    if not has_tokens(tokens_dir):
        return {
            "logged_in": False,
            "profile": None,
            "token_dir": str(tokens_dir),
            "error": "no stored tokens",
            "hint": LOGIN_HINT,
        }
    if not live_check:
        return {
            "logged_in": True,
            "profile": None,
            "token_dir": str(tokens_dir),
            "note": "tokens present (not validated against the API)",
        }
    try:
        client = get_client(tokens_dir)
        return {
            "logged_in": True,
            "profile": client.full_name or client.display_name,
            "token_dir": str(tokens_dir),
        }
    except AuthError as e:
        return {"logged_in": False, "profile": None, "token_dir": str(tokens_dir), **e.to_dict()}
