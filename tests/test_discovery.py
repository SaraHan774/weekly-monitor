"""Tests for discovery.py — RSS XML parsing (fixture-based) and Shorts filter.

We do NOT test fetch_recent_from_rss() or fetch_view_count() directly because they
hit the network. The pure parsing function parse_rss() and the Shorts predicate
_is_short() cover the logic that can break on a code change.
"""
from datetime import datetime, timezone

from discovery import _cap_per_channel, _is_short, parse_rss

from .conftest import FIXTURES_DIR


RSS_XML = (FIXTURES_DIR / "rss_sample.xml").read_bytes()
# Cutoff well before all fixture entries' "recent" ones but after the "old" entry.
CUTOFF_AFTER_OLD = datetime(2026, 4, 1, tzinfo=timezone.utc)


def test_parse_rss_returns_only_entries_after_cutoff():
    entries = parse_rss(RSS_XML, "UCtest12345", CUTOFF_AFTER_OLD)
    ids = [e["video_id"] for e in entries]
    assert "vid_old" not in ids
    assert "vid_recent_a" in ids
    assert "vid_recent_b" in ids


def test_parse_rss_skips_entries_with_missing_required_fields():
    entries = parse_rss(RSS_XML, "UCtest12345", CUTOFF_AFTER_OLD)
    ids = [e["video_id"] for e in entries]
    assert "vid_no_title" not in ids


def test_parse_rss_extracts_expected_fields():
    entries = parse_rss(RSS_XML, "UCtest12345", CUTOFF_AFTER_OLD)
    a = next(e for e in entries if e["video_id"] == "vid_recent_a")
    assert a["channel_id"] == "UCtest12345"
    assert a["title"] == "Recent video A"
    assert a["url"] == "https://www.youtube.com/watch?v=vid_recent_a"
    assert a["published"] == datetime(2026, 4, 24, 10, 0, tzinfo=timezone.utc)


def test_parse_rss_returns_empty_for_malformed_xml():
    entries = parse_rss(b"<not xml", "UCtest12345", CUTOFF_AFTER_OLD)
    assert entries == []


def test_parse_rss_returns_empty_when_cutoff_excludes_everything():
    future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    entries = parse_rss(RSS_XML, "UCtest12345", future)
    assert entries == []


def test_is_short_flags_shorts_url():
    assert _is_short({"url": "https://youtube.com/shorts/abc", "duration": 0}, 60)


def test_is_short_flags_short_duration():
    assert _is_short({"url": "https://youtube.com/watch?v=abc", "duration": 30}, 60)


def test_is_short_passes_normal_video():
    assert not _is_short({"url": "https://youtube.com/watch?v=abc", "duration": 600}, 60)


def test_is_short_respects_threshold_argument():
    video = {"url": "https://youtube.com/watch?v=abc", "duration": 90}
    assert _is_short(video, 120)  # threshold 120 → 90s is a Short
    assert not _is_short(video, 60)  # threshold 60 → 90s passes


def test_is_short_treats_missing_duration_as_unknown_not_short():
    # If yt-dlp returned 0 duration (unknown), don't classify as Short by length
    assert not _is_short({"url": "https://youtube.com/watch?v=abc", "duration": 0}, 60)


def _vid(channel, vid):
    return {"channel_name": channel, "video_id": vid}


def test_cap_per_channel_limits_each_channel():
    videos = [
        _vid("A", "a1"), _vid("A", "a2"), _vid("A", "a3"),
        _vid("B", "b1"), _vid("B", "b2"),
        _vid("C", "c1"),
    ]
    kept = _cap_per_channel(videos, 2)
    ids = [v["video_id"] for v in kept]
    assert ids == ["a1", "a2", "b1", "b2", "c1"]  # A capped at 2, order preserved


def test_cap_per_channel_zero_disables_cap():
    videos = [_vid("A", "a1"), _vid("A", "a2"), _vid("A", "a3")]
    assert _cap_per_channel(videos, 0) == videos


def test_cap_per_channel_keeps_highest_viewed_first():
    # Input is pre-sorted by view_count desc; cap must keep the earliest (highest) entries
    videos = [_vid("A", "high"), _vid("A", "mid"), _vid("A", "low")]
    kept = _cap_per_channel(videos, 1)
    assert [v["video_id"] for v in kept] == ["high"]
