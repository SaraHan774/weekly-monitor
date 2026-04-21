"""Discovery: find recent uploads via YouTube RSS feeds, then fetch view counts via yt-dlp."""
import logging
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import yt_dlp

logger = logging.getLogger(__name__)

RSS_NAMESPACES = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


def fetch_recent_from_rss(channel_id: str, days: int = 7) -> List[Dict]:
    """Fetch uploads from a channel's public RSS feed, filtered to the last `days` days.

    YouTube RSS returns the 15 most recent uploads — enough for weekly monitoring.
    """
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError) as e:
        logger.warning(f"RSS fetch failed for {channel_id}: {e}")
        return []

    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        logger.warning(f"RSS parse failed for {channel_id}: {e}")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    results = []
    for entry in root.findall("atom:entry", RSS_NAMESPACES):
        pub_el = entry.find("atom:published", RSS_NAMESPACES)
        vid_el = entry.find("yt:videoId", RSS_NAMESPACES)
        title_el = entry.find("atom:title", RSS_NAMESPACES)
        link_el = entry.find("atom:link", RSS_NAMESPACES)
        if pub_el is None or vid_el is None or title_el is None or link_el is None:
            continue

        published = datetime.fromisoformat(pub_el.text.replace("Z", "+00:00"))
        if published < cutoff:
            continue

        results.append({
            "channel_id": channel_id,
            "video_id": vid_el.text,
            "title": title_el.text,
            "url": link_el.attrib["href"],
            "published": published,
        })
    return results


def fetch_view_count(video_url: str) -> Optional[Dict]:
    """Fetch view count + duration for a single video via yt-dlp (no download)."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except Exception as e:
        logger.warning(f"view_count fetch failed for {video_url}: {e}")
        return None
    return {
        "view_count": info.get("view_count", 0) or 0,
        "duration": info.get("duration", 0) or 0,
        "uploader": info.get("uploader", ""),
    }


SHORTS_MIN_DURATION_SEC = 60


def _is_short(video: Dict) -> bool:
    """YouTube Shorts are unsuitable for transcript summarization (too brief, often visual-only)."""
    if "/shorts/" in video.get("url", ""):
        return True
    duration = video.get("duration", 0)
    if duration and duration < SHORTS_MIN_DURATION_SEC:
        return True
    return False


def discover_top_videos(channels: List[Dict], days: int = 7, top_n: int = 10) -> List[Dict]:
    """Discover the top N most-viewed non-Shorts videos uploaded across all channels in the last `days` days."""
    candidates: List[Dict] = []
    logger.info(f"Scanning {len(channels)} channels for videos from the last {days} days")

    for ch in channels:
        recent = fetch_recent_from_rss(ch["channel_id"], days=days)
        kept = [v for v in recent if not _is_short(v)]
        for v in kept:
            v["channel_name"] = ch["name"]
            candidates.append(v)
        dropped = len(recent) - len(kept)
        msg = f"  {ch['name']}: {len(kept)} new"
        if dropped:
            msg += f" ({dropped} Shorts skipped)"
        logger.info(msg)

    logger.info(f"Total candidates: {len(candidates)} — fetching view counts")

    final: List[Dict] = []
    for v in candidates:
        meta = fetch_view_count(v["url"])
        if meta is None:
            v["view_count"] = 0
            v["duration"] = 0
        else:
            v.update(meta)
        # Duration-based Shorts filter — catches anything RSS didn't flag via URL
        if _is_short(v):
            continue
        final.append(v)

    final.sort(key=lambda x: x.get("view_count", 0), reverse=True)
    return final[:top_n]
