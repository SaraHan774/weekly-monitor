# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

Generic "YouTube topic monitor": scheduled GitHub Actions job (default Sunday 00:00 UTC) that scans channels listed in `channels.yaml`, picks the top-N most-viewed uploads in the lookback window, transcribes with Whisper, summarizes with Gemini, commits `reports/<filename_pattern>`, and emails the link. Default instance is wine — see `channels.yaml` and `config.yaml`. Switching topic is a `config.yaml` + `channels.yaml` change, no code edits.

**Primary consumer is another AI agent, not a human.** Treat the CLI surface (flags, stdout/stderr, exit codes, side effects) as a contract. After any change to `monitor.py` flags, public function signatures in `discovery.py`/`notifier.py`, output formats, config schema, or workflow invocation, run the `agent-friendly-check` skill (`.claude/skills/agent-friendly-check/SKILL.md`) to catch regressions in the agent contract.

## Local development

```bash
pip install -r requirements.txt   # also installs ytt from git
```

`ffmpeg` must be on PATH for audio chunking. Required env vars: `GEMINI_API_KEY` (only when processing — `--no-process` skips); `GMAIL_USER` + `GMAIL_APP_PASSWORD` (only when sending email and `notification.channel == "gmail"` — `--no-email` skips). Missing env vars are reported up front by `config.validate_runtime`, before any side effects start.

Common dry-run invocations:

```bash
# Discovery only — no audio download, no Gemini calls, no email
GEMINI_API_KEY=... python monitor.py --channels-limit 3 --no-process --no-email

# Full pipeline against 2 channels, 2 top videos, no email
GEMINI_API_KEY=... python monitor.py --channels-limit 2 --top 2 --no-email
```

Flags: `--config PATH` (alternate config), `--days N` (lookback window), `--top N` (videos to process), `--channels-limit N` (subset), `--no-process` (skip transcribe/summarize), `--no-email`, `--dry-run` (shorthand for `--no-process --no-email`), `--output {text,json}` (stdout format; logs go to stderr in both modes). All flag values override the corresponding `config.yaml` field for that run.

## Tests

```bash
pip install -r requirements-dev.txt   # adds pytest
pytest -v                             # ~1s, all hermetic (no network)
```

Test layout (`tests/`):
- `test_state.py` — dedupe / prune / save-load round-trip (uses `tmp_path`)
- `test_config.py` — DEFAULTS layering, validation rules, env-var checks (uses `monkeypatch`)
- `test_discovery.py` — RSS XML parsing via `tests/fixtures/rss_sample.xml` and `_is_short` predicate. **Network-touching** functions (`fetch_recent_from_rss`, `fetch_view_count`) are intentionally NOT tested — those are external surfaces.
- `test_monitor.py` — `with_retry`, `format_duration`, `build_report`
- `test_cli_contract.py` — **enforces the agent-friendly contract**: `--help` lists all flags; dry-run emits a `RESULT ` line; missing env vars abort before side effects with the missing var's name in stderr; `--no-email` skips Gmail validation. Runs `monitor.py` as subprocess against a hermetic empty-channel config in `tmp_path`. If you change CLI flags, output format, or env-var validation, this file is the canary.

There is no linter or type checker configured.

See "Tests" section below for pytest setup.

## Configuration

All behavior knobs (project name, language, top_n, lookback days, Whisper params, Shorts threshold, report/email templates, notification channel) live in `config.yaml`. `config.py:load_config` layers it on top of `DEFAULTS` and validates the structure; `config.example.yaml` documents every key. To run with a different topic/language, edit `config.yaml` only — code stays untouched. CLI flags (`--days`, `--top`, `--no-process`, `--no-email`, `--config`) override config values for ad-hoc runs.

## Architecture

Three-stage pipeline orchestrated by `monitor.py:main`:

1. **Discovery (`discovery.py`)** — for each channel, fetches the public YouTube RSS feed (`/feeds/videos.xml?channel_id=...`) which returns the 15 most recent uploads. Filters to the lookback window, then calls `yt-dlp` per video to fetch view count + duration. Shorts are filtered twice: by URL (`/shorts/`) and by duration (configurable threshold from `discovery.shorts_min_duration_sec`). Final list is sorted by `view_count` desc and truncated to `top_n`. **No YouTube Data API key is used or required** — RSS + yt-dlp are the entire data path. `monitor.py` requests `top_n + fallback_buffer` candidates and trims to `top_n` (the buffer is currently unused but reserved for Step 4 fallback work).

2. **Processing (`monitor.py:process_video`)** — delegates to the external `ytt` library (installed from `git+https://github.com/SaraHan774/ytt.git`). Calls `download_youtube → chunk_audio → transcribe_audio → summarize_with_claude` per video. Each of `download`, `transcribe`, `summarize` is wrapped in `with_retry` configured by `config.yaml:processing.retry_attempts` and `retry_backoff_sec`; the failed stage is recorded in `video["failed_stage"]` so the report distinguishes which step blew up. Whisper params and summary language come from `config.yaml:processing` and `config.yaml:project.language`. Each video runs in its own temp dir under `/tmp/youtube-monitor/<video_id>` and is cleaned up afterward.

