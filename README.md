# VOD worker

Sidecar that claims queued court-day jobs from Tourney, downloads the YouTube VOD with yt-dlp, cuts matches with ffmpeg, uploads clips with the YouTube Data API, and PATCHes match VOD rows.

## Requirements

- Python 3.11+
- `yt-dlp` and `ffmpeg` on PATH
- Backend `VOD_WORKER_TOKEN` matching this worker
- YouTube OAuth client (installed app) + a one-time refresh token for the channel that owns the streams

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Place `client_secret.json` from Google Cloud (YouTube Data API v3 enabled) next to this folder or set `YOUTUBE_CLIENT_SECRETS`.

First run with `python youtube_oauth.py` to store `youtube_token.json`.

```bash
python worker.py
```

If YouTube starts requiring browser-backed access or EJS challenge solving, add these to `.env`:

```bash
YTDLP_COOKIES_FROM_BROWSER=chrome
YTDLP_REMOTE_COMPONENTS=ejs:github
```

You can also point `yt-dlp` at a cookies file directly:

```bash
YTDLP_COOKIES_FILE=/absolute/path/to/cookies.txt
```

Set `SKIP_R2_UPLOAD=true` to skip Cloudflare R2 and only cut locally.

Set `SKIP_YOUTUBE_UPLOAD=true` to skip YouTube uploads (R2 only).

After R2 upload, clips are uploaded to YouTube one-by-one with titles like
`Home v Away | Tournament Name`. Default privacy is `private` (`YOUTUBE_PRIVACY`).
YouTube's API cannot set **members-only** visibility — after upload, set that in
YouTube Studio (Visibility → Members-only) for each clip.

To backfill clips that are already in R2:

```bash
python youtube_backfill.py
```

The organiser **Process this stream** button sets the session to `QUEUED`. This process polls `GET /api/v1/internal/vod-jobs`.
