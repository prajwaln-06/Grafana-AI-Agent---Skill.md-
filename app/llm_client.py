"""
llm_client.py

The ONLY module that talks to the Gemini API directly. Every pipeline phase
calls `call_llm_json()` and gets back a parsed dict (or a well-typed
failure) -- none of them import google.genai themselves. This isolation
means: (a) swapping models/providers later touches one file, and (b) every
other module in the pipeline is unit-testable by mocking this one function,
without needing a real API key or network access.

NOTE FOR WHOEVER DEPLOYS THIS: this wraps the `google-genai` SDK
(`pip install google-genai`). The exact call shape below matches that
package's documented usage as of this writing, but SDK call signatures do
shift between versions -- verify this against the installed version's docs
if `call_llm_json` raises immediately on first real use. This module could
not be exercised against a live Gemini endpoint in the environment this was
written/reviewed in either (no network path to generativelanguage.
googleapis.com from that sandbox) -- see HANDOFF.md's testing section for
exactly what to run once you have a real API key and network access.

FAIL-FAST vs RETRY: `google.genai.errors.APIError` carries the HTTP status
in `.code`. A 4xx here (400 bad request -- e.g. a malformed config field a
newer/older SDK version doesn't recognize, 401/403 -- bad or missing API
key, 404 -- unknown model name) means every retry will fail identically;
retrying it 3x with exponential backoff only adds ~6 seconds of latency to
every single request before surfacing the same error. Those fail
immediately, with the actual message intact. 429 (rate limit) and 5xx
(server error) are genuinely transient and go through the normal retry
path, as does any exception that isn't an APIError at all (network errors,
timeouts).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 2


class LLMCallError(Exception):
    """Raised when every retry attempt has been exhausted, OR immediately
    for a non-retryable failure (see _PermanentError below). Callers should
    treat this as an internal_error result, never as a signal to guess at
    the answer themselves."""


class _PermanentError(Exception):
    """A failure that will not resolve itself on retry (bad API key,
    unknown model name, malformed request) -- raised instead of
    _TransientError so call_llm_json can fail immediately rather than
    burning ~6 seconds of exponential backoff to reproduce the same
    error 3 times."""


@dataclass
class LLMResponse:
    text: str
    parsed: dict | list


def call_llm_json(prompt: str, system_instruction: str, api_key: str, model: str,
                   max_retries: int = MAX_RETRIES) -> LLMResponse:
    """Calls Gemini with the given prompt + system instruction, requesting a
    JSON-only response, retrying with exponential backoff on transient
    failures. Raises LLMCallError if every attempt fails, or if every
    attempt returns text that doesn't parse as JSON (also treated as a
    failure -- a phase that can't parse its own model's output has nothing
    safe to do with it)."""
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            text = _call_gemini_once(prompt, system_instruction, api_key, model)
            parsed = _parse_json_response(text)
            return LLMResponse(text=text, parsed=parsed)
        except _PermanentError as e:
            raise LLMCallError(f"LLM call failed with a non-retryable error: {e}") from e
        except (_TransientError, json.JSONDecodeError) as e:
            last_error = e
            if attempt < max_retries - 1:
                backoff = BASE_BACKOFF_SECONDS * (2 ** attempt)
                logger.warning("LLM call attempt %d/%d failed (%s); retrying in %ds",
                                attempt + 1, max_retries, e, backoff)
                time.sleep(backoff)
                continue
    raise LLMCallError(f"LLM call failed after {max_retries} attempts: {last_error}")


class _TransientError(Exception):
    """Wraps SDK-level failures (network, rate limit, 5xx) that are worth
    retrying, as opposed to a JSON-parse failure which is handled
    separately above but retried the same way (a retried call sometimes
    produces well-formed JSON even when one attempt didn't)."""


_PERMANENT_HTTP_STATUS_CODES = {400, 401, 403, 404}


def _call_gemini_once(prompt: str, system_instruction: str, api_key: str, model: str) -> str:
    try:
        from google import genai
        from google.genai import errors, types
    except ImportError as e:
        raise _PermanentError(
            "google-genai is not installed. Run: pip install google-genai"
        ) from e

    try:
        import os
        from app.config import get_settings
        settings = get_settings()
        is_vertex = api_key.startswith("AQ.") or os.environ.get("VERTEXAI", "").lower() in ("true", "1")
        if is_vertex:
            client = genai.Client(
                vertexai=True,
                api_key=api_key,
                location=getattr(settings, "vertex_location", "global"),
                project=getattr(settings, "vertex_project", "project-8d47da29-7cf0-45f0-b55"),
            )
            full_content = f"SYSTEM INSTRUCTION:\n{system_instruction}\n\nUSER REQUEST:\n{prompt}"
            response = client.models.generate_content(
                model=model,
                contents=full_content,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.0,
                ),
            )
        else:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    temperature=0.0,
                ),
            )
        text = response.text
        if text is None:
            raise _TransientError("Gemini returned an empty response body.")
        return text
    except _TransientError:
        raise
    except errors.APIError as e:
        code = getattr(e, "code", None)
        if code in _PERMANENT_HTTP_STATUS_CODES:
            raise _PermanentError(
                f"HTTP {code} from Gemini API (model={model!r}): {getattr(e, 'message', e)}. "
                f"Check GEMINI_API_KEY and GEMINI_MODEL in your .env -- this will not resolve "
                f"on retry."
            ) from e
        raise _TransientError(str(e)) from e
    except Exception as e:  # noqa: BLE001 -- anything else from the call
        # (network errors, timeouts, an SDK version whose exception types
        # don't match the errors.APIError hierarchy checked above) is
        # treated as transient and retried, up to max_retries.
        raise _TransientError(str(e)) from e


def _parse_json_response(text: str) -> dict | list:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Strip a ```json ... ``` fence if the model wrapped its output in
        # one despite response_mime_type -- happens occasionally across
        # model versions and is cheap to defend against.
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    return json.loads(cleaned)
