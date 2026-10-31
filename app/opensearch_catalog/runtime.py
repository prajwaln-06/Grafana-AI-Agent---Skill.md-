"""
runtime.py

Process-level wiring for the OpenSearch registry and field-catalog cache.
Built lazily from Settings and reused across requests so the TTL cache
actually caches. `reset()` is for tests and admin reloads.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from app.opensearch_catalog.context import Limits, OpenSearchContext, build_context, error_context
from app.opensearch_catalog.field_catalog import FieldCatalogCache, make_mapping_fetcher
from app.opensearch_catalog.registry import (
    DEFAULT_REGISTRY_PATH, IndexRegistry, RegistryError, load_registry,
)

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_registries: dict[tuple, IndexRegistry] = {}
_caches: dict[tuple, FieldCatalogCache] = {}


def reset() -> None:
    with _lock:
        _registries.clear()
        _caches.clear()


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
