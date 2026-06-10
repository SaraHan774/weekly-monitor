# YouTube Topic Monitor

Weekly automated digest of the top YouTube videos for any topic. This repo runs a scheduled GitHub Actions workflow that scans a curated channel list, picks the most-viewed recent uploads, transcribes them with Whisper, summarizes them with Gemini, commits a markdown report, and emails the link.

The default instance is wine — see `channels.yaml` and `config.yaml`. Fork and edit those two files to switch topic.

## What it does (default schedule: Sunday 09:00 KST)

1. Scans the YouTube channels listed in `channels.yaml` for videos uploaded in the last `discovery.lookback_days` days
2. Ranks them by view count and picks the top `discovery.top_n`
3. Downloads audio, transcribes it with Whisper, and summarizes with Gemini
4. Commits a markdown report to `reports/YYYY-Www.md` (configurable)
5. Emails the report link

## Setup

### 1. Register three GitHub repository secrets

Settings → Secrets and variables → Actions → **New repository secret**:

| Name | Value |
|---|---|
| `GEMINI_API_KEY` | Your Google AI Studio API key (free tier — https://aistudio.google.com/app/apikey) |
| `GMAIL_USER` | Gmail address used to send the email |
| `GMAIL_APP_PASSWORD` | Gmail app password (16 chars, spaces removed is fine) |

### 2. Edit `config.yaml`

All behavior knobs (project name, summary language, top_n, lookback days, Whisper model, report template, email subject) live in `config.yaml`. See `config.example.yaml` for the full schema with comments. Code stays untouched when you change topic.

### 3. Edit the channel list

See `channels.yaml`. Each entry needs `name` and `channel_id` (the `UC...` identifier from the YouTube channel URL).

### 4. Trigger the first run

- Actions tab → **YouTube Topic Monitor** → **Run workflow**
- Or wait for the next scheduled run

For a smoke test, set `channels_limit` to 3 and `skip_email` to true on manual dispatch.

## Local testing

```bash
pip install -r requirements.txt

# Discovery only (no download/transcribe/email) against 3 channels
GEMINI_API_KEY=... python monitor.py \
    --channels-limit 3 --no-process --no-email

# Full end-to-end against 2 channels, skipping email
GEMINI_API_KEY=... python monitor.py \
    --channels-limit 2 --top 2 --no-email
```

`ffmpeg` must be on PATH for audio chunking. Missing env vars are reported up front before any side effects start.

## For AI agents

This monitor is designed to be invoked by other AI agents as well as humans. Contract:

- **Entry point**: `python monitor.py [flags]`
- **Help**: `python monitor.py --help` — non-interactive, lists all flags
- **Dry run**: `--dry-run` (or the equivalent `--no-process --no-email`) skips all expensive side effects (no Gemini calls, no email, no audio download, no state writes). The discovery + ranking still runs and prints the selected videos. No env vars are required for dry-run.
- **Exit codes**: `0` on success or graceful skip (no new videos); `2` if at least one video failed to process; non-zero `SystemExit` on missing env vars, invalid config, or unhandled error
- **Output mode** (`--output`):
  - `text` (default): final stdout line `RESULT processed=<N> succeeded=<N> failed=<N> report=<path|none> url=<github-url|none>`
  - `json`: stdout is a single JSON object, logs stay on stderr. Schema:
    ```json
    {
      "processed": 0, "succeeded": 0, "failed": 0,
      "report_path": "reports/2026-W17.md" | null,
      "report_url": "https://github.com/..." | null,
      "videos": [
        {
          "video_id": "...", "channel_id": "...", "channel_name": "...",
          "title": "...", "url": "...", "view_count": 0, "duration": 0,
          "status": "ok" | "failed" | null,
          "failed_stage": "download" | "chunk" | "transcribe" | "summarize" | null,
          "short_summary": "..." | null
        }
      ]
    }
    ```
- **Side effects** when not in dry run:
  - Writes `<config.report.output_dir>/<config.report.filename_pattern>` (default `reports/YYYY-Www.md`)
  - Writes `<output_dir>/.processed.json` (cross-run dedupe state, auto-pruned after 90 days)
  - Calls the Gemini API **once per processed video** (single JSON-mode call returns both summaries, model = `processing.gemini_model`). All Gemini calls are globally paced to `processing.gemini_rpm` (default 4, under the observed 5 RPM per-model free-tier limit) and 429/503s wait for the API-suggested retry delay
  - When `discovery.relevance_filter: true` and `project.topic_hint` is set, makes one extra batch Gemini call to classify candidate titles and drops off-topic videos before processing (fails open on API errors; skipped with `--no-process`)
  - Calls Gmail SMTP if notification channel is gmail and `--no-email` is not set
  - Writes to `/tmp/youtube-monitor/<video_id>` during processing (cleaned up per video)
- **Configuration**: read `config.yaml`. Override individual fields with `--days`, `--top`, `--no-process`, `--no-email`, `--channels-limit`. Pass an alternate config with `--config path/to/other.yaml`.
- **Per-video failure** is captured in the report (`status: failed`, `failed_stage`) rather than aborting the run. Each stage gets `processing.retry_attempts - 1` automatic retries on transient failure.
- **Cross-run dedupe**: a video processed successfully in week N is skipped when it reappears in week N+1's RSS feed.

## Architecture

- `monitor.py` — orchestrator
- `config.py` / `config.yaml` — behavior knobs and runtime validation
- `discovery.py` — YouTube RSS + yt-dlp view-count fetcher (no YouTube Data API required)
- `notifier.py` — Gmail SMTP sender
- `gemini_summarize.py` — Gemini-backed summarizer (returns `{short_summary, long_summary}`)
- [ytt](https://github.com/SaraHan774/ytt) — download + audio chunking + Whisper transcription library, installed from git (its built-in Claude summarizer is unused; we call our own Gemini one)

## Schedule

Workflow runs every Sunday at `00:00 UTC` (09:00 KST). Change the cron in `.github/workflows/monitor.yml` if you want a different time. Workflow failures (e.g. dependency install fails) auto-create a labeled GitHub issue.
