"""
Tests llm_client.py's own logic (retry/backoff, permanent-vs-transient
error classification, JSON-fence stripping) by mocking the `google.genai`
SDK boundary, NOT by making a real network call -- there is no network path
to Google's API from this environment (or from most CI runners). See
HANDOFF.md's testing section for the live smoke test to run once you have a
real GEMINI_API_KEY and network access.
"""
from unittest.mock import MagicMock, patch

import pytest
from google.genai import errors as genai_errors

from app import llm_client


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
