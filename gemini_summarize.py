"""Gemini-backed replacement for ytt.core.summarize_with_claude.

Same return shape (`{short_summary, long_summary}`) so monitor.py's process_video
can swap implementations without touching anything else.

Quota design: gemini-2.5-flash free tier allows ~10 RPM / 250 RPD. The whole
transcript fits in the 1M-token context window, so each video costs exactly ONE
API call (JSON output carries both summaries). A module-level rate limiter paces
all calls (default 8 RPM), and 429s are retried after the API-suggested delay.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Dict, List, Optional

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.5-flash"
# Observed free-tier limit for flash models is 5 requests/min *per model*
# (quotaId GenerateRequestsPerMinutePerProjectPerModel-FreeTier) — stay under it.
DEFAULT_RPM = 4

_WARNING_LINE = {
    "ko": "⚠️ 전사 결과가 영상 제목과 무관한 내용입니다 (음악/무음 구간의 전사 오류 가능성). 요약을 생략합니다.",
    "en": "⚠️ Transcript appears unrelated to the video title (possible transcription error on music/silence). Summary skipped.",
    "ja": "⚠️ 文字起こし結果が動画タイトルと無関係です（音楽/無音区間の誤認識の可能性）。要約を省略します。",
}

PROMPTS: Dict[str, str] = {
    "ko": (
        "입력 상단에 [VIDEO CONTEXT](제목·채널·주제)와 [READER](독자 프로필·활용 목적) 라인이 주어지고, "
        "그 아래에 YouTube 영상 전체 transcript가 온다. 잡담·추임새를 제거하고 JSON으로 요약하라.\n"
        "- short_summary: 한국어 2~3문장의 단일 단락. 첫 문장은 이 영상을 전혀 모르는 독자가 "
        "더 읽고 싶어지게 만드는 후킹이어야 한다 — 영상에서 가장 흥미롭거나 의외인 사실·주장·숫자를 "
        "앞세워라. '이 영상은 ~', '~가 ~를 소개합니다' 같은 설명조 도입과 'TL;DR' 등 메타 머리말 금지. "
        "뒤 문장에서 누가·어떤 맥락인지 보충하라.\n"
        "- long_summary: 한국어 마크다운, 다음 구조를 따르라.\n"
        "  1. '#### 핵심 결론' 헤딩 + 2~3문장 단락.\n"
        "  2. 주제별 '#### <소제목>' 섹션 2~4개, 각 섹션에 bullet 3~6개. "
        "숫자·고유명사·점수·가격은 보존하고 중복은 한 번만. 시간순 중계가 아니라 주제별 정리.\n"
        "  3. '#### 용어 설명' 헤딩: 영상에 등장한 용어(품종·산지·양조 기법 등) 중 [READER] 수준에서 "
        "낯설 만한 것 3~6개를 '**용어**: 한 줄 설명' bullet로. 그런 용어가 없으면 이 섹션은 생략.\n"
        "  4. '#### 시청자 인사이트' 헤딩: [READER]의 활용 목적에 비춰 이 영상에서 얻어갈 "
        "실용적 포인트 2~4개 bullet (예: 구매·시음에 적용할 팁, 기억할 학습 포인트).\n"
        f"중요: transcript가 영상 제목과 명백히 무관한 주제라면(전사 오류 가능성) 두 필드 모두 "
        f"'{_WARNING_LINE['ko']}' 한 줄로 채워라."
    ),
    "en": (
        "The input starts with [VIDEO CONTEXT] (title/channel/topic) and [READER] (reader profile "
        "and purpose) lines, followed by the full YouTube transcript. Drop filler and summarize as JSON.\n"
        "- short_summary: a single 2-3 sentence paragraph. The FIRST sentence must hook a reader "
        "who knows nothing about the video — lead with the most interesting or surprising fact, "
        "claim, or number from it. No descriptive openers like 'This video is about' or "
        "'X introduces Y', no 'TL;DR:' preamble. Later sentences add who/what context.\n"
        "- long_summary: markdown with this structure:\n"
        "  1. '#### Key takeaways' heading + a 2-3 sentence paragraph.\n"
        "  2. 2-4 topical '#### <subheading>' sections with 3-6 bullets each. Preserve numbers, "
        "names, scores, prices; deduplicate; organize by topic, not chronology.\n"
        "  3. '#### Glossary' heading: 3-6 terms from the video (varieties, regions, techniques) "
        "likely unfamiliar at the [READER] level, as '**term**: one-line explanation' bullets. "
        "Omit the section if there are none.\n"
        "  4. '#### Viewer insights' heading: 2-4 bullets on what the reader gains given their "
        "[READER] purpose (e.g. buying/tasting tips, learning points).\n"
        f"IMPORTANT: if the transcript is clearly unrelated to the video title (likely "
        f"transcription error), fill BOTH fields with exactly: '{_WARNING_LINE['en']}'"
    ),
    "ja": (
        "入力の上部に[VIDEO CONTEXT]（タイトル・チャンネル・主題）と[READER]（読者プロフィール・目的）の行、"
        "その下にYouTube動画の全文文字起こしが与えられる。雑談を除きJSONで要約せよ。\n"
        "- short_summary: 日本語2〜3文の単一段落。最初の文は動画を全く知らない読者を引き込むフックにせよ — "
        "最も興味深い・意外な事実・主張・数字を先頭に。「この動画は〜」「〜が〜を紹介します」のような"
        "説明調の導入と「TL;DR」等の前置き禁止。\n"
        "- long_summary: 日本語マークダウン、次の構造に従え。\n"
        "  1. '#### 要点'見出し + 2〜3文の段落。\n"
        "  2. トピック別'#### <小見出し>'セクション2〜4個、各bullet 3〜6個。数字・固有名詞は保存、重複除去。\n"
        "  3. '#### 用語解説'見出し: [READER]水準で馴染みの薄い用語3〜6個を'**用語**: 一行説明'のbulletで。なければ省略。\n"
        "  4. '#### 視聴者インサイト'見出し: [READER]の目的に照らした実用ポイント2〜4個。\n"
        f"重要：文字起こしが動画タイトルと明らかに無関係なら（誤認識の可能性）両フィールドを"
        f"'{_WARNING_LINE['ja']}'で埋めよ。"
    ),
}

_SUMMARY_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "short_summary": types.Schema(type=types.Type.STRING),
        "long_summary": types.Schema(type=types.Type.STRING),
    },
    required=["short_summary", "long_summary"],
)

def _thinking_config(model: str) -> types.ThinkingConfig:
    """Gemini 2.x disables thinking via thinking_budget=0 (it eats max_output_tokens
    and truncates summaries otherwise); Gemini 3+ replaced the integer budget with a
    thinking_level enum and rejects budget=0."""
    if model.startswith("gemini-2"):
        return types.ThinkingConfig(thinking_budget=0)
    return types.ThinkingConfig(thinking_level="low")

_rate_lock = threading.Lock()
_next_call_at = 0.0

_RETRY_DELAY_RE = re.compile(r"retry[_ ]?delay['\"]?\s*[:=]\s*['\"]?(\d+)", re.IGNORECASE)


def _pace(rpm: int) -> None:
    """Block until the next API call slot. Shared across threads and call sites."""
    global _next_call_at
    interval = 60.0 / max(1, rpm)
    with _rate_lock:
        now = time.monotonic()
        wait = _next_call_at - now
        _next_call_at = max(now, _next_call_at) + interval
    if wait > 0:
        logger.debug("Rate limiter: sleeping %.1fs", wait)
        time.sleep(wait)


def _generate(client, model: str, contents: str, config, rpm: int, attempts: int = 4):
    """Paced generate_content with quota-aware retry.

    On 429/RESOURCE_EXHAUSTED, sleeps for the API-suggested retryDelay (fallback
    30s) before retrying — the short generic backoff in monitor.with_retry is not
    long enough for per-minute quota windows.
    """
    for attempt in range(attempts):
        _pace(rpm)
        try:
            return client.models.generate_content(
                model=model, contents=contents, config=config
            )
        except Exception as exc:
            code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            is_quota = code == 429 or "RESOURCE_EXHAUSTED" in str(exc)
            is_overloaded = code == 503 or "UNAVAILABLE" in str(exc)
            if not (is_quota or is_overloaded) or attempt + 1 >= attempts:
                raise
            match = _RETRY_DELAY_RE.search(str(exc))
            delay = int(match.group(1)) if match else 30
            logger.warning(
                "Gemini %s (attempt %d/%d) — sleeping %ds before retry",
                "quota hit" if is_quota else "overloaded",
                attempt + 1, attempts, delay,
            )
            time.sleep(delay + 1)
    raise RuntimeError("unreachable")


def _build_context_line(video_title: str, channel_name: str, topic_hint: str) -> str:
    parts = []
    if video_title:
        parts.append(f"title={video_title!r}")
    if channel_name:
        parts.append(f"channel={channel_name!r}")
    if topic_hint:
        parts.append(f"topic={topic_hint!r}")
    return "[VIDEO CONTEXT] " + " ".join(parts) if parts else ""


def summarize_with_gemini(
    transcripts: List[Dict],
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    language: str = "ko",
    video_title: str = "",
    channel_name: str = "",
    topic_hint: str = "",
    audience: str = "",
    rpm: int = DEFAULT_RPM,
) -> Dict[str, str]:
    """Summarize a full video transcript in a single Gemini call.

    `transcripts` matches ytt's shape: each entry has a `segments` list whose items
    have a `text` field. All segments are flattened into one transcript (it fits the
    1M-token context easily) and one JSON-mode call returns both summaries —
    keeping quota usage at 1 request per video.

    `video_title`/`channel_name`/`topic_hint` ground the prompt; the model flags
    transcripts that look unrelated to the title (Whisper hallucination on
    music/silence) with a `⚠️` warning instead of a fabricated summary.

    Returns: `{"short_summary": str, "long_summary": str}` — same as ytt's helper.
    """
    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY not set; pass --no-process to skip summarization"
        )

    if language not in PROMPTS:
        logger.warning("Unsupported language %r; defaulting to ko", language)
        language = "ko"

    full_text = " ".join(
        seg["text"] for chunk in transcripts for seg in chunk["segments"]
    ).strip()
    context_line = _build_context_line(video_title, channel_name, topic_hint)
    if audience:
        context_line = f"{context_line}\n[READER] {audience}".strip()
    contents = f"{context_line}\n\n{full_text}" if context_line else full_text

    logger.info(
        "Gemini summary: %d transcript chars, model=%s, language=%s (1 API call)",
        len(full_text), model, language,
    )

    client = genai.Client(api_key=api_key)
    response = _generate(
        client,
        model,
        contents,
        types.GenerateContentConfig(
            system_instruction=PROMPTS[language],
            temperature=0.3,
            max_output_tokens=8192,
            thinking_config=_thinking_config(model),
            response_mime_type="application/json",
            response_schema=_SUMMARY_SCHEMA,
        ),
        rpm=rpm,
    )

    raw = (response.text or "").strip()
    try:
        parsed = json.loads(raw)
        short_summary = (parsed.get("short_summary") or "").strip()
        long_summary = (parsed.get("long_summary") or "").strip()
    except (json.JSONDecodeError, AttributeError):
        logger.warning("Summary response was not valid JSON; using raw text")
        short_summary, long_summary = "", raw

    if not long_summary:
        long_summary = raw or "[요약 비어있음]"
    if not short_summary:
        short_summary = long_summary.split("\n", 1)[0]

    return {"long_summary": long_summary, "short_summary": short_summary}


def check_relevance(
    videos: List[Dict],
    topic_hint: str,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    rpm: int = DEFAULT_RPM,
) -> List[bool]:
    """Classify each video title as on-topic or not, in a single Gemini call.

    Returns a list of booleans aligned with `videos`. Fails open: on any API or
    parse error, every video is treated as relevant (a noisy report beats an
    empty one).
    """
    if not videos or not topic_hint:
        return [True] * len(videos)

    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return [True] * len(videos)

    numbered = "\n".join(
        f"{i}. [{v.get('channel_name', '')}] {v.get('title', '')}"
        for i, v in enumerate(videos, 1)
    )
    prompt = (
        f"Topic: {topic_hint}\n\n"
        "For each numbered YouTube video title below, decide if the video is about the topic "
        "(directly or strongly related). Be lenient with adjacent subjects only if the title "
        "clearly connects them to the topic.\n"
        "Output EXACTLY one line per video in the form 'N: yes' or 'N: no'. Nothing else.\n\n"
        f"{numbered}"
    )

    try:
        client = genai.Client(api_key=api_key)
        response = _generate(
            client,
            model,
            prompt,
            types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=1024,
                thinking_config=_thinking_config(model),
            ),
            rpm=rpm,
        )
        text = (response.text or "").strip()
        verdicts: Dict[int, bool] = {}
        for line in text.splitlines():
            line = line.strip()
            if ":" not in line:
                continue
            num_part, _, answer = line.partition(":")
            try:
                idx = int(num_part.strip().lstrip("#").rstrip("."))
            except ValueError:
                continue
            verdicts[idx] = answer.strip().lower().startswith("y")
        if not verdicts:
            raise ValueError(f"unparseable relevance response: {text[:200]!r}")
        return [verdicts.get(i, True) for i in range(1, len(videos) + 1)]
    except Exception as exc:
        logger.error("Relevance check failed; keeping all videos: %s", exc)
        return [True] * len(videos)
