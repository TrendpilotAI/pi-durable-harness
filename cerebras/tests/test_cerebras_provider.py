"""Cerebras as a chat LLM provider (``PIPELINE_LLM_PROVIDER=cerebras``).

Only the Cerebras SDK constructor is faked; the factory, settings and model
resolution under test are the production code.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

import app.clients.llm as llm
from app.api import _resolve_chat_model
from app.config import Settings


class _RecordingAsyncCerebras:
    """Stands in for ``cerebras.cloud.sdk.AsyncCerebras``."""

    instances: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        type(self).instances.append(kwargs)


@pytest.fixture
def recorded_cerebras(monkeypatch):
    import cerebras.cloud.sdk as sdk

    _RecordingAsyncCerebras.instances = []
    monkeypatch.setattr(sdk, "AsyncCerebras", _RecordingAsyncCerebras)
    return _RecordingAsyncCerebras.instances


@pytest.fixture
def recorded_openai(monkeypatch):
    import openai

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: calls.append(kw) or object())
    return calls


def _settings(monkeypatch, **values: Any) -> None:
    defaults = {
        "llm_provider": "cerebras",
        "cerebras_api_key": SecretStr(""),
        "cerebras_base_url": "https://cerebras.test",
        "openrouter_api_key": SecretStr("sk-or-test"),
        "openai_api_key": SecretStr("sk-openai-direct"),
    }
    defaults.update(values)
    for name, value in defaults.items():
        monkeypatch.setattr(llm.settings, name, value)


def test_cerebras_chat_client_uses_only_the_cerebras_key(
    monkeypatch, recorded_cerebras, recorded_openai
):
    _settings(monkeypatch, cerebras_api_key=SecretStr("csk-test"))

    assert llm.get_chat_client() is not None
    (call,) = recorded_cerebras
    assert call["api_key"] == "csk-test"
    assert call["base_url"] == "https://cerebras.test"
    assert call["timeout"] == llm.CHAT_CEREBRAS.timeout
    assert call["max_retries"] == llm.CHAT_CEREBRAS.max_retries
    # The SDK default opens a blocking sync connection inside __init__,
    # which would stall the event loop on every chat turn.
    assert call["warm_tcp_connection"] is False
    assert recorded_openai == [], "Cerebras provider built an OpenAI/OpenRouter client"


def test_cerebras_without_key_returns_none_and_never_falls_back(
    monkeypatch, recorded_cerebras, recorded_openai
):
    _settings(monkeypatch)

    assert llm.get_chat_client() is None
    assert recorded_cerebras == []
    assert recorded_openai == []


def test_transcription_stays_on_openrouter_under_cerebras(
    monkeypatch, recorded_cerebras, recorded_openai
):
    _settings(monkeypatch, cerebras_api_key=SecretStr("csk-test"))
    llm.get_transcription_client.cache_clear()
    try:
        assert llm.get_transcription_client() is not None
    finally:
        llm.get_transcription_client.cache_clear()
    assert recorded_cerebras == []
    (call,) = recorded_openai
    assert call["api_key"] == "sk-or-test"


def test_cerebras_maps_catalog_slash_ids_to_the_configured_model(monkeypatch):
    _settings(monkeypatch, cerebras_model="qwen-3.8-27b")
    monkeypatch.setattr("app.api.settings.llm_provider", "cerebras")
    monkeypatch.setattr("app.api.settings.cerebras_model", "qwen-3.8-27b")

    assert _resolve_chat_model("inception/mercury-3") == "qwen-3.8-27b"
    # A bare id is already a Cerebras model id and passes through.
    assert _resolve_chat_model("gpt-oss-120b") == "gpt-oss-120b"


def test_settings_read_cerebras_env(monkeypatch):
    monkeypatch.setenv("PIPELINE_LLM_PROVIDER", "cerebras")
    monkeypatch.setenv("PIPELINE_CEREBRAS_API_KEY", "csk-env")
    monkeypatch.setenv("PIPELINE_CEREBRAS_MODEL", "gpt-oss-120b")

    s = Settings(_env_file=None)
    assert s.llm_provider == "cerebras"
    assert s.cerebras_api_key.get_secret_value() == "csk-env"
    assert s.cerebras_model == "gpt-oss-120b"
    assert s.cerebras_base_url == "https://api.cerebras.ai"
