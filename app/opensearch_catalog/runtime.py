"""
runtime.py

Process-level wiring for the OpenSearch registry, the field-catalog cache and
the RESULT cache (query_cache.py). Built lazily from Settings and reused across
requests so the TTL caches actually cache. `reset()` is for tests; the admin
reload (`POST /api/v1/admin/reload-skill`) uses `reload_registry_status()`,
which drops the cached registry so an edited index_registry.json is picked up
without a restart, and empties the result cache (stored answers are also
fingerprint-checked on every reuse, so this is belt and braces, not the only
protection).

Field cache vs result cache: the field cache remembers which fields EXIST on the
cluster; the result cache remembers completed ANSWERS. The agent talks to the
result cache only through lookup_cached_result() / store_executed_result(), which
never raise: any failure is a cache miss / not stored, never a failed request.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from app.opensearch_catalog import query_cache
from app.opensearch_catalog.context import Limits, OpenSearchContext, build_context, error_context
from app.opensearch_catalog.field_catalog import FieldCatalogCache, make_mapping_fetcher
from app.opensearch_catalog.registry import (
    DEFAULT_REGISTRY_PATH, IndexRegistry, RegistryError, load_registry,
)

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_registries: dict[tuple, IndexRegistry] = {}
_caches: dict[tuple, FieldCatalogCache] = {}
_result_caches: dict[tuple, query_cache.ResultCache] = {}


def reset() -> None:
    with _lock:
        _registries.clear()
        _caches.clear()
        _result_caches.clear()


def reset_registry() -> None:
    """Drops only the cached registry (field-catalog caches are kept), so an edited
    index_registry.json takes effect on the next load. Used by the admin reload."""
    with _lock:
        _registries.clear()


def reload_registry_status(settings) -> dict:
    """Admin reload: drop the cached registry and load it again now, so a broken
    edit is reported to the operator immediately. Never raises. On failure nothing
    is cached and every OpenSearch request keeps failing closed until it is fixed."""
    reset_registry()
    reset_result_cache()
    try:
        reg = get_registry(settings)
    except RegistryError as e:
        return {"loaded": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 -- report, never raise from an admin endpoint
        logger.exception("Unexpected failure while reloading the OpenSearch registry")
        return {"loaded": False, "error": f"unexpected error: {type(e).__name__}"}
    return {"loaded": True, "patterns": sorted(p.pattern_id for p in reg.enabled_patterns)}


def registry_path(settings) -> Path:
    return Path(settings.opensearch_registry_path) if settings.opensearch_registry_path else DEFAULT_REGISTRY_PATH


def get_registry(settings) -> IndexRegistry:
    """Raises RegistryError. Cached per (path, skills_root); a failed load is
    NOT cached, so fixing the file takes effect on the next request."""
    path = registry_path(settings)
    key = (str(path), str(settings.skills_root))
    with _lock:
        cached = _registries.get(key)
    if cached is not None:
        return cached
    reg = load_registry(path, skills_root=Path(settings.skills_root))
    with _lock:
        _registries[key] = reg
    return reg


def get_field_cache(settings) -> FieldCatalogCache:
    key = (settings.opensearch_url, settings.opensearch_field_catalog_ttl_seconds,
           settings.opensearch_field_catalog_stale_ttl_seconds,
           settings.opensearch_field_catalog_max_indices,
           settings.opensearch_field_catalog_failure_backoff_seconds,
           settings.opensearch_auth_username)
    with _lock:
        cache = _caches.get(key)
        if cache is None:
            cache = FieldCatalogCache(
                make_mapping_fetcher(settings.opensearch_url, auth=settings.opensearch_auth,
                                     timeout=settings.opensearch_timeout_seconds),
                ttl_seconds=settings.opensearch_field_catalog_ttl_seconds,
                stale_ttl_seconds=settings.opensearch_field_catalog_stale_ttl_seconds,
                max_indices=settings.opensearch_field_catalog_max_indices,
                failure_backoff_seconds=settings.opensearch_field_catalog_failure_backoff_seconds,
            )
            _caches[key] = cache
        return cache


def field_cache_snapshots() -> dict[str, dict]:
    """Status of every field-catalog cache built so far in this process (normally
    one). Never fetches; used by the read-only status endpoint."""
    with _lock:
        caches = list(_caches.values())
    merged: dict[str, dict] = {}
    for cache in caches:
        merged.update(cache.snapshot())
    return merged


def build_request_context(matched: list[dict], settings) -> OpenSearchContext:
    """The ONLY entry the pipeline uses. Never raises: any failure becomes an
    explicit unusable context (fail closed)."""
    limits = Limits.from_settings(settings)
    try:
        registry = get_registry(settings)
    except RegistryError as e:
        logger.error("OpenSearch registry could not be loaded: %s", e)
        return error_context("the OpenSearch index registry could not be loaded", limits)
    except Exception:  # noqa: BLE001 -- never raise into the pipeline; fail closed
        logger.exception("Unexpected failure while loading the OpenSearch registry")
        return error_context("the OpenSearch index registry could not be loaded", limits)
    try:
        return build_context(matched, registry, get_field_cache(settings), limits)
    except Exception:  # noqa: BLE001 -- a context bug must not become a guessed query
        logger.exception("Building the OpenSearch request context failed")
        return error_context("the OpenSearch context could not be built", limits)


# ---- result cache (completed answers; see query_cache.py) ------------------------------------------------


def get_result_cache(settings) -> query_cache.ResultCache | None:
    """The one process-wide result cache for these settings, or None when disabled."""
    if not settings.opensearch_result_cache_enabled:
        return None
    key = (settings.opensearch_result_cache_ttl_seconds, settings.opensearch_result_cache_max_entries,
           settings.opensearch_result_cache_semantic_enabled,
           json.dumps(settings.opensearch_result_cache_aliases, sort_keys=True))
    with _lock:
        cache = _result_caches.get(key)
        if cache is None:
            cache = query_cache.ResultCache(
                ttl_seconds=settings.opensearch_result_cache_ttl_seconds,
                max_entries=settings.opensearch_result_cache_max_entries,
                semantic_enabled=settings.opensearch_result_cache_semantic_enabled,
                aliases=settings.opensearch_result_cache_aliases)
            _result_caches[key] = cache
        return cache


def reset_result_cache() -> None:
    """Empties every result cache (keeps the instances). Used by the admin reload and by tests."""
    with _lock:
        caches = list(_result_caches.values())
    for cache in caches:
        cache.clear()


def result_cache_snapshot(settings) -> dict:
    """Counts only (never a question, result or scope). Used by the read-only status endpoint."""
    cache = get_result_cache(settings)
    return {"enabled": False} if cache is None else cache.snapshot()


def lookup_cached_result(question: str, scope: str, settings, skill_index) -> dict | None:
    """A completed result for an identical or equivalent question, or None. Never raises. A hit has
    already been re-proved against the current registry, live field view, configuration and skill
    text, and the stored canonical query re-run through the real guard."""
    cache = get_result_cache(settings)
    if cache is None:
        return None
    try:
        registry = get_registry(settings)
        field_cache = get_field_cache(settings)
    except Exception:  # noqa: BLE001 -- a registry problem is a miss; the pipeline then fails closed itself
        return None

    def revalidate(entry):
        return query_cache.revalidate_entry(entry, settings=settings, registry=registry,
                                            field_cache=field_cache, skill_index=skill_index)

    hit = cache.lookup(question, scope, revalidate)
    return hit.result if hit is not None else None


def store_executed_result(question: str, scope: str, executed: dict, settings, skill_index) -> str:
    """Stores `executed` if (and only if) it is complete, successful and safe. Returns "" when
    stored, otherwise the reason. Never raises."""
    cache = get_result_cache(settings)
    if cache is None:
        return "result cache disabled"
    try:
        registry = get_registry(settings)
        assessment = query_cache.assess_executed(question, executed, registry=registry, aliases=cache.aliases)
        if not assessment.cacheable:
            cache.put(question, scope, executed, assessment, None)       # counted, not stored
            return assessment.reason
        prints, why = query_cache.compute_fingerprints(
            [p.pattern_id for p in assessment.parts], registry=registry, field_cache=get_field_cache(settings),
            settings=settings, skill_index=skill_index)
        if prints is None:
            return why
        stored, reason = cache.put(question, scope, executed, assessment, prints)
        return "" if stored else reason
    except Exception:  # noqa: BLE001 -- never fail a request because of the cache
        logger.exception("storing the OpenSearch result in the result cache failed")
        return "internal error"
