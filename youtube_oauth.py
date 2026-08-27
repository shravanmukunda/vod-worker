#!/usr/bin/env python3
"""One-time OAuth to create youtube_token.json for the upload channel."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

SETUP_HELP = """
YouTube upload needs OAuth client credentials (not the YOUTUBE_API_KEY).

1. Open https://console.cloud.google.com/apis/credentials
2. Pick the project that has YouTube Data API v3 enabled
3. Create credentials → OAuth client ID → Desktop app
4. Download JSON and save it as:
     vod-worker/client_secret.json
   Or set YOUTUBE_CLIENT_SECRETS=/absolute/path/to/the.json in .env

5. In OAuth consent screen, add your Google account as a test user
   (if the app is in "Testing" mode).

6. Re-run: python youtube_oauth.py
   Sign in with the Google account that owns the @tourney24 YouTube channel.
"""


def main() -> None:
    load_dotenv()
    secrets = os.environ.get("YOUTUBE_CLIENT_SECRETS", "./client_secret.json")
    token_path = os.environ.get("YOUTUBE_TOKEN_FILE", "./youtube_token.json")
    secrets_path = Path(secrets).expanduser().resolve()
    if not secrets_path.is_file():
        print(f"Missing OAuth client secrets file: {secrets_path}", file=sys.stderr)
        print(SETUP_HELP, file=sys.stderr)
        raise SystemExit(1)
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
    creds = flow.run_local_server(port=0)
    out = Path(token_path).expanduser().resolve()
    out.write_text(creds.to_json(), encoding="utf-8")
    print(f"Wrote {out}")
    print("Next: python youtube_backfill.py")


if __name__ == "__main__":
    main()
