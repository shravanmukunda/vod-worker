#!/usr/bin/env python3
"""Upload match VOD MP4s from R2 to YouTube (one-by-one backfill)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

from worker import (
    WORK_DIR,
    YOUTUBE_PRIVACY,
    api_get,
    download_r2,
    log,
    patch_vod,
    r2_client,
    upload_youtube_clip,
    R2_BUCKET,
)
from youtube_upload import youtube_enabled

load_dotenv()

QUEUE_PATHS = (
    "/internal/youtube-upload-queue",
    "/internal/match-vods/pending-youtube",
)

MANIFEST_SQL = """
SELECT
    m.id,
    m.storage_key,
    m.home_player_label,
    m.away_player_label,
    m.event_name,
    m.score_snapshot,
    m.court_name,
    m.tournament_id,
    t.name AS tournament_name
FROM match_vod_assets m
LEFT JOIN tournaments t ON t.id = m.tournament_id
WHERE m.storage_key IS NOT NULL
  AND BTRIM(m.storage_key) <> ''
  AND (m.youtube_video_id IS NULL OR BTRIM(m.youtube_video_id) = '')
ORDER BY m.started_at ASC NULLS LAST, m.created_at ASC
"""


def db_dsn() -> str | None:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    host = os.environ.get("DB_HOST", "").strip()
    if not host:
        return None
    port = os.environ.get("DB_PORT", "5432").strip()
    name = os.environ.get("DB_NAME", "tourney").strip()
    user = os.environ.get("DB_USER", "postgres").strip()
    password = os.environ.get("DB_PASS", "").strip()
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


def fetch_pending_from_api() -> list[dict[str, Any]] | None:
    last_error: Exception | None = None
    for path in QUEUE_PATHS:
        resp = api_get(path)
        if resp.status_code == 404:
            continue
        if resp.status_code == 405:
            continue
        try:
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
        payload = resp.json()
        rows = payload.get("data") or []
        log(f"Loaded {len(rows)} clip(s) from API {path}")
        return rows
    if last_error:
        log(f"API queue lookup failed: {last_error}")
    return None


def fetch_pending_from_db() -> list[dict[str, Any]]:
    dsn = db_dsn()
    if not dsn:
        raise RuntimeError(
            "No upload queue API on production yet. Set DATABASE_URL (prod Postgres) in .env "
            "or deploy the backend update that adds GET /internal/youtube-upload-queue."
        )
    import psycopg2
    from psycopg2.extras import RealDictCursor

    parsed = urlparse(dsn)
    log(f"Loading queue from database {parsed.hostname}:{parsed.port or 5432}/{parsed.path.lstrip('/')}")
    with psycopg2.connect(dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(MANIFEST_SQL)
        rows = cur.fetchall()
    manifest = []
    for row in rows:
        manifest.append(
            {
                "id": row["id"],
                "storageKey": row["storage_key"],
                "homePlayerLabel": row["home_player_label"],
                "awayPlayerLabel": row["away_player_label"],
                "eventName": row["event_name"],
                "scoreSnapshot": row["score_snapshot"],
                "courtName": row["court_name"],
                "tournamentId": row["tournament_id"],
                "tournamentName": row["tournament_name"],
            }
        )
    log(f"Loaded {len(manifest)} clip(s) from database")
    return manifest


def count_r2_mp4s() -> int:
    client = r2_client()
    paginator = client.get_paginator("list_objects_v2")
    count = 0
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix="vod/"):
        for obj in page.get("Contents") or []:
            if obj["Key"].endswith(".mp4"):
                count += 1
    return count


def fetch_pending() -> list[dict[str, Any]]:
    rows = fetch_pending_from_api()
    if rows is not None:
        return rows
    return fetch_pending_from_db()


def backfill_vod(vod: dict[str, Any], *, work_dir: Path, dry_run: bool = False) -> None:
    vod_id = vod["id"]
    storage_key = vod.get("storageKey")
    if not storage_key:
        raise RuntimeError(f"{vod_id} has no storageKey")
    local_path = work_dir / f"{vod_id}.mp4"
    if dry_run:
        from youtube_upload import build_description, build_title

        job = {
            "tournamentId": vod.get("tournamentId"),
            "tournamentName": vod.get("tournamentName"),
            "courtName": vod.get("courtName"),
        }
        clip = {
            "vodId": vod_id,
            "id": vod_id,
            "homePlayerLabel": vod.get("homePlayerLabel"),
            "awayPlayerLabel": vod.get("awayPlayerLabel"),
            "eventName": vod.get("eventName"),
            "scoreSnapshot": vod.get("scoreSnapshot"),
        }
        log(f"DRY RUN {vod_id} -> {build_title({**clip, **job})} ({storage_key})")
        return
    if not local_path.exists() or local_path.stat().st_size < 1024:
        download_r2(
            storage_key,
            local_path,
            label=f"[download] {vod_id[:8]}",
        )
    job = {
        "tournamentId": vod.get("tournamentId"),
        "tournamentName": vod.get("tournamentName"),
        "courtName": vod.get("courtName"),
    }
    clip = {
        "vodId": vod_id,
        "id": vod_id,
        "homePlayerLabel": vod.get("homePlayerLabel"),
        "awayPlayerLabel": vod.get("awayPlayerLabel"),
        "eventName": vod.get("eventName"),
        "scoreSnapshot": vod.get("scoreSnapshot"),
    }
    label = f"[youtube] {vod_id[:8]}"
    video_id = upload_youtube_clip(local_path, clip=clip, job=job, label=label)
    patch_vod(
        vod_id,
        {
            "youtubeVideoId": video_id,
            "youtubePrivacy": YOUTUBE_PRIVACY,
        },
    )
    local_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload match VODs from R2 to YouTube")
    parser.add_argument(
        "--vod-id",
        action="append",
        dest="vod_ids",
        help="Upload specific VOD id(s) only",
    )
    parser.add_argument(
        "--manifest",
        help="JSON array file with VOD rows (id, storageKey, labels, tournamentName, ...)",
    )
    parser.add_argument(
        "--work-dir",
        default=str(WORK_DIR / "youtube-backfill"),
        help="Temporary directory for R2 downloads",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Upload at most N clips (0 = all)",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Keep going if one clip fails",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print titles/keys only; do not download or upload",
    )
    args = parser.parse_args()

    from worker import API_BASE, TOKEN

    if not TOKEN:
        log("VOD_WORKER_TOKEN is required")
        return 1
    if not args.dry_run and not youtube_enabled():
        log("YouTube upload is not configured. Run: python youtube_oauth.py")
        return 1

    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    log(f"API {API_BASE} · privacy={YOUTUBE_PRIVACY}")

    try:
        r2_count = count_r2_mp4s()
        log(f"R2 bucket has {r2_count} mp4 object(s) under vod/")
    except Exception as exc:  # noqa: BLE001
        log(f"Could not count R2 objects: {exc}")

    if args.manifest:
        pending = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        if not isinstance(pending, list):
            raise RuntimeError("--manifest must contain a JSON array")
    else:
        pending = fetch_pending()

    if args.vod_ids:
        wanted = set(args.vod_ids)
        pending = [row for row in pending if row.get("id") in wanted]
        missing = wanted - {row.get("id") for row in pending}
        for vod_id in sorted(missing):
            log(f"warning: {vod_id} not found in queue")

    if args.limit and args.limit > 0:
        pending = pending[: args.limit]

    if not pending:
        log("No clips pending YouTube upload")
        return 0

    log(f"Queue: {len(pending)} clip(s) to upload")
    failures = 0
    for index, vod in enumerate(pending, start=1):
        vod_id = vod.get("id", "?")
        log(f"--- clip {index}/{len(pending)} {vod_id} ---")
        try:
            backfill_vod(vod, work_dir=work_dir, dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            log(f"failed {vod_id}: {exc}")
            if not args.continue_on_error:
                return 1
    if failures:
        log(f"Finished with {failures} failure(s)")
        return 1
    log(f"Done. Processed {len(pending)} clip(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
