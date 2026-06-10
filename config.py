"""Configuration loader.

Layered defaults: hardcoded DEFAULTS → config.yaml (or path passed in) → CLI overrides.
load_config() returns a frozen dataclass-like dict; validate_runtime() fails fast at
startup if required env vars for the selected notification channel are missing.
"""
from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

import yaml

DEFAULTS: Dict[str, Any] = {
    "project": {
        "name": "YouTube Topic Weekly",
        "language": "ko",
        "topic_hint": "",
        "audience": "",
        "repo_fallback": "owner/repo",
    },
    "discovery": {
        "lookback_days": 7,
        "top_n": 10,
        "fallback_buffer": 5,
        "shorts_min_duration_sec": 60,
        "relevance_filter": False,
    },
    "processing": {
        "whisper_model": "base",
        "segment_length_sec": 300,
        "beam_size": 1,
        "retry_attempts": 2,
        "retry_backoff_sec": 2.0,
        "gemini_model": "gemini-3.5-flash",
        "gemini_rpm": 4,
    },
    "report": {
        "output_dir": "reports",
        "filename_pattern": "{year}-W{week:02d}.md",
        "title_pattern": "{project_name} — {year}-W{week:02d}",
    },
    "notification": {
        "channel": "gmail",
        "recipient": None,
        "subject_pattern": "{project_name} — {year}-W{week:02d}",
        "body_intro": "이번 주 요약 리포트가 준비됐어요.",
    },
}

VALID_NOTIFICATION_CHANNELS = {"gmail", "none"}


def _deep_merge(base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(base)
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path | str = "config.yaml") -> Dict[str, Any]:
    """Load config.yaml layered on top of DEFAULTS. Missing file → defaults only."""
    path = Path(path)
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    else:
        loaded = {}
    cfg = _deep_merge(DEFAULTS, loaded)
    _validate_static(cfg)
    return cfg


def _validate_static(cfg: Dict[str, Any]) -> None:
    """Structural checks that don't depend on runtime env."""
    channel = cfg["notification"]["channel"]
    if channel not in VALID_NOTIFICATION_CHANNELS:
        raise ValueError(
            f"notification.channel must be one of {sorted(VALID_NOTIFICATION_CHANNELS)}, "
            f"got {channel!r}"
        )
    if cfg["discovery"]["top_n"] < 1:
        raise ValueError("discovery.top_n must be >= 1")
    if cfg["discovery"]["lookback_days"] < 1:
        raise ValueError("discovery.lookback_days must be >= 1")


def validate_runtime(cfg: Dict[str, Any], *, will_send_email: bool, will_process: bool) -> None:
    """Verify env vars needed by the selected runtime path are present.

    Raises SystemExit with a clear message if anything is missing — fail fast at startup
    rather than crashing mid-pipeline (agent-friendly: error before side effects start).
    """
    missing: list[str] = []
    if will_process and not os.environ.get("GEMINI_API_KEY"):
        missing.append("GEMINI_API_KEY (required for summarization; pass --no-process to skip)")
    if will_send_email and cfg["notification"]["channel"] == "gmail":
        for var in ("GMAIL_USER", "GMAIL_APP_PASSWORD"):
            if not os.environ.get(var):
                missing.append(f"{var} (required for gmail notification; pass --no-email to skip)")
    if missing:
        lines = ["Missing required environment variables:"] + [f"  - {m}" for m in missing]
        raise SystemExit("\n".join(lines))
