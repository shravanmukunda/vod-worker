#!/usr/bin/env python3
"""Poll Tourney for queued court VODs, cut matches, upload MP4s to R2."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE = os.environ.get("TOURNEY_API_BASE", "http://localhost:8082/api/v1").rstrip("/")
TOKEN = os.environ.get("VOD_WORKER_TOKEN", "")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "20"))
WORK_DIR = Path(os.environ.get("WORK_DIR", "./work")).resolve()
SKIP_UPLOAD = os.environ.get("SKIP_R2_UPLOAD", os.environ.get("SKIP_YOUTUBE_UPLOAD", "false")).lower() in (
    "1",
    "true",
    "yes",
)
YTDLP = os.environ.get("YTDLP_BIN", "yt-dlp")
FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
PUBLIC_APP_URL = os.environ.get("PUBLIC_APP_URL", "https://www.tourney24.com").rstrip("/")
YTDLP_COOKIES_FROM_BROWSER = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "").strip()
YTDLP_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
YTDLP_REMOTE_COMPONENTS = os.environ.get("YTDLP_REMOTE_COMPONENTS", "").strip()
R2_ENABLED = os.environ.get("R2_ENABLED", "false").lower() in ("1", "true", "yes")
R2_ENDPOINT = os.environ.get("R2_ENDPOINT", "").rstrip("/")
R2_BUCKET = os.environ.get("R2_BUCKET", "")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY", "")
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY", "")
R2_REGION = os.environ.get("R2_REGION", "auto")

HEADERS = {"X-Vod-Worker-Token": TOKEN, "Content-Type": "application/json"}


def log(message: str) -> None:
    print(message, flush=True)


def api_get(path: str) -> requests.Response:
    return requests.get(f"{API_BASE}{path}", headers=HEADERS, timeout=60)


def api_post(path: str, body: dict[str, Any]) -> requests.Response:
    return requests.post(f"{API_BASE}{path}", headers=HEADERS, json=body, timeout=60)


def api_patch(path: str, body: dict[str, Any]) -> requests.Response:
    return requests.patch(f"{API_BASE}{path}", headers=HEADERS, json=body, timeout=60)


def set_session_status(session_id: str, status: str, failure: str | None = None) -> None:
    body: dict[str, Any] = {"processStatus": status}
    if failure:
        body["failureReason"] = failure[:500]
    resp = api_post(f"/internal/vod-jobs/{session_id}/status", body)
    if resp.status_code >= 400:
        log(f"status update failed {resp.status_code}: {resp.text}")


def patch_vod(vod_id: str, body: dict[str, Any]) -> None:
    resp = api_patch(f"/internal/match-vods/{vod_id}", body)
    if resp.status_code >= 400:
        log(f"vod patch failed {vod_id} {resp.status_code}: {resp.text}")
        resp.raise_for_status()


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    log("$ " + " ".join(cmd))
    return subprocess.run(cmd, check=check, text=True)


def yt_dlp_base_cmd() -> list[str]:
    cmd = [YTDLP]
    if YTDLP_COOKIES_FROM_BROWSER:
        cmd.extend(["--cookies-from-browser", YTDLP_COOKIES_FROM_BROWSER])
    if YTDLP_COOKIES_FILE:
        cmd.extend(["--cookies", YTDLP_COOKIES_FILE])
    if YTDLP_REMOTE_COMPONENTS:
        cmd.extend(["--remote-components", YTDLP_REMOTE_COMPONENTS])
    return cmd


def download_source(video_id: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 1024 * 1024 and probe_duration(dest) > 60:
        log(f"reusing {dest}")
        return
    result = run(
        yt_dlp_base_cmd()
        + [
            "-f",
            "bv*[height<=1080]+ba/b[height<=1080]/b",
            "--merge-output-format",
            "mp4",
            # HLS/MPEG-TS fixup often exits 1 after a long download even when the
            # file is already usable; prefer keeping the downloaded media.
            "--fixup",
            "never",
            "-o",
            str(dest),
            f"https://www.youtube.com/watch?v={video_id}",
        ],
        check=False,
    )
    if result.returncode == 0 and dest.exists() and dest.stat().st_size > 1024:
        return
    # yt-dlp may exit non-zero after FixupM3u8 / skipped fragments while still
    # writing a playable day.mp4. Accept it if ffprobe can read a real duration.
    if dest.exists() and dest.stat().st_size > 1024 * 1024:
        duration = probe_duration(dest)
        if duration > 60:
            log(
                f"yt-dlp exited {result.returncode} but {dest.name} looks usable "
                f"({dest.stat().st_size} bytes, {duration:.1f}s); continuing"
            )
            return
    raise RuntimeError(
        f"yt-dlp failed for {video_id} (exit {result.returncode}); "
        f"output missing or unreadable at {dest}"
    )


def probe_duration(path: Path) -> float:
    out = subprocess.check_output(
        [
            FFMPEG.replace("ffmpeg", "ffprobe") if "ffmpeg" in FFMPEG else "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
    )
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


def cut_clip(src: Path, dest: Path, start: float, end: float) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            FFMPEG,
            "-y",
            "-ss",
            f"{start:.3f}",
            "-to",
            f"{end:.3f}",
            "-i",
            str(src),
            "-vf",
            "scale=-2:1080",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(dest),
        ]
    )


def r2_client():
    import boto3
    from botocore.config import Config

    if not (R2_ENABLED and R2_ENDPOINT and R2_BUCKET and R2_ACCESS_KEY and R2_SECRET_KEY):
        raise RuntimeError("R2 is not configured (R2_ENABLED / endpoint / bucket / keys)")
    # R2 is S3-compatible but does not fully support boto3's default flexible
    # checksum / aws-chunked wrapping. That wrapper is not seekable, so a mid-
    # upload retry raises "Need to rewind the stream ... stream is not seekable".
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto" if not R2_REGION or R2_REGION == "auto" else R2_REGION,
        config=Config(
            signature_version="s3v4",
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )


def storage_key(job: dict[str, Any], clip: dict[str, Any]) -> str:
    tournament_id = job.get("tournamentId") or "unknown"
    fixture_id = clip.get("fixtureId") or clip["vodId"]
    return f"vod/{tournament_id}/{fixture_id}.mp4"


def upload_r2(path: Path, key: str, *, attempts: int = 3) -> None:
    from boto3.s3.transfer import TransferConfig

    client = r2_client()
    extra = {"ContentType": "video/mp4"}
    # Prefer a seekable file body over upload_file's internal stream wrapping.
    transfer = TransferConfig(
        multipart_threshold=64 * 1024 * 1024,
        multipart_chunksize=32 * 1024 * 1024,
        max_concurrency=2,
        use_threads=True,
    )
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with path.open("rb") as handle:
                client.upload_fileobj(
                    handle,
                    R2_BUCKET,
                    key,
                    ExtraArgs=extra,
                    Config=transfer,
                )
            log(f"uploaded s3://{R2_BUCKET}/{key}")
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log(f"R2 upload attempt {attempt}/{attempts} failed for {key}: {exc}")
            if attempt < attempts:
                time.sleep(min(30, 2 ** attempt))
    assert last_exc is not None
    raise last_exc


def process_job(job: dict[str, Any]) -> None:
    session_id = job["sessionId"]
    video_id = job.get("externalVideoId")
    clips = job.get("clips") or []
    if not video_id:
        raise RuntimeError("Job has no externalVideoId")
    if not clips:
        raise RuntimeError("Job has no clips")

    pad_before = int(job.get("paddingBeforeSec") or 10)
    pad_after = int(job.get("paddingAfterSec") or 15)
    env_before = os.environ.get("PADDING_BEFORE_SEC")
    env_after = os.environ.get("PADDING_AFTER_SEC")
    if env_before:
        pad_before = int(env_before)
    if env_after:
        pad_after = int(env_after)

    session_dir = WORK_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    source = session_dir / "day.mp4"

    set_session_status(session_id, "DOWNLOADING")
    download_source(video_id, source)
    duration = probe_duration(source)

    set_session_status(session_id, "CUTTING")
    for clip in clips:
        vod_id = clip["vodId"]
        start = max(0.0, float(clip["sourceOffsetStartSec"]) - pad_before)
        end = float(clip["sourceOffsetEndSec"]) + pad_after
        if duration > 0:
            end = min(end, duration)
        if end <= start:
            log(
                f"invalid cut window for {vod_id}: start={start:.3f} end={end:.3f} "
                f"(raw {clip.get('sourceOffsetStartSec')}–{clip.get('sourceOffsetEndSec')}, "
                f"source_duration={duration:.3f})"
            )
            patch_vod(
                vod_id,
                {"status": "FAILED", "failureReason": "Invalid cut window after padding"},
            )
            continue
        out = session_dir / f"{vod_id}.mp4"
        if out.exists() and out.stat().st_size > 1024:
            log(f"reusing {out}")
            clip["_path"] = str(out)
            clip["_cut_duration"] = int(max(1, probe_duration(out) or (end - start)))
            continue
        cut_clip(source, out, start, end)
        clip["_path"] = str(out)
        clip["_cut_duration"] = int(max(1, end - start))

    if SKIP_UPLOAD:
        set_session_status(session_id, "DONE")
        log("SKIP_R2_UPLOAD set; clips left on disk, session marked DONE without storage keys")
        return

    set_session_status(session_id, "UPLOADING")
    for clip in clips:
        raw_path = clip.get("_path")
        if not raw_path:
            log(f"skipping upload for {clip.get('vodId')}: no cut file (invalid/missing window)")
            continue
        path = Path(raw_path)
        if not path.is_file():
            log(f"skipping upload for {clip.get('vodId')}: not a file ({path})")
            continue
        key = storage_key(job, clip)
        upload_r2(path, key)
        patch_vod(
            clip["vodId"],
            {
                "storageKey": key,
                "status": "PUBLISHED",
                "durationSeconds": clip.get("_cut_duration"),
            },
        )
        log(f"published {clip['vodId']} -> {key}")

    set_session_status(session_id, "DONE")
    shutil.rmtree(session_dir, ignore_errors=True)


def claim_job() -> dict[str, Any] | None:
    resp = api_get("/internal/vod-jobs")
    if resp.status_code == 204:
        return None
    resp.raise_for_status()
    payload = resp.json()
    return payload.get("data")


def main() -> int:
    if not TOKEN:
        log("VOD_WORKER_TOKEN is required")
        return 1
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    log(f"polling {API_BASE}/internal/vod-jobs every {POLL_SECONDS}s")
    while True:
        try:
            job = claim_job()
            if job:
                log(json.dumps({"claimed": job.get("sessionId"), "clips": len(job.get("clips") or [])}))
                try:
                    process_job(job)
                except Exception as exc:  # noqa: BLE001
                    log(f"job failed: {exc}")
                    set_session_status(job.get("sessionId", ""), "FAILED", str(exc))
            else:
                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:  # noqa: BLE001
            log(f"poll error: {exc}")
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
