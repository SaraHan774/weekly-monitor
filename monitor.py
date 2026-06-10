"""YouTube topic monitor — entry point.

Pipeline:
  1. Read channels.yaml + config.yaml
  2. Discover videos uploaded in the last `discovery.lookback_days` across all channels (via RSS)
  3. Rank by view count → pick top `discovery.top_n`
  4. For each: download audio → transcribe with Whisper → summarize with Gemini
  5. Assemble a markdown report at <output_dir>/<filename_pattern>
  6. Send notification with the report link (channel = config.notification.channel)

Behavior knobs live in config.yaml — see config.example.yaml for the full schema.
CLI flags override the corresponding config values (useful for dry-run testing).
"""
import argparse
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List

import yaml

from config import load_config, validate_runtime
from discovery import discover_top_videos
from notifier import send_email
from state import filter_new, load_state, mark_processed, save_state, update_channel_seen

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,  # keep stdout clean for --output json consumers
)
logger = logging.getLogger("monitor")


def load_channels(path: Path) -> List[Dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data["channels"]


def _emit_result(
    fmt: str,
    *,
    processed: int,
    succeeded: int,
    failed: int,
    report_path: str | None,
    report_url: str | None,
    videos: List[Dict],
) -> None:
    """Write the final result to stdout. `text` = single RESULT line; `json` = JSON object.

    JSON consumers (typically AI agents) get only the result on stdout — logs go to stderr.
    """
    if fmt == "json":
        payload = {
            "processed": processed,
            "succeeded": succeeded,
            "failed": failed,
            "report_path": report_path,
            "report_url": report_url,
            "videos": [
                {
                    "video_id": v.get("video_id"),
                    "channel_id": v.get("channel_id"),
                    "channel_name": v.get("channel_name"),
                    "title": v.get("title"),
                    "url": v.get("url"),
                    "view_count": v.get("view_count", 0),
                    "duration": v.get("duration", 0),
                    "status": v.get("status"),
                    "failed_stage": v.get("failed_stage"),
                    "short_summary": v.get("short_summary"),
                }
                for v in videos
            ],
        }
        print(json.dumps(payload, ensure_ascii=False))
    else:
        path_str = report_path if report_path else "none"
        url_str = report_url if report_url else "none"
        print(
            f"RESULT processed={processed} succeeded={succeeded} failed={failed} "
            f"report={path_str} url={url_str}"
        )


def with_retry(fn: Callable, *args, attempts: int = 2, backoff_sec: float = 2.0, label: str = "") -> Any:
    """Call fn(*args) up to `attempts` times with exponential backoff. Re-raises last exception.

    `attempts=2` means 1 retry after the first failure (total 2 calls). Used to absorb
    transient network blips in download/transcribe/summarize without masking real bugs.
    """
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return fn(*args)
        except Exception as e:
            last_exc = e
            if i + 1 < attempts:
                wait = backoff_sec * (2 ** i)
                logger.warning(f"{label} failed (attempt {i+1}/{attempts}): {e} — retrying in {wait:.1f}s")
                time.sleep(wait)
    assert last_exc is not None
    raise last_exc


def process_video(video: Dict, workdir: Path, cfg: Dict[str, Any]) -> Dict:
    """Download → transcribe → summarize a single video. Returns the video dict with summary fields added.

    Each stage gets one retry on transient failure. Per-stage failure is recorded in
    `failed_stage` so the report distinguishes download vs transcribe vs summarize errors.
    """
    from ytt.core import (
        chunk_audio,
        cleanup_temp_files,
        download_youtube,
        transcribe_audio,
    )

    from gemini_summarize import summarize_with_gemini

    proc = cfg["processing"]
    whisper_kwargs = dict(
        model_size=proc["whisper_model"],
        beam_size=proc["beam_size"],
        condition_on_previous_text=False,
        vad_config={"min_silence_duration_ms": 500},
    )
    retry_kwargs = dict(
        attempts=proc["retry_attempts"],
        backoff_sec=proc["retry_backoff_sec"],
    )

    video_dir = workdir / video["video_id"]
    video_dir.mkdir(parents=True, exist_ok=True)
    label = f"[{video['channel_name']}] {video['title']}"

    stage = "download"
    try:
        logger.info(label)
        info = with_retry(download_youtube, video["url"], video_dir,
                          label=f"download {video['video_id']}", **retry_kwargs)

        stage = "chunk"
        chunks = chunk_audio(info["audio_path"], video_dir, segment_length=proc["segment_length_sec"])

        stage = "transcribe"
        transcripts = with_retry(
            lambda: transcribe_audio(chunks, language=None, **whisper_kwargs),
            label=f"transcribe {video['video_id']}", **retry_kwargs,
        )

        stage = "summarize"
        summary = with_retry(
            lambda: summarize_with_gemini(
                transcripts,
                language=cfg["project"]["language"],
                video_title=video["title"],
                channel_name=video["channel_name"],
                topic_hint=cfg["project"]["topic_hint"],
                audience=cfg["project"]["audience"],
                model=proc["gemini_model"],
                rpm=proc["gemini_rpm"],
            ),
            label=f"summarize {video['video_id']}", **retry_kwargs,
        )

        video["short_summary"] = summary["short_summary"]
        video["long_summary"] = summary["long_summary"]
        video["status"] = "ok"
        video["failed_stage"] = None
    except Exception as e:
        logger.exception(f"Processing failed at {stage} for {video['url']}")
        video["short_summary"] = f"[처리 실패 ({stage}): {e}]"
        video["long_summary"] = ""
        video["status"] = "failed"
        video["failed_stage"] = stage
    finally:
        cleanup_temp_files(video_dir)
        shutil.rmtree(video_dir, ignore_errors=True)

    return video


def format_duration(seconds: float) -> str:
    if not seconds:
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s"


def build_report(entries: List[Dict], year: int, week: int, cfg: Dict[str, Any]) -> str:
    now = datetime.now(timezone.utc)
    title = cfg["report"]["title_pattern"].format(
        project_name=cfg["project"]["name"], year=year, week=week
    )
    lines = [
        f"# {title}",
        "",
        f"**Generated:** {now.strftime('%Y-%m-%d %H:%M UTC')}",
        f"**Videos analyzed:** {len(entries)}",
        "",
        "---",
        "",
    ]
    for i, e in enumerate(entries, 1):
        published = e["published"].strftime("%Y-%m-%d") if isinstance(e["published"], datetime) else str(e["published"])
        status_marker = ""
        if e.get("status") == "failed":
            status_marker = f" — ❌ failed at {e.get('failed_stage', 'unknown')}"
        lines.extend([
            f"## {i}. [{e['channel_name']}] {e['title']}{status_marker}",
            "",
            f"- Link: {e['url']}",
            f"- Views: {e.get('view_count', 0):,}",
            f"- Published: {published}",
            f"- Duration: {format_duration(e.get('duration', 0))}",
            "",
            "### TL;DR",
            "",
            e.get("short_summary", ""),
            "",
            "### 상세 요약",
            "",
            e.get("long_summary", ""),
            "",
            "---",
            "",
        ])
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="YouTube topic monitor")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--days", type=int, default=None, help="Override discovery.lookback_days")
    parser.add_argument("--top", type=int, default=None, help="Override discovery.top_n")
    parser.add_argument("--channels-limit", type=int, default=None, help="Limit channels for dry-run testing")
    parser.add_argument("--no-email", action="store_true", help="Skip notification send")
    parser.add_argument("--no-process", action="store_true", help="Skip transcribe/summarize (discovery only)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Shorthand for --no-process --no-email (no side effects on external services)")
    parser.add_argument("--channels-file", default="channels.yaml")
    parser.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
        help="Result format on stdout. text=RESULT line; json=single JSON object (logs stay on stderr).",
    )
    args = parser.parse_args(argv)
    if args.dry_run:
        args.no_process = True
        args.no_email = True

    cfg = load_config(args.config)
    validate_runtime(cfg, will_send_email=not args.no_email, will_process=not args.no_process)

    days = args.days if args.days is not None else cfg["discovery"]["lookback_days"]
    top_n = args.top if args.top is not None else cfg["discovery"]["top_n"]
    fallback_buffer = cfg["discovery"]["fallback_buffer"]
    shorts_min = cfg["discovery"]["shorts_min_duration_sec"]

    channels = load_channels(Path(args.channels_file))
    if args.channels_limit:
        channels = channels[: args.channels_limit]
        logger.info(f"Dry-run: limited to {len(channels)} channels")

    reports_dir = Path(cfg["report"]["output_dir"])
    state = load_state(reports_dir)

    candidates = discover_top_videos(
        channels,
        days=days,
        top_n=top_n + fallback_buffer,
        shorts_min_duration_sec=shorts_min,
    )
    new, seen = filter_new(candidates, state)
    if seen:
        logger.info(f"Skipping {len(seen)} already-processed video(s): " +
                    ", ".join(v["video_id"] for v in seen))
    candidates = new

    if (
        candidates
        and not args.no_process
        and cfg["discovery"]["relevance_filter"]
        and cfg["project"]["topic_hint"]
    ):
        from gemini_summarize import check_relevance

        relevant = check_relevance(
            candidates, cfg["project"]["topic_hint"],
            model=cfg["processing"]["gemini_model"],
            rpm=cfg["processing"]["gemini_rpm"],
        )
        dropped = [v for v, ok in zip(candidates, relevant) if not ok]
        if dropped:
            logger.info(
                f"Relevance filter dropped {len(dropped)} off-topic video(s): " +
                ", ".join(f"[{v['channel_name']}] {v['title']}" for v in dropped)
            )
        candidates = [v for v, ok in zip(candidates, relevant) if ok]

    if not candidates:
        logger.warning("No new videos found — skipping report and email")
        _emit_result(args.output, processed=0, succeeded=0, failed=0,
                     report_path=None, report_url=None, videos=[])
        return 0

    now = datetime.now(timezone.utc)
    year, week, _ = now.isocalendar()

    if args.no_process:
        # Dry-run: just take the first top_n new candidates without processing
        top_videos = candidates[:top_n]
        logger.info(f"Top {len(top_videos)} videos selected (dry-run, no processing)")
        for v in top_videos:
            logger.info(f"  {v.get('view_count', 0):>10,}  {v['channel_name']}  |  {v['title']}")
    else:
        # Process candidates one at a time, walking down the list to fill top_n successes
        workdir = Path("/tmp/youtube-monitor")
        workdir.mkdir(parents=True, exist_ok=True)
        top_videos = []
        ok_count = 0
        for v in candidates:
            if ok_count >= top_n:
                break
            logger.info(f"Processing candidate {len(top_videos)+1}/{len(candidates)} (filled {ok_count}/{top_n})")
            processed = process_video(v, workdir, cfg)
            top_videos.append(processed)
            if processed["status"] == "ok":
                ok_count += 1
                mark_processed(state, processed["video_id"], year, week)
                update_channel_seen(state, processed["channel_id"])
        save_state(reports_dir, state)
        logger.info(f"Processed {len(top_videos)} candidates, {ok_count} succeeded")

    iso_date = now.strftime("%Y-%m-%d")
    report_name = cfg["report"]["filename_pattern"].format(year=year, week=week, date=iso_date)
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / report_name
    report_path.write_text(build_report(top_videos, year, week, cfg), encoding="utf-8")
    logger.info(f"Report written: {report_path}")

    repo = os.environ.get("GITHUB_REPOSITORY", cfg["project"]["repo_fallback"])
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    report_url = f"https://github.com/{repo}/blob/{branch}/{report_path.as_posix()}"

    failed = sum(1 for v in top_videos if v.get("status") == "failed")
    succeeded = len(top_videos) - failed

    if args.no_email or cfg["notification"]["channel"] == "none":
        logger.info(f"Skipping notification. Report URL: {report_url}")
    else:
        subject = cfg["notification"]["subject_pattern"].format(
            project_name=cfg["project"]["name"], year=year, week=week
        )
        body_lines = [
            cfg["notification"]["body_intro"],
            "",
            f"  {report_url}",
            "",
            f"분석한 영상 {len(top_videos)}개:",
        ]
        for i, v in enumerate(top_videos, 1):
            body_lines.append(f"  {i}. [{v['channel_name']}] {v['title']} ({v.get('view_count', 0):,} views)")
        send_email(
            subject=subject,
            body="\n".join(body_lines),
            to_addr=cfg["notification"]["recipient"],
        )

    _emit_result(
        args.output,
        processed=len(top_videos),
        succeeded=succeeded,
        failed=failed,
        report_path=report_path.as_posix(),
        report_url=report_url,
        videos=top_videos,
    )
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
