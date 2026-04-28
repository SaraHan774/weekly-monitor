"""Gemini-backed replacement for ytt.core.summarize_with_claude.

Same return shape (`{short_summary, long_summary}`) so monitor.py's process_video
can swap implementations without touching anything else.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

PROMPTS: Dict[str, Dict[str, str]] = {
    "ko": {
        "chunk": (
            "YouTube 영상 transcript의 한 부분을 한국어 bullet point로 추출하라. "
            "고유명사·숫자·인용·핵심 주장만 남기고 잡담·추임새는 제거하라. "
            "절대 금지: '다음은', '요약입니다', '내용입니다', 'chunk', '부분', '제공된' 같은 메타·머리말. "
            "출력은 '*'로 시작하는 bullet 라인만. 머리말·결론 문장 금지."
        ),
        "final": (
            "여러 부분 요약이 주어진다. 영상 전체의 핵심을 한국어 2~3문장의 단일 단락 TL;DR로 응축하라. "
            "절대 금지: 'TL;DR', '요약하면', '다음은' 같은 메타 머리말. "
            "출력은 본문 단락 하나뿐. bullet·헤더·인용부호 금지."
        ),
    },
    "en": {
        "chunk": (
            "Extract bullet points from this YouTube transcript chunk. "
            "Keep proper nouns, numbers, quotes, and key claims; drop filler. "
            "Output ONLY bullet lines starting with '*'. No preamble, no 'Here is', "
            "no 'summary', no closing remarks."
        ),
        "final": (
            "Compress these chunk summaries into a single 2-3 sentence TL;DR paragraph. "
            "Output ONLY the paragraph. No 'TL;DR:', no 'In summary', no bullets."
        ),
    },
    "ja": {
        "chunk": (
            "YouTube動画の文字起こしの一部を日本語の箇条書きで抽出せよ。"
            "固有名詞・数字・引用・要点のみ残す。「以下は」「要約です」など前置きは禁止。"
            "出力は'*'で始まる箇条書きのみ。"
        ),
        "final": (
            "複数の部分要約から動画全体の核心を日本語2〜3文の単一段落TL;DRに凝縮せよ。"
            "「TL;DR」「以下は」など前置き禁止。出力は本文の段落のみ。"
        ),
    },
}

DEFAULT_MODEL = "gemini-2.5-flash"


def summarize_with_gemini(
    transcripts: List[Dict],
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    language: str = "ko",
    max_workers: int = 2,
) -> Dict[str, str]:
    """Summarize a list of transcript chunks via the Gemini API.

    `transcripts` matches ytt's shape: each entry has a `segments` list whose items
    have a `text` field. The function flattens segments per chunk, asks Gemini for a
    chunk-level summary in parallel (max_workers caps free-tier RPM exposure), then
    asks Gemini once more to compress the joined chunk summaries into a TL;DR.

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
    chunk_prompt = PROMPTS[language]["chunk"]
    final_prompt = PROMPTS[language]["final"]

    client = genai.Client(api_key=api_key)
    chunk_texts = [
        " ".join(seg["text"] for seg in chunk["segments"])
        for chunk in transcripts
    ]
    logger.info(
        "Gemini summary: %d chunks, model=%s, language=%s",
        len(chunk_texts), model, language,
    )

    no_thinking = types.ThinkingConfig(thinking_budget=0)

    def _summarize_chunk(idx_text):
        idx, text = idx_text
        try:
            response = client.models.generate_content(
                model=model,
                contents=text,
                config=types.GenerateContentConfig(
                    system_instruction=chunk_prompt,
                    temperature=0.3,
                    max_output_tokens=4096,
                    thinking_config=no_thinking,
                ),
            )
            return idx, (response.text or "").strip()
        except Exception as exc:
            logger.error("Chunk %d summary failed: %s", idx + 1, exc)
            return idx, f"[요약 실패: {exc}]"

    chunk_results: Dict[int, str] = {}
    workers = max(1, min(max_workers, len(chunk_texts)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(_summarize_chunk, (i, t))
            for i, t in enumerate(chunk_texts)
        ]
        for fut in as_completed(futures):
            idx, summary = fut.result()
            chunk_results[idx] = summary

    long_summary = "\n\n".join(chunk_results[i] for i in sorted(chunk_results))

    try:
        response = client.models.generate_content(
            model=model,
            contents=long_summary,
            config=types.GenerateContentConfig(
                system_instruction=final_prompt,
                temperature=0.3,
                max_output_tokens=1024,
                thinking_config=no_thinking,
            ),
        )
        short_summary = (response.text or "").strip() or "[최종 요약 비어있음]"
    except Exception as exc:
        logger.error("Final TL;DR summary failed: %s", exc)
        short_summary = f"[최종 요약 실패: {exc}]"

    return {"long_summary": long_summary, "short_summary": short_summary}
