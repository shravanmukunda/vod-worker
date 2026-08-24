#!/usr/bin/env python3
"""One-time OAuth to create youtube_token.json for the upload channel."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main() -> None:
    load_dotenv()
    secrets = os.environ.get("YOUTUBE_CLIENT_SECRETS", "./client_secret.json")
    token_path = os.environ.get("YOUTUBE_TOKEN_FILE", "./youtube_token.json")
    flow = InstalledAppFlow.from_client_secrets_file(secrets, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(token_path, "w", encoding="utf-8") as handle:
        handle.write(creds.to_json())
    print(f"Wrote {token_path}")


if __name__ == "__main__":
    main()
