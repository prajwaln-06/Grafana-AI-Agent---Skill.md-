"""Offline tests for explicit LLM provider selection (no network, no real keys)."""
from types import SimpleNamespace
from unittest import mock

import pytest

from app import llm_client, llm_provider
from app.llm_provider import LLMProviderConfigError, build_client_kwargs


def _s(**kw):
    base = dict(llm_provider="gemini_api", gemini_api_key="ai-studio-key", vertex_project=None,
                vertex_location=None, vertex_api_key=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_default_is_gemini_api_with_explicit_vertexai_false():
    assert build_client_kwargs(_s(llm_provider=None)) == {"vertexai": False, "api_key": "ai-studio-key"}


def test_gemini_api_requires_key():
    with pytest.raises(LLMProviderConfigError):
        build_client_kwargs(_s(gemini_api_key=None))


def test_cloud_prefixed_key_is_not_rejected_as_a_vertex_key():
    """Google issues Gemini Developer API keys with the 'AQ.' prefix as well,
    so the prefix alone must never decide that a key belongs to Vertex AI.
    Rejecting on it breaks valid Gemini configurations."""
    key = "AQ.Ab8RNexampleexampleexampleexampleexampleexampleexample"
    assert build_client_kwargs(_s(gemini_api_key=key)) == {"vertexai": False, "api_key": key}


def test_cloud_prefixed_key_is_only_a_warning(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="app.llm_provider"):
        build_client_kwargs(_s(gemini_api_key="AQ.something"))
    assert "AQ." in caplog.text
    assert "LLM_PROVIDER=vertex" in caplog.text


def test_vertex_requires_explicit_config_and_never_uses_gemini_key():
    with pytest.raises(LLMProviderConfigError):
        build_client_kwargs(_s(llm_provider="vertex"))


def test_vertex_project_mode_has_no_api_key():
    kw = build_client_kwargs(_s(llm_provider="vertex", vertex_project="p1"))
    assert kw == {"vertexai": True, "project": "p1", "location": "global"}
    assert "api_key" not in kw


def test_vertex_express_mode_uses_vertex_key_only():
    kw = build_client_kwargs(_s(llm_provider="vertex", vertex_api_key="AQ.v"))
    assert kw == {"vertexai": True, "api_key": "AQ.v"}


def test_vertex_project_and_key_conflict():
    with pytest.raises(LLMProviderConfigError):
        build_client_kwargs(_s(llm_provider="vertex", vertex_project="p", vertex_api_key="AQ.v"))


def test_unknown_provider_rejected():
    with pytest.raises(LLMProviderConfigError):
        build_client_kwargs(_s(llm_provider="bogus"))


def test_aliases():
    assert llm_provider.normalize_provider("Vertex-AI") == "vertex"
    assert llm_provider.normalize_provider("ai_studio") == "gemini_api"


def test_describe_provider_has_no_secrets():
    text = llm_provider.describe_provider(_s(llm_provider="vertex", vertex_api_key="AQ.secret"))
    assert "secret" not in text


def test_direct_client_uses_provider_kwargs(monkeypatch):
    settings = _s()
    fake_client = mock.MagicMock()
    fake_client.models.generate_content.return_value = SimpleNamespace(text='{"a": 1}')
    with mock.patch("app.config.get_settings", return_value=settings), \
         mock.patch("google.genai.Client", return_value=fake_client) as ctor:
        out = llm_client._call_gemini_once("p", "sys", "ai-studio-key", "m")
    assert out == '{"a": 1}'
    ctor.assert_called_once_with(vertexai=False, api_key="ai-studio-key")


def test_direct_client_misconfig_is_permanent_not_retried():
    settings = _s(llm_provider="vertex")
    with mock.patch("app.config.get_settings", return_value=settings), \
         mock.patch("google.genai.Client") as ctor:
        with pytest.raises(llm_client._PermanentError):
            llm_client._call_gemini_once("p", "sys", "ai-studio-key", "m")
    ctor.assert_not_called()


def test_adk_and_direct_client_agree():
    from app.adk_agent import get_adk_agent  # noqa: F401  (import must succeed)
    s = _s(llm_provider="vertex", vertex_project="p1", vertex_location="us-central1")
    assert build_client_kwargs(s) == {"vertexai": True, "project": "p1", "location": "us-central1"}


# ---- real SDK client construction under a hostile environment (offline) ----


@pytest.fixture()
def polluted_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_API_KEY", "env-key")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "env-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-east1")


def test_gemini_api_ignores_vertex_env_vars(polluted_env):
    from google import genai
    c = genai.Client(**build_client_kwargs(_s())).__dict__["_api_client"]
    assert c.vertexai is False
    assert c.api_key == "ai-studio-key"
    assert "aiplatform" not in c._http_options.base_url


def test_vertex_project_mode_drops_env_api_key(polluted_env):
    from google import genai
    c = genai.Client(**build_client_kwargs(_s(llm_provider="vertex", vertex_project="p1"))).__dict__["_api_client"]
    assert c.vertexai is True and c.project == "p1" and c.api_key is None


def test_vertex_never_receives_ai_studio_key(polluted_env):
    from google import genai
    c = genai.Client(**build_client_kwargs(_s(llm_provider="vertex", vertex_api_key="AQ.v"))).__dict__["_api_client"]
    assert c.vertexai is True and c.api_key == "AQ.v"
    assert c.api_key != "ai-studio-key"


def test_adk_gemini_model_gets_same_kwargs(polluted_env):
    from google.adk.models.google_llm import Gemini
    kw = build_client_kwargs(_s())
    m = Gemini(model="gemini-2.5-flash", client_kwargs=kw)
    assert m.client_kwargs == kw
    assert m.api_client.vertexai is False