3. **Report + notify** — writes `<report.output_dir>/<report.filename_pattern>` (default `reports/YYYY-Www.md`), then `notifier.py:send_email` posts to Gmail via SMTP `smtp.gmail.com:587` with STARTTLS. The email body contains the GitHub blob URL of the just-written report (built from `GITHUB_REPOSITORY` + `GITHUB_REF_NAME`, falling back to `config.yaml:project.repo_fallback`). Final stdout is one of two formats based on `--output`: `text` (default) prints `RESULT processed=N succeeded=N failed=N report=<path> url=<url>`; `json` prints a single JSON object (see `_emit_result` for the schema). Logs always go to stderr. Exit code is `0` on full success, `2` when any video failed, non-zero `SystemExit` for env-var/config problems.

**Cross-run dedupe (`state.py`)**: `<output_dir>/.processed.json` tracks `video_ids` (mapped to ISO week label) and `channel_last_seen`. Successful processes are recorded; failures are not (so they get retried next run). Entries older than 90 days are pruned on load. The CI workflow's `git add reports/` step picks up this file alongside reports.

**Fallback buffer**: `monitor.py` requests `top_n + fallback_buffer` candidates from `discover_top_videos`, then walks down the list calling `process_video` until `top_n` successes accumulate (or candidates run out). A failed top-N video does not shrink the report.

## Key external dependency: `ytt`

The `ytt` library does download, audio chunking, and Whisper transcription. It is **not in this repo** — it lives at https://github.com/SaraHan774/ytt and is pinned by `requirements.txt`. If the pipeline breaks at the download/transcribe stage, check that repo's API for `chunk_audio / cleanup_temp_files / download_youtube / transcribe_audio` (the four functions imported in `process_video`).

Summarization uses **Gemini**, not Claude — `gemini_summarize.py:summarize_with_gemini` (lives in this repo) returns `{short_summary, long_summary}` and reads `GEMINI_API_KEY` from env. We swapped off `ytt.summarize_with_claude` to use Google's free tier. **Quota design**: the observed free-tier limit for flash models is **5 RPM per model** (quotaId `GenerateRequestsPerMinutePerProjectPerModel-FreeTier` — seen in W23 report errors), so the full transcript is flattened into ONE JSON-mode call per video that returns both summaries — no chunk map-reduce (the 1M-token context makes it unnecessary). A module-level rate limiter paces every Gemini call (`processing.gemini_rpm`, default 4), and 429/RESOURCE_EXHAUSTED and 503/UNAVAILABLE responses are retried after the API-suggested `retryDelay` (fallback 30s, up to 4 attempts) inside `_generate` — `monitor.with_retry`'s 2s/4s backoff alone is too short for per-minute quota windows. The model is `processing.gemini_model` (default `gemini-3.5-flash`, free-tier eligible); `_thinking_config` maps Gemini 2.x to `thinking_budget=0` and Gemini 3+ to `thinking_level="low"` (3.x rejects budget=0). Prompts are grounded with a `[VIDEO CONTEXT]` line (title/channel/`project.topic_hint`) and a `[READER]` line (`project.audience` — reader profile/purpose); the prompt emits a `⚠️` warning instead of a summary when the transcript looks unrelated to the title (Whisper hallucination on music/silence). The `long_summary` structure is `#### 핵심 결론` → topical `####` sections → `#### 용어 설명` (terms unfamiliar at the reader's level; omitted when none) → `#### 시청자 인사이트` (takeaways for the reader's purpose). The `short_summary` must open with a hook (most surprising fact/claim/number), never a descriptive "this video is about" opener.

**Relevance filter** (`discovery.relevance_filter`, default false; wine instance enables it): when on and `project.topic_hint` is set, `gemini_summarize.py:check_relevance` classifies all candidate titles in one Gemini call after dedupe and drops off-topic videos before processing. Fails open (keeps everything) on API/parse errors; skipped under `--no-process`.

## Editing the channel list

`channels.yaml` is the source of truth — each entry needs `name` and `channel_id` (the `UC...` prefix from the channel URL). No code changes are needed when adding/removing channels.

## GitHub Actions

`.github/workflows/monitor.yml` runs on cron `0 0 * * 0` and on `workflow_dispatch` (manual). Manual dispatch exposes `channels_limit` and `skip_email` inputs for smoke testing. The workflow caches `~/.cache/huggingface` (key `whisper-base-v1`) so re-runs don't re-download the Whisper model. After the run, the job stages `reports/`, commits as `github-actions[bot]`, and pushes to the same branch — `permissions: contents: write` is required for that push, and `permissions: issues: write` lets the failure-notify step open a labeled issue (`monitor-failure`) when any step fails. Timeout is 330 minutes; concurrency group `monitor` prevents overlapping runs.

Three secrets must be set in repo settings: `GEMINI_API_KEY`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`.
