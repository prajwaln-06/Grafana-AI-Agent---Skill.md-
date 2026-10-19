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

from typing import Any

PROVIDER_GEMINI_API = "gemini_api"
PROVIDER_VERTEX = "vertex"
VALID_PROVIDERS = (PROVIDER_GEMINI_API, PROVIDER_VERTEX)

# Vertex AI express-mode keys start with this prefix; an AI Studio key never does.
_VERTEX_KEY_PREFIX = "AQ."


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
        if key.startswith(_VERTEX_KEY_PREFIX):
            raise LLMProviderConfigError(
                "GEMINI_API_KEY looks like a Vertex AI key (prefix 'AQ.'). It will not be "
                "sent to the Gemini API. If you intend to use Vertex AI, set "
                "LLM_PROVIDER=vertex and put the key in VERTEX_API_KEY."
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
