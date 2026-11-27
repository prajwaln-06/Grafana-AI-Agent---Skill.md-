"""Exercise the Gemini transport with a fake SDK and clock."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from google.genai import errors as genai_errors
from google.genai import types

from app import llm_client, pipeline
from app.config import GeminiTransportSettings


@pytest.fixture(autouse=True)
def clear_clients():
    llm_client._clients.clear()
    llm_client._model_routes.clear()
    yield
    llm_client._clients.clear()
    llm_client._model_routes.clear()


def _fake_client(response_text=None, side_effect=None):
    client = MagicMock()
    if side_effect is not None:
        client.models.generate_content.side_effect = side_effect
    else:
        client.models.generate_content.return_value = MagicMock(text=response_text)
    return client


def test_happy_path_returns_parsed_json():
    fake_client = _fake_client(response_text='{"status": "ok"}')
    with patch("google.genai.Client", return_value=fake_client), patch("time.sleep"):
        result = llm_client.call_llm_json("prompt", "system", "key", "model")
    assert result.parsed == {"status": "ok"}


def test_strips_markdown_json_fence_if_present():
    fake_client = _fake_client(response_text='```json\n{"status": "ok"}\n```')
    with patch("google.genai.Client", return_value=fake_client), patch("time.sleep"):
        result = llm_client.call_llm_json("prompt", "system", "key", "model")
    assert result.parsed == {"status": "ok"}


def test_temperature_is_passed_for_deterministic_output():
    """Section 2 requirement: validation is deterministic, but the LLM
    phases themselves should also be as close to deterministic as the API
    allows -- temperature=0.0 is the whole point of this call, and a
    regression here would silently make Router/Generator output flaky."""
    fake_client = _fake_client(response_text='{"a": 1}')
    with patch("google.genai.Client", return_value=fake_client), patch("time.sleep"):
        llm_client.call_llm_json("prompt", "system", "key", "model")
    _, kwargs = fake_client.models.generate_content.call_args
    assert kwargs["config"].temperature == 0.0
    assert kwargs["config"].response_mime_type == "application/json"
    assert kwargs["model"] == "model"


def test_404_model_not_found_fails_immediately_without_retrying():
    """The concrete bug this guards against: a misconfigured GEMINI_MODEL
    (e.g. a shut-down or misspelled model name) used to retry 3x with
    exponential backoff before failing, adding ~6 seconds of pure waste to
    every single request. A 404 is permanent -- it must fail on the first
    attempt."""
    not_found = genai_errors.ClientError(404, {"error": {"message": "model not found", "status": "NOT_FOUND"}})
    fake_client = _fake_client(side_effect=not_found)
    with patch("google.genai.Client", return_value=fake_client), patch("time.sleep") as mock_sleep:
        with pytest.raises(llm_client.LLMCallError, match="non-retryable"):
            llm_client.call_llm_json("prompt", "system", "key", "bad-model-name")
    assert fake_client.models.generate_content.call_count == 1
    assert not mock_sleep.called


def test_401_auth_error_fails_immediately():
    auth_error = genai_errors.ClientError(401, {"error": {"message": "bad key", "status": "UNAUTHENTICATED"}})
    fake_client = _fake_client(side_effect=auth_error)
    with patch("google.genai.Client", return_value=fake_client), patch("time.sleep"):
        with pytest.raises(llm_client.LLMCallError, match="non-retryable"):
            llm_client.call_llm_json("prompt", "system", "bad-key", "model")
    assert fake_client.models.generate_content.call_count == 1


def test_429_rate_limit_is_retried_then_succeeds():
    rate_limited = genai_errors.ClientError(429, {"error": {"message": "rate limited", "status": "RESOURCE_EXHAUSTED"}})
    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = [rate_limited, MagicMock(text='{"ok": true}')]
    with patch("google.genai.Client", return_value=fake_client), patch("time.sleep") as mock_sleep:
        result = llm_client.call_llm_json("prompt", "system", "key", "model")
    assert result.parsed == {"ok": True}
    assert fake_client.models.generate_content.call_count == 2
    assert mock_sleep.called  # backed off before the retry


def test_500_server_error_is_retried():
    server_error = genai_errors.ServerError(500, {"error": {"message": "internal", "status": "INTERNAL"}})
    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = [server_error, MagicMock(text='{"ok": true}')]
    with patch("google.genai.Client", return_value=fake_client), patch("time.sleep"):
        result = llm_client.call_llm_json("prompt", "system", "key", "model")
    assert result.parsed == {"ok": True}


def test_exhausting_all_retries_raises_llm_call_error():
    server_error = genai_errors.ServerError(503, {"error": {"message": "unavailable", "status": "UNAVAILABLE"}})
    fake_client = _fake_client(side_effect=server_error)
    with patch("google.genai.Client", return_value=fake_client), patch("time.sleep"):
        with pytest.raises(llm_client.LLMCallError):
            llm_client.call_llm_json("prompt", "system", "key", "model", max_retries=2)
    assert fake_client.models.generate_content.call_count == 2


def test_malformed_json_response_is_retried_then_raises_if_never_valid():
    fake_client = _fake_client(response_text="not json at all")
    with patch("google.genai.Client", return_value=fake_client), patch("time.sleep"):
        with pytest.raises(llm_client.LLMCallError):
            llm_client.call_llm_json("prompt", "system", "key", "model", max_retries=2)
    assert fake_client.models.generate_content.call_count == 2


def test_empty_response_body_is_treated_as_transient_and_retried():
    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = [MagicMock(text=None), MagicMock(text='{"ok": true}')]
    with patch("google.genai.Client", return_value=fake_client), patch("time.sleep"):
        result = llm_client.call_llm_json("prompt", "system", "key", "model")
    assert result.parsed == {"ok": True}


def test_client_is_created_once_for_parallel_calls_and_separates_vertex_mode(monkeypatch):
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    with patch("google.genai.Client", return_value=_fake_client('{"a": 1}')) as client:
        with ThreadPoolExecutor(max_workers=4) as threads:
            results = list(threads.map(lambda _: llm_client.call_llm_json("q", "s", "key", "model"), range(8)))
        assert client.call_count == 1 and all(r.parsed == {"a": 1} for r in results)
        assert client.call_args.kwargs["http_options"].timeout == 60_000
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
        llm_client.call_llm_json("q", "s", "key", "model")
        assert client.call_count == 2


def test_transport_settings_and_jittered_backoff(monkeypatch):
    clock = [0.0]
    times = []

    def sleep(seconds):
        clock[0] += seconds

    def generate(**kwargs):
        times.append(clock[0])
        if len(times) < 3:
            raise genai_errors.ServerError(503, {"error": {"message": "busy"}})
        return SimpleNamespace(text='{"ok": true}')

    settings = GeminiTransportSettings(_env_file=None, gemini_http_timeout_seconds=43, gemini_max_attempts=3)
    monkeypatch.setattr(llm_client, "get_gemini_transport_settings", lambda: settings)
    monkeypatch.setattr(llm_client.time, "sleep", sleep)
    monkeypatch.setattr(llm_client.random, "uniform", lambda low, high: 1.1 if (low, high) == (0.8, 1.2) else 1)
    with patch("google.genai.Client", return_value=_fake_client(side_effect=generate)) as client:
        assert llm_client.call_llm_json("q", "s", "key", "model").parsed == {"ok": True}
    assert times == pytest.approx([0, 0.55, 1.65])
    assert client.call_args.kwargs["http_options"].timeout == 43_000


@pytest.mark.parametrize("header,delay", [("4", 4), ("Thu, 01 Jan 1970 00:00:09 GMT", 3)])
def test_rate_limit_honors_retry_after(monkeypatch, header, delay):
    rate_limit = genai_errors.ClientError(429, {"error": {"message": "busy"}},
                                          SimpleNamespace(headers={"Retry-After": header}))
    monkeypatch.setattr(llm_client.time, "time", lambda: 6)
    fake = _fake_client(side_effect=[rate_limit, SimpleNamespace(text="{}")])
    with patch("google.genai.Client", return_value=fake), patch.object(llm_client.time, "sleep") as sleep:
        llm_client.call_llm_json("q", "s", "key", "model")
    sleep.assert_called_once_with(delay)


def test_parse_failure_retries_once_without_sleep_even_with_more_attempts():
    fake = _fake_client("invalid")
    with patch("google.genai.Client", return_value=fake), patch.object(llm_client.time, "sleep") as sleep:
        with pytest.raises(llm_client.LLMCallError, match="after 2 attempts"):
            llm_client.call_llm_json("q", "s", "key", "model", max_retries=4)
    assert fake.models.generate_content.call_count == 2
    sleep.assert_not_called()


@pytest.mark.parametrize("available", [True, False])
def test_model_probe_is_memoized_for_success_and_exhaustion(available):
    attempted = []

    def generate(model, **kwargs):
        attempted.append(model)
        if available and model == "gemini-2.5-flash-lite":
            return SimpleNamespace(text="{}")
        raise genai_errors.ClientError(404, {"error": {"message": "missing"}})

    with patch("google.genai.Client", return_value=_fake_client(side_effect=generate)):
        for _ in range(2):
            if available:
                llm_client.call_llm_json("q", "s", "AIza-test", "gemini-3.5-flash-lite")
            else:
                with pytest.raises(llm_client.LLMCallError, match="non-retryable"):
                    llm_client.call_llm_json("q", "s", "AIza-test", "gemini-3.5-flash-lite")
    expected = ["gemini-3.5-flash-lite", "gemini-2.5-flash-lite"]
    expected += ["gemini-2.5-flash-lite"] if available else ["gemini-2.5-flash", "gemini-2.0-flash"]
    assert attempted == expected


def test_none_policy_kwargs_send_the_original_config():
    fake = _fake_client("{}")
    with patch("google.genai.Client", return_value=fake):
        llm_client.call_llm_json("original", "system", "key", "model",
                                 thinking_budget=None, max_output_tokens=None, timeout_seconds=None)
    expected = types.GenerateContentConfig(
        system_instruction="system", response_mime_type="application/json", temperature=0.0)
    assert fake.models.generate_content.call_args.kwargs == {"model": "model", "contents": "original", "config": expected}


def test_thinking_rejection_retries_the_same_request_without_only_that_option():
    unsupported = genai_errors.ClientError(400, {"error": {"message": "Thinking budget is not supported"}})
    fake = _fake_client(side_effect=[unsupported, SimpleNamespace(text="{}")])
    with patch("google.genai.Client", return_value=fake) as client, patch.object(llm_client.time, "sleep") as sleep:
        llm_client.call_llm_json("q", "s", "key", "model", thinking_budget=0, max_output_tokens=123, timeout_seconds=5)
    before, after = [c.kwargs["config"] for c in fake.models.generate_content.call_args_list]
    assert before.thinking_config.thinking_budget == 0
    assert after == before.model_copy(update={"thinking_config": None})
    assert after.max_output_tokens == 123 and after.http_options.timeout == 5_000
    assert client.call_count == 1
    sleep.assert_not_called()


@pytest.mark.parametrize("datasources,action,apply", [
    (["prometheus"], "read_query", False),
    (["prometheus"], "propose_alert_rule", False),
    (["opensearch", "prometheus"], "read_query", False),
    (["opensearch", "opensearch"], "read_query", True),
])
def test_generator_policy_preserves_prometheus_and_alert_config(settings, skill_index, datasources, action, apply):
    settings = settings.model_copy(update={"alert_rule_creation_enabled": action == "propose_alert_rule",
                                           "gemini_generator_thinking_budget": 8,
                                           "gemini_generator_timeout_seconds": 9})
    fake = _fake_client("{}")
    matched = [{"data_source": source} for source in datasources]
    with patch("google.genai.Client", return_value=fake):
        asyncio.run(pipeline._run_generator("q", matched, False, pipeline.GeneratorContext(),
                                            skill_index, settings, pipeline.PipelineContext(), action))
    config = fake.models.generate_content.call_args.kwargs["config"]
    if apply:
        assert config.thinking_config.thinking_budget == 8
        assert config.max_output_tokens == 8192 and config.http_options.timeout == 9_000
    else:
        assert config == types.GenerateContentConfig(
            system_instruction=pipeline._build_generator_instructions(settings, action),
            temperature=0.0, response_mime_type="application/json")


@pytest.mark.parametrize("budget", [None, 0])
def test_router_thinking_is_opt_in(settings, skill_index, budget):
    settings = settings.model_copy(update={"gemini_router_thinking_budget": budget})
    fake = _fake_client("{}")
    with patch("google.genai.Client", return_value=fake):
        asyncio.run(pipeline._run_router("q", skill_index, settings, pipeline.PipelineContext()))
    expected = types.GenerateContentConfig(system_instruction=pipeline._build_router_instructions(settings),
                                           temperature=0.0, response_mime_type="application/json")
    if budget is not None:
        expected.thinking_config = types.ThinkingConfig(thinking_budget=budget)
    assert fake.models.generate_content.call_args.kwargs["config"] == expected
