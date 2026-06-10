"""Hermetic tests for gemini_summarize — no network, no API key."""
import json

import pytest

import gemini_summarize
from gemini_summarize import (
    _build_context_line,
    _generate,
    check_relevance,
    summarize_with_gemini,
)


class _FakeResponse:
    def __init__(self, text):
        self.text = text


class _FakeModels:
    def __init__(self, outcomes):
        # outcomes: list of Exception (raised) or str (returned as response.text)
        self.outcomes = list(outcomes)
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)


class _FakeClient:
    def __init__(self, outcomes):
        self.models = _FakeModels(outcomes)


def test_build_context_line_includes_all_parts():
    line = _build_context_line("My Video", "My Channel", "wine")
    assert line.startswith("[VIDEO CONTEXT]")
    assert "My Video" in line
    assert "My Channel" in line
    assert "wine" in line


def test_build_context_line_empty_when_no_parts():
    assert _build_context_line("", "", "") == ""


def test_check_relevance_returns_all_true_without_topic(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    videos = [{"title": "a"}, {"title": "b"}]
    assert check_relevance(videos, "") == [True, True]


def test_check_relevance_returns_all_true_without_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    videos = [{"title": "a"}, {"title": "b"}, {"title": "c"}]
    assert check_relevance(videos, "wine") == [True, True, True]


def test_check_relevance_empty_videos():
    assert check_relevance([], "wine") == []


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(gemini_summarize.time, "sleep", sleeps.append)
    monkeypatch.setattr(gemini_summarize, "_next_call_at", 0.0)
    return sleeps


def test_generate_retries_quota_error_with_api_delay(no_sleep):
    quota_err = Exception(
        "429 RESOURCE_EXHAUSTED: quota exceeded. retryDelay: 7s"
    )
    client = _FakeClient([quota_err, "ok"])
    response = _generate(client, "m", "contents", config=None, rpm=600)
    assert response.text == "ok"
    assert client.models.calls == 2
    assert 8 in no_sleep  # API-suggested 7s + 1


def test_generate_does_not_retry_non_quota_error(no_sleep):
    client = _FakeClient([ValueError("boom"), "never"])
    with pytest.raises(ValueError):
        _generate(client, "m", "contents", config=None, rpm=600)
    assert client.models.calls == 1


def test_generate_gives_up_after_max_attempts(no_sleep):
    errs = [Exception("429 RESOURCE_EXHAUSTED retryDelay: 0s") for _ in range(4)]
    client = _FakeClient(errs)
    with pytest.raises(Exception, match="RESOURCE_EXHAUSTED"):
        _generate(client, "m", "contents", config=None, rpm=600, attempts=4)
    assert client.models.calls == 4


def _transcripts(text="와인 시음 평가 내용"):
    return [{"segments": [{"text": text}]}]


def test_summarize_single_call_parses_json(monkeypatch, no_sleep):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    payload = json.dumps({"short_summary": "짧은 요약", "long_summary": "**핵심 결론**\n긴 요약"})
    client = _FakeClient([payload])
    monkeypatch.setattr(gemini_summarize.genai, "Client", lambda api_key: client)

    result = summarize_with_gemini(_transcripts(), video_title="t", channel_name="c")

    assert result == {"short_summary": "짧은 요약", "long_summary": "**핵심 결론**\n긴 요약"}
    assert client.models.calls == 1  # exactly one API call per video


def test_summarize_injects_reader_line(monkeypatch, no_sleep):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    captured = {}

    class _SpyModels(_FakeModels):
        def generate_content(self, **kwargs):
            captured.update(kwargs)
            return super().generate_content(**kwargs)

    client = _FakeClient([])
    client.models = _SpyModels([json.dumps({"short_summary": "s", "long_summary": "l"})])
    monkeypatch.setattr(gemini_summarize.genai, "Client", lambda api_key: client)

    summarize_with_gemini(_transcripts(), video_title="t", audience="중급 학습자, 구매 참고 목적")

    assert "[READER] 중급 학습자, 구매 참고 목적" in captured["contents"]
    assert "[VIDEO CONTEXT]" in captured["contents"]


def test_config_default_audience_empty():
    from config import DEFAULTS

    assert DEFAULTS["project"]["audience"] == ""


def test_summarize_falls_back_when_response_not_json(monkeypatch, no_sleep):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    client = _FakeClient(["그냥 평문 요약입니다"])
    monkeypatch.setattr(gemini_summarize.genai, "Client", lambda api_key: client)

    result = summarize_with_gemini(_transcripts())

    assert result["long_summary"] == "그냥 평문 요약입니다"
    assert result["short_summary"] == "그냥 평문 요약입니다"


def test_summarize_requires_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        summarize_with_gemini(_transcripts())


def test_generate_retries_503_overload(no_sleep):
    overload = Exception(
        "503 UNAVAILABLE. {'error': {'code': 503, 'message': 'high demand'}}"
    )
    client = _FakeClient([overload, "ok"])
    response = _generate(client, "m", "contents", config=None, rpm=600)
    assert response.text == "ok"
    assert client.models.calls == 2


def test_thinking_config_per_model_family():
    from gemini_summarize import _thinking_config

    assert _thinking_config("gemini-2.5-flash").thinking_budget == 0
    cfg3 = _thinking_config("gemini-3.5-flash")
    assert str(cfg3.thinking_level.value).lower() == "low"  # SDK normalizes to enum
    assert cfg3.thinking_budget is None


def test_config_default_gemini_model_and_rpm():
    from config import DEFAULTS

    assert DEFAULTS["processing"]["gemini_model"] == "gemini-3.5-flash"
    # Observed per-model free-tier limit is 5 RPM — default must stay below it.
    assert DEFAULTS["processing"]["gemini_rpm"] < 5
