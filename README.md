# Wine Weekly Monitor

Automated weekly digest of the top wine YouTube videos.

## What it does

Every Sunday at 09:00 KST, the workflow:

1. Scans 50 curated wine YouTube channels for videos uploaded in the last 7 days
2. Ranks them by view count and picks the top 10
3. Downloads audio, transcribes it with Whisper, and summarizes with Claude
4. Commits a markdown report to `reports/YYYY-Www.md`
5. Emails the report link to the configured recipient

## Setup

### 1. Register three GitHub repository secrets

Settings → Secrets and variables → Actions → **New repository secret**:

| Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Claude API key |
| `GMAIL_USER` | Gmail address used to send the email |
| `GMAIL_APP_PASSWORD` | Gmail app password (16 chars, spaces removed is fine) |

### 2. (Optional) Edit the channel list

See [channels.yaml](channels.yaml). Each entry needs `name` and `channel_id` (the `UC...` identifier from the YouTube channel URL).

### 3. Trigger the first run

- Actions tab → **Weekly Wine Monitor** → **Run workflow**
- Or wait for the next Sunday 00:00 UTC

For a smoke test, set `channels_limit` to 3 and `skip_email` to true on manual dispatch.

## Local testing

```bash
pip install -r requirements.txt

# Discovery only (no download/transcribe/email) against 3 channels
ANTHROPIC_API_KEY=... python weekly_monitor.py \
    --channels-limit 3 --no-process --no-email

# Full end-to-end against 2 channels, skipping email
ANTHROPIC_API_KEY=... python weekly_monitor.py \
    --channels-limit 2 --top 2 --no-email
```

`ffmpeg` must be on PATH for audio chunking.

## Architecture

- [weekly_monitor.py](weekly_monitor.py) — orchestrator
- [discovery.py](discovery.py) — YouTube RSS + yt-dlp view-count fetcher (no YouTube Data API required)
- [notifier.py](notifier.py) — Gmail SMTP sender
- [ytt](https://github.com/SaraHan774/ytt) — transcription + Claude summarization library, installed from git

## Schedule

Workflow runs every Sunday at `00:00 UTC` (09:00 KST). Change the cron in [.github/workflows/weekly.yml](.github/workflows/weekly.yml) if you want a different time.
