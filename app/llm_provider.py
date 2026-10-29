"""
llm_provider.py

Single source of truth for WHICH Google backend the agent talks to, shared by
the direct client (app/llm_client.py) and the ADK agent (app/adk_agent.py) so
the two can never disagree.

Providers (``LLM_PROVIDER``):

* ``gemini_api`` (default) -- Gemini Developer API / AI Studio, authenticated
  with ``GEMINI_API_KEY``. ``vertexai=False`` is passed explicitly so a stray
  ``GOOGLE_GENAI_USE_VERTEXAI`` env var cannot silently flip the backend.
* ``vertex`` -- Vertex AI, only when explicitly selected. Two auth modes:
  - project/location with Application Default Credentials
    (``VERTEX_PROJECT`` + ``VERTEX_LOCATION``), or
  - Vertex express mode with a *Vertex* API key (``VERTEX_API_KEY``).
  A ``GEMINI_API_KEY`` is NEVER sent to Vertex AI, and no project is ever
  assumed: ``VERTEX_PROJECT`` has no default.

Nothing here logs or returns key material in messages.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

PROVIDER_GEMINI_API = "gemini_api"
PROVIDER_VERTEX = "vertex"
VALID_PROVIDERS = (PROVIDER_GEMINI_API, PROVIDER_VERTEX)

# Google Cloud API keys -- including Vertex AI express-mode keys -- commonly
# carry this prefix. It is NOT exclusive to Vertex: Google also issues Gemini
# Developer API (AI Studio) keys with this prefix, and those keys work
# against the Gemini Developer API. The prefix is therefore a *hint* used to
# sharpen error messages, never a criterion for rejecting a key: rejecting on
# it breaks valid Gemini configurations with a misleading "wrong provider"
# error. Which backend is used is decided solely by the explicit
# ``LLM_PROVIDER`` setting.
_CLOUD_KEY_PREFIX = "AQ."


class LLMProviderConfigError(ValueError):
    """The provider configuration is missing, inconsistent, or unsafe."""


def normalize_provider(value: str | None) -> str:
    provider = (value or PROVIDER_GEMINI_API).strip().lower().replace("-", "_")
    if provider in ("gemini", "ai_studio", "aistudio", "gemini_developer_api"):
        provider = PROVIDER_GEMINI_API
    if provider in ("vertexai", "vertex_ai"):
        provider = PROVIDER_VERTEX
    if provider not in VALID_PROVIDERS:
        raise LLMProviderConfigError(
            f"Unknown LLM_PROVIDER {value!r}; expected one of {', '.join(VALID_PROVIDERS)}."
        )
    return provider


def build_client_kwargs(settings: Any, api_key_override: str | None = None) -> dict[str, Any]:
    """Return ``google.genai.Client`` kwargs for the configured provider.

    ``api_key_override`` only applies to the ``gemini_api`` provider (kept so
    ``call_llm_json(api_key=...)`` retains its meaning).
    """
    provider = normalize_provider(getattr(settings, "llm_provider", None))

    if provider == PROVIDER_GEMINI_API:
        key = api_key_override or getattr(settings, "gemini_api_key", None)
        if not key:
            raise LLMProviderConfigError(
                "LLM_PROVIDER=gemini_api requires GEMINI_API_KEY."
            )
        if key.startswith(_CLOUD_KEY_PREFIX):
            # Not an error: this prefix does not imply the key is a Vertex
            # key (Google issues Gemini Developer API keys with it too).
            # Warn instead, so a genuine provider mismatch still surfaces
            # with an actionable message instead of a silent surprise.
            logger.warning(
                "GEMINI_API_KEY carries the %r prefix commonly seen on Google Cloud / "
                "Vertex express-mode keys. The provider is explicitly 'gemini_api', so "
                "this key is sent to the Gemini Developer API. If the API rejects it "
                "(HTTP 400/401/403), set LLM_PROVIDER=vertex and move the key to "
                "VERTEX_API_KEY.",
                _CLOUD_KEY_PREFIX,
            )
        return {"vertexai": False, "api_key": key}

    # --- Vertex AI (explicit opt-in) ---
    project = getattr(settings, "vertex_project", None)
    location = getattr(settings, "vertex_location", None)
    vertex_key = getattr(settings, "vertex_api_key", None)

    if vertex_key and project:
        raise LLMProviderConfigError(
            "Set either VERTEX_API_KEY (express mode) or VERTEX_PROJECT (ADC), not both."
        )
    if vertex_key:
        return {"vertexai": True, "api_key": vertex_key}
    if project:
        return {"vertexai": True, "project": project, "location": location or "global"}
    raise LLMProviderConfigError(
        "LLM_PROVIDER=vertex requires VERTEX_PROJECT (with Application Default "
        "Credentials) or VERTEX_API_KEY. No project is assumed."
    )


def describe_provider(settings: Any) -> str:
    """Secret-free one-line description for logs."""
    provider = normalize_provider(getattr(settings, "llm_provider", None))
    if provider == PROVIDER_GEMINI_API:
        return "Gemini API (AI Studio key)"
    if getattr(settings, "vertex_api_key", None):
        return "Vertex AI (express mode, API key)"
    return f"Vertex AI (project={getattr(settings, 'vertex_project', None)}, " \
           f"location={getattr(settings, 'vertex_location', None) or 'global'})"
