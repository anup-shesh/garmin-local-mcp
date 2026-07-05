"""Interactive login flow (the only place that ever prompts).

Kept out of the MCP server on purpose: `serve` must never block on stdin, so
credential + MFA entry happens here, once, and everything else resumes from
the token store.
"""

from __future__ import annotations

import getpass
import os
import shutil
import sys

from . import auth
from .config import Config


def run_login(config: Config, status_only: bool = False, logout: bool = False) -> int:
    tokens_dir = config.tokens_dir

    if status_only:
        info = auth.status(tokens_dir, live_check=True)
        for key, value in info.items():
            print(f"{key}: {value}")
        return 0 if info.get("logged_in") else 1

    if logout:
        if tokens_dir.is_dir():
            shutil.rmtree(tokens_dir)
            print(f"Removed token store: {tokens_dir}")
        else:
            print(f"No token store at {tokens_dir}")
        return 0

    if auth.has_tokens(tokens_dir):
        info = auth.status(tokens_dir, live_check=True)
        if info.get("logged_in"):
            print(f"Already logged in as: {info.get('profile')}")
            print(f"Token store: {tokens_dir}")
            return 0
        print(f"Stored tokens are stale ({info.get('error')}); logging in fresh.\n")

    email = os.environ.get("GARMIN_EMAIL") or input("Garmin Connect email: ")
    password = os.environ.get("GARMIN_PASSWORD") or getpass.getpass("Password: ")

    from garminconnect import (
        Garmin,
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
    )

    def prompt_mfa() -> str:
        return input("MFA code: ").strip()

    tokens_dir.mkdir(parents=True, exist_ok=True)
    client = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)
    try:
        client.login(str(tokens_dir))
    except GarminConnectAuthenticationError as e:
        print(f"Login failed (authentication): {e}", file=sys.stderr)
        return 1
    except GarminConnectTooManyRequestsError as e:
        print(f"Login failed (rate limited): {e}\nWait a while and retry.", file=sys.stderr)
        return 1
    except GarminConnectConnectionError as e:
        print(f"Login failed (connection): {e}", file=sys.stderr)
        return 1

    print(f"\nLogged in as: {client.full_name or client.display_name}")
    print(f"Tokens saved to: {tokens_dir}")
    print("Future runs (sync, serve) resume from tokens - no password needed.")
    return 0
