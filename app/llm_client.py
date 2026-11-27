"""Shared Gemini JSON transport for the Router and Generator."""
from __future__ import annotations

import json
import logging
import math
import os
import random
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from threading import Lock

from app.config import get_gemini_transport_settings

logger = logging.getLogger(__name__)

BASE_BACKOFF_SECONDS = 0.5
_PERMANENT_HTTP_STATUS_CODES = {400, 401, 403, 404}
_STUDIO_MODELS = ("gemini-3.5-flash-lite", "gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.0-flash")
_client_lock = Lock()
_clients: dict[tuple, object] = {}
_model_routes: dict[tuple, _ModelRoute] = {}


class LLMCallError(Exception):
    """Gemini returned a permanent failure or exhausted the allowed attempts."""


class _PermanentError(Exception):
    pass


class _TransientError(Exception):
    def __init__(self, message: str, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class LLMResponse:
    text: str
    parsed: dict | list


@dataclass
class _ModelRoute:
    """Remember prior 404s even if the next model temporarily fails."""
    candidate: int = 0
    confirmed: bool = False
    lock: Lock = field(default_factory=Lock)


def call_llm_json(prompt: str, system_instruction: str, api_key: str, model: str,
                  max_retries: int | None = None, *, thinking_budget: int | None = None,
                  max_output_tokens: int | None = None, timeout_seconds: float | None = None) -> LLMResponse:
    """Send the original prompt and parse JSON, with bounded retries."""
    settings = get_gemini_transport_settings()
    attempts = settings.gemini_max_attempts if max_retries is None else max_retries
    last_error: Exception | None = None
    parse_retry_used = False
    transport_retries = 0
    attempt = 0
    for attempt in range(1, attempts + 1):
        try:
            text = _call_gemini_once(
                prompt, system_instruction, api_key, model, settings.gemini_http_timeout_seconds,
                thinking_budget=thinking_budget, max_output_tokens=max_output_tokens, timeout_seconds=timeout_seconds)
            return LLMResponse(text=text, parsed=_parse_json_response(text))
        except _PermanentError as exc:
            raise LLMCallError(f"LLM call failed with a non-retryable error: {exc}") from exc
        except json.JSONDecodeError as exc:
            last_error = exc
            if parse_retry_used:
                break
            parse_retry_used = True
        except _TransientError as exc:
            last_error = exc
            if attempt < attempts:
                delay = BASE_BACKOFF_SECONDS * (2 ** transport_retries) * random.uniform(0.8, 1.2)
                delay = max(delay, exc.retry_after)
                transport_retries += 1
                logger.warning("Gemini attempt %d/%d failed; retrying in %.2fs", attempt, attempts, delay)
                time.sleep(delay)
    raise LLMCallError(f"LLM call failed after {attempt} attempts: {last_error}") from last_error


def _client_for(genai, types, api_key: str, timeout_seconds: float):
    use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("true", "1")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION")
    key = api_key.strip() if api_key else api_key
    studio = bool(key and key.startswith("AIza"))
    if studio:
        options = {"api_key": key, "vertexai": False}
        identity = (key, False, None, None)
    elif use_vertex and project and key and key.startswith("AQ."):
        options = {"vertexai": True, "project": project, "location": "global"}
        identity = (api_key, True, project, "global")
    else:
        options = {"api_key": api_key}
        identity = (api_key, use_vertex, project if use_vertex else None, location if use_vertex else None)
    with _client_lock:
        client = _clients.get(identity)
        if client is None:
            http = types.HttpOptions(timeout=round(timeout_seconds * 1000))
            client = _clients[identity] = genai.Client(http_options=http, **options)
        return client, identity, studio


def _generate_content(client, model: str, prompt: str, config, errors):
    try:
        return client.models.generate_content(model=model, contents=prompt, config=config)
    except (errors.APIError, TypeError, ValueError) as exc:
        # Older Gemini models and SDKs can reject a thinking configuration.
        # Preserve the original request settings and remove only that option.
        if (getattr(config, "thinking_config", None) is None
                or (isinstance(exc, errors.APIError) and exc.code != 400)
                or "thinking" not in str(exc).lower()):
            raise
        return client.models.generate_content(
            model=model, contents=prompt, config=config.model_copy(update={"thinking_config": None}))


def _generate(client, identity: tuple, studio: bool, model: str, prompt: str, config, errors):
    if not studio or model != _STUDIO_MODELS[0]:
        return _generate_content(client, model, prompt, config, errors)
    with _client_lock:
        route = _model_routes.get(identity)
        if route is None:
            route = _model_routes[identity] = _ModelRoute()
    if not route.confirmed:
        with route.lock:
            while not route.confirmed and route.candidate < len(_STUDIO_MODELS):
                try:
                    response = _generate_content(client, _STUDIO_MODELS[route.candidate], prompt, config, errors)
                except errors.APIError as exc:
                    if exc.code != 404:
                        raise
                    route.candidate += 1
                else:
                    route.confirmed = True
                    return response
    if not route.confirmed:
        raise _PermanentError(f"HTTP 404 from Gemini API: no model in the fallback list for {model!r} is available.")
    return _generate_content(client, _STUDIO_MODELS[route.candidate], prompt, config, errors)


def _retry_after(error) -> float:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    value = headers.get("Retry-After") if headers is not None else None
    if not value:
        return 0
    try:
        try:
            delay = float(value)
        except ValueError:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        return max(0, delay) if math.isfinite(delay) else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def _call_gemini_once(prompt: str, system_instruction: str, api_key: str, model: str,
                      transport_timeout: float, *, thinking_budget: int | None,
                      max_output_tokens: int | None, timeout_seconds: float | None) -> str:
    try:
        from google import genai
        from google.genai import errors, types
    except ImportError as exc:
        raise _PermanentError("google-genai is not installed. Run: pip install google-genai") from exc

    try:
        client, identity, studio = _client_for(genai, types, api_key, transport_timeout)
        options = {"system_instruction": system_instruction, "response_mime_type": "application/json", "temperature": 0.0}
        if max_output_tokens is not None:
            options["max_output_tokens"] = max_output_tokens
        if timeout_seconds is not None:
            options["http_options"] = types.HttpOptions(timeout=round(timeout_seconds * 1000))
        config = types.GenerateContentConfig(**options)
        if thinking_budget is not None:
            try:
                thinking = types.ThinkingConfig(thinking_budget=thinking_budget)
                config = types.GenerateContentConfig(**options, thinking_config=thinking)
            except (AttributeError, TypeError, ValueError):
                logger.debug("Gemini SDK did not accept thinking_config")
        response = _generate(client, identity, studio, model, prompt, config, errors)
        text = response.text
        if text is None:
            raise _TransientError("Gemini returned an empty response body.")
        return text
    except (_PermanentError, _TransientError):
        raise
    except errors.APIError as exc:
        if exc.code in _PERMANENT_HTTP_STATUS_CODES:
            raise _PermanentError(
                f"HTTP {exc.code} from Gemini API (model={model!r}): {exc.message}. "
                f"Check GEMINI_API_KEY and GEMINI_MODEL in your .env -- this will not resolve on retry."
            ) from exc
        raise _TransientError(str(exc), _retry_after(exc) if exc.code == 429 else 0) from exc
    except Exception as exc:  # SDK transport failures have the same retry surface.
        raise _TransientError(str(exc)) from exc


def _parse_json_response(text: str) -> dict | list:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    return json.loads(cleaned)
