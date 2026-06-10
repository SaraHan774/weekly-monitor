"""Tests for config.py — layering, validation, runtime env detection."""
import pytest

from config import (
    DEFAULTS,
    VALID_NOTIFICATION_CHANNELS,
    _deep_merge,
    load_config,
    validate_runtime,
)


def test_load_missing_file_returns_defaults(tmp_path):
    cfg = load_config(tmp_path / "nonexistent.yaml")
    assert cfg["project"]["name"] == DEFAULTS["project"]["name"]
    assert cfg["discovery"]["top_n"] == DEFAULTS["discovery"]["top_n"]


def test_load_partial_config_layers_on_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "project:\n  name: 'Custom'\ndiscovery:\n  top_n: 3\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg["project"]["name"] == "Custom"
    assert cfg["project"]["language"] == DEFAULTS["project"]["language"]  # untouched
    assert cfg["discovery"]["top_n"] == 3
    assert cfg["discovery"]["lookback_days"] == DEFAULTS["discovery"]["lookback_days"]


def test_deep_merge_recurses_into_nested_dicts():
    base = {"a": {"b": 1, "c": 2}, "d": 3}
    overrides = {"a": {"b": 99}}
    merged = _deep_merge(base, overrides)
    assert merged == {"a": {"b": 99, "c": 2}, "d": 3}


def test_deep_merge_does_not_mutate_inputs():
    base = {"a": {"b": 1}}
    overrides = {"a": {"b": 2}}
    _deep_merge(base, overrides)
    assert base["a"]["b"] == 1
    assert overrides["a"]["b"] == 2


def test_load_rejects_invalid_notification_channel(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("notification:\n  channel: 'carrier-pigeon'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="notification.channel"):
        load_config(path)


def test_load_rejects_zero_top_n(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("discovery:\n  top_n: 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="top_n"):
        load_config(path)


def test_load_rejects_zero_lookback_days(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("discovery:\n  lookback_days: 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="lookback_days"):
        load_config(path)


def test_validate_runtime_passes_when_processing_with_gemini_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
    cfg = {"notification": {"channel": "none"}}
    validate_runtime(cfg, will_send_email=False, will_process=True)  # no raise


def test_validate_runtime_fails_when_processing_without_gemini_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    cfg = {"notification": {"channel": "none"}}
    with pytest.raises(SystemExit) as exc:
        validate_runtime(cfg, will_send_email=False, will_process=True)
    assert "GEMINI_API_KEY" in str(exc.value)


def test_validate_runtime_skips_gemini_check_when_no_process(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    cfg = {"notification": {"channel": "none"}}
    validate_runtime(cfg, will_send_email=False, will_process=False)  # no raise


def test_validate_runtime_requires_gmail_vars_when_emailing_via_gmail(monkeypatch):
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    cfg = {"notification": {"channel": "gmail"}}
    with pytest.raises(SystemExit) as exc:
        validate_runtime(cfg, will_send_email=True, will_process=False)
    msg = str(exc.value)
    assert "GMAIL_USER" in msg and "GMAIL_APP_PASSWORD" in msg


def test_validate_runtime_skips_gmail_when_no_email(monkeypatch):
    monkeypatch.delenv("GMAIL_USER", raising=False)
    cfg = {"notification": {"channel": "gmail"}}
    validate_runtime(cfg, will_send_email=False, will_process=False)  # no raise


def test_validate_runtime_skips_gmail_for_none_channel(monkeypatch):
    monkeypatch.delenv("GMAIL_USER", raising=False)
    cfg = {"notification": {"channel": "none"}}
    validate_runtime(cfg, will_send_email=True, will_process=False)  # no raise


def test_validate_runtime_aggregates_all_missing_vars(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    cfg = {"notification": {"channel": "gmail"}}
    with pytest.raises(SystemExit) as exc:
        validate_runtime(cfg, will_send_email=True, will_process=True)
    msg = str(exc.value)
    assert "GEMINI_API_KEY" in msg
    assert "GMAIL_USER" in msg
    assert "GMAIL_APP_PASSWORD" in msg


def test_valid_notification_channels_includes_gmail_and_none():
    assert "gmail" in VALID_NOTIFICATION_CHANNELS
    assert "none" in VALID_NOTIFICATION_CHANNELS


def test_processing_defaults_include_retry_params():
    """retry_attempts and retry_backoff_sec must have sensible defaults so older
    config.yaml files that omit them keep working after the upgrade."""
    assert DEFAULTS["processing"]["retry_attempts"] >= 1
    assert DEFAULTS["processing"]["retry_backoff_sec"] >= 0


def test_load_uses_default_retry_when_config_omits_them(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("processing:\n  whisper_model: 'small'\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg["processing"]["whisper_model"] == "small"
    assert cfg["processing"]["retry_attempts"] == DEFAULTS["processing"]["retry_attempts"]
    assert cfg["processing"]["retry_backoff_sec"] == DEFAULTS["processing"]["retry_backoff_sec"]


def test_load_overrides_retry_params_when_provided(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "processing:\n  retry_attempts: 5\n  retry_backoff_sec: 0.5\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg["processing"]["retry_attempts"] == 5
    assert cfg["processing"]["retry_backoff_sec"] == 0.5


def test_relevance_filter_defaults_to_false(tmp_path):
    cfg = load_config(tmp_path / "missing.yaml")
    assert cfg["discovery"]["relevance_filter"] is False


def test_relevance_filter_can_be_enabled(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("discovery:\n  relevance_filter: true\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg["discovery"]["relevance_filter"] is True
