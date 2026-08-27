"""Upload match VOD MP4s to YouTube via the Data API."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
DEFAULT_CATEGORY_ID = "17"  # Sports


def youtube_enabled() -> bool:
    skip = os.environ.get("SKIP_YOUTUBE_UPLOAD", "false").lower() in ("1", "true", "yes")
    if skip:
        return False
    token_path = Path(os.environ.get("YOUTUBE_TOKEN_FILE", "./youtube_token.json"))
    return token_path.is_file()


def build_title(vod: dict[str, Any]) -> str:
    home = (vod.get("homePlayerLabel") or "Home").strip()
    away = (vod.get("awayPlayerLabel") or "Away").strip()
    tournament = (vod.get("tournamentName") or "").strip()
    title = f"{home} v {away}"
    if tournament:
        title = f"{title} | {tournament}"
    return title[:100]


def build_description(vod: dict[str, Any]) -> str:
    lines: list[str] = []
    event = (vod.get("eventName") or "").strip()
    court = (vod.get("courtName") or "").strip()
    score = (vod.get("scoreSnapshot") or "").strip()
    if event:
        lines.append(event)
    if court:
        lines.append(f"Court: {court}")
    if score:
        lines.append(f"Score: {score}")
    app_url = os.environ.get("PUBLIC_APP_URL", "https://www.tourney24.com").rstrip("/")
    tournament_id = vod.get("tournamentId")
    vod_id = vod.get("id") or vod.get("vodId")
    if tournament_id and vod_id:
        lines.append(f"{app_url}/tournaments/{tournament_id}/videos/{vod_id}")
    return "\n".join(lines)[:4900]


def _load_credentials() -> Credentials:
    token_path = os.environ.get("YOUTUBE_TOKEN_FILE", "./youtube_token.json")
    creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, "w", encoding="utf-8") as handle:
            handle.write(creds.to_json())
    if not creds.valid:
        raise RuntimeError(
            f"YouTube OAuth token at {token_path} is missing or expired. "
            "Run: python youtube_oauth.py"
        )
    return creds


def upload_video(
    path: Path,
    *,
    title: str,
    description: str,
    privacy: str,
    on_progress: Any | None = None,
) -> str:
    youtube = build("youtube", "v3", credentials=_load_credentials())
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "categoryId": os.environ.get("YOUTUBE_CATEGORY_ID", DEFAULT_CATEGORY_ID),
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(
        str(path),
        chunksize=8 * 1024 * 1024,
        resumable=True,
        mimetype="video/mp4",
    )
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    last_emit = 0.0
    while response is None:
        status, response = request.next_chunk()
        if status and on_progress:
            now = time.monotonic()
            if now - last_emit >= float(os.environ.get("PROGRESS_INTERVAL_SEC", "2")):
                on_progress(min(100.0, status.progress() * 100.0))
                last_emit = now
    if response is None:
        raise RuntimeError("YouTube upload finished without a response")
    video_id = response.get("id")
    if not video_id:
        raise RuntimeError(f"YouTube upload response missing id: {response}")
    return video_id
