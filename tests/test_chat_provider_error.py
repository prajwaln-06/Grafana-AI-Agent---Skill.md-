"""Tests for the chat endpoint's handling of a misconfigured LLM provider.

A provider misconfiguration (wrong provider, missing credentials, an
unusable key) is an operational fault. It must not be swallowed into a
generic "Internal server error": the caller gets a precise 503, and the
server logs the reason.

These tests also cover the regression that caused the reported failure: a
valid Gemini Developer API key that happens to carry the "AQ." prefix was
rejected as if it were a Vertex AI key, which broke every chat request.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from app import llm_provider
from app.llm_provider import LLMProviderConfigError, build_client_kwargs


def _s(**kw):
    base = dict(llm_provider="gemini_api", gemini_api_key="ai-studio-key", vertex_project=None,
                vertex_location=None, vertex_api_key=None)
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------
# The provider selection itself
# --------------------------------------------------------------------------

def test_cloud_style_prefix_is_not_treated_as_a_vertex_key():
    """Google issues Gemini Developer API keys with the 'AQ.' prefix as well,
    so the prefix must not decide which backend a key belongs to."""
    key = "AQ.Ab8RNexampleexampleexampleexampleexampleexampleexample"
    kwargs = build_client_kwargs(_s(gemini_api_key=key))
    assert kwargs == {"vertexai": False, "api_key": key}


def test_cloud_style_prefix_is_only_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="app.llm_provider"):
        build_client_kwargs(_s(gemini_api_key="AQ.something"))
    assert "AQ." in caplog.text
    assert "LLM_PROVIDER=vertex" in caplog.text


def test_vertex_provider_never_uses_the_gemini_key():
    """The explicit provider selection is the only thing that decides where a
    key is sent."""
    with pytest.raises(LLMProviderConfigError):
        build_client_kwargs(_s(llm_provider="vertex", gemini_api_key="AQ.something"))


def test_normal_gemini_key_is_unaffected():
    assert build_client_kwargs(_s(gemini_api_key="AIzaSyExample")) == {
        "vertexai": False,
        "api_key": "AIzaSyExample",
    }


# --------------------------------------------------------------------------
# The chat endpoint
# --------------------------------------------------------------------------

@pytest.fixture()
def client():
    """A client with the application's lifespan running, so the skill index
    is loaded and the chat endpoint takes its normal path."""
    from app.api.main import app
    with TestClient(app) as test_client:
        yield test_client


def test_chat_reports_a_provider_misconfiguration_as_503(client):
    """The pipeline's misconfiguration must not be downgraded into 'the ADK
    fallback failed too'."""
    with mock.patch(
        "app.pipeline.run_pipeline",
        side_effect=LLMProviderConfigError("LLM_PROVIDER=gemini_api requires GEMINI_API_KEY."),
    ), mock.patch(
        "app.pipeline.run_dependency_aware_pipeline",
        side_effect=LLMProviderConfigError("LLM_PROVIDER=gemini_api requires GEMINI_API_KEY."),
    ):
        response = client.post("/api/chat", json={"message": "how many requests per second are we serving"})

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "not configured correctly" in detail
    assert "GEMINI_API_KEY" in detail


def test_chat_reports_an_adk_provider_misconfiguration_as_503(client):
    """Same for the conversational fallback path."""
    with mock.patch(
        "app.pipeline.run_pipeline",
        side_effect=RuntimeError("pipeline unavailable in this test"),
    ), mock.patch(
        "app.pipeline.run_dependency_aware_pipeline",
        side_effect=RuntimeError("pipeline unavailable in this test"),
    ), mock.patch(
        "app.api.routes_chat.run_adk_agent",
        side_effect=LLMProviderConfigError("GEMINI_API_KEY is missing."),
    ):
        response = client.post("/api/chat", json={"message": "how many requests per second are we serving"})

    assert response.status_code == 503
    assert "not configured correctly" in response.json()["detail"]


def test_provider_errors_never_leak_key_material():
    """The configuration error message must not contain key material."""
    message = LLMProviderConfigError("LLM_PROVIDER=gemini_api requires GEMINI_API_KEY.").args[0]
    assert "AIza" not in message
    assert "AQ.Ab8" not in message
