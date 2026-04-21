"""Weekly wine YouTube monitor.

Pipeline:
  1. Read channels.yaml (50 channels)
  2. Discover videos uploaded in the last 7 days across all channels (via RSS)
  3. Rank by view count → pick top 10
  4. For each: download audio → transcribe with Whisper (--fast) → summarize with Claude
  5. Assemble a weekly markdown report at reports/YYYY-Www.md
  6. Send email with the GitHub link to the report
"""
import argparse
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import yaml

from discovery import discover_top_videos
from notifier import send_email

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("weekly_monitor")

FAST_PARAMS = dict(
    model_size="base",
    beam_size=1,
    condition_on_previous_text=False,
    vad_config={"min_silence_duration_ms": 500},
)
FAST_SEGMENT_LENGTH = 300


def load_channels(path: Path) -> List[Dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data["channels"]


def process_video(video: Dict, workdir: Path) -> Dict:
    """Download → transcribe → summarize a single video. Returns the video dict with summary fields added."""
    from ytt.core import (
        chunk_audio,
        cleanup_temp_files,
        download_youtube,
        summarize_with_claude,
        transcribe_audio,
    )

    video_dir = workdir / video["video_id"]
    video_dir.mkdir(parents=True, exist_ok=True)

    try:
        logger.info(f"[{video['channel_name']}] {video['title']}")
        info = download_youtube(video["url"], video_dir)
        chunks = chunk_audio(info["audio_path"], video_dir, segment_length=FAST_SEGMENT_LENGTH)
        transcripts = transcribe_audio(chunks, language=None, **FAST_PARAMS)
        summary = summarize_with_claude(transcripts, language="ko")
        video["short_summary"] = summary["short_summary"]
        video["long_summary"] = summary["long_summary"]
        video["status"] = "ok"
    except Exception as e:
        logger.exception(f"Processing failed for {video['url']}")
        video["short_summary"] = f"[처리 실패: {e}]"
        video["long_summary"] = ""
        video["status"] = "failed"
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


def build_report(entries: List[Dict], year: int, week: int) -> str:
    now = datetime.now(timezone.utc)
    lines = [
        f"# Wine YouTube Weekly — {year}-W{week:02d}",
        "",
        f"**Generated:** {now.strftime('%Y-%m-%d %H:%M UTC')}",
        f"**Videos analyzed:** {len(entries)}",
        "",
        "---",
        "",
    ]
    for i, e in enumerate(entries, 1):
        published = e["published"].strftime("%Y-%m-%d") if isinstance(e["published"], datetime) else str(e["published"])
        lines.extend([
            f"## {i}. [{e['channel_name']}] {e['title']}",
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
    parser = argparse.ArgumentParser(description="Weekly wine YouTube monitor")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days")
    parser.add_argument("--top", type=int, default=10, help="Number of top videos to process")
    parser.add_argument("--channels-limit", type=int, default=None, help="Limit channels for dry-run testing")
    parser.add_argument("--no-email", action="store_true", help="Skip email send (for local testing)")
    parser.add_argument("--no-process", action="store_true", help="Skip transcribe/summarize (discovery only)")
    parser.add_argument("--channels-file", default="channels.yaml")
    parser.add_argument("--reports-dir", default="reports")
    args = parser.parse_args(argv)

    channels = load_channels(Path(args.channels_file))
    if args.channels_limit:
        channels = channels[: args.channels_limit]
        logger.info(f"Dry-run: limited to {len(channels)} channels")

    top_videos = discover_top_videos(channels, days=args.days, top_n=args.top)
    logger.info(f"Top {len(top_videos)} videos selected")
    for v in top_videos:
        logger.info(f"  {v.get('view_count', 0):>10,}  {v['channel_name']}  |  {v['title']}")

    if not top_videos:
        logger.warning("No videos found — skipping report and email")
        return 0

    if not args.no_process:
        workdir = Path("/tmp/wine-weekly")
        workdir.mkdir(parents=True, exist_ok=True)
        top_videos = [process_video(v, workdir) for v in top_videos]

    now = datetime.now(timezone.utc)
    year, week, _ = now.isocalendar()
    report_name = f"{year}-W{week:02d}.md"
    reports_dir = Path(args.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / report_name
    report_path.write_text(build_report(top_videos, year, week), encoding="utf-8")
    logger.info(f"Report written: {report_path}")

    repo = os.environ.get("GITHUB_REPOSITORY", "SaraHan774/wine-weekly-monitor")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    report_url = f"https://github.com/{repo}/blob/{branch}/{report_path.as_posix()}"

    if args.no_email:
        logger.info(f"Skipping email. Report URL: {report_url}")
        return 0

    body_lines = [
        f"이번 주 와인 유튜브 요약 리포트가 준비됐어요.",
        "",
        f"  {report_url}",
        "",
        f"분석한 영상 {len(top_videos)}개:",
    ]
    for i, v in enumerate(top_videos, 1):
        body_lines.append(f"  {i}. [{v['channel_name']}] {v['title']} ({v.get('view_count', 0):,} views)")
    send_email(
        subject=f"Wine YouTube Weekly — {year}-W{week:02d}",
        body="\n".join(body_lines),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
