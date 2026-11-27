"""Request-local wall-clock timings for OpenSearch, including worker threads."""
from __future__ import annotations

import inspect
import json
import logging
from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from functools import wraps
from threading import Lock
from time import perf_counter

logger = logging.getLogger(__name__)

STAGES = (
    "router_ms", "context_ms", "generator_ms", "validate_ms", "guard_ms",
    "search_client_ms", "opensearch_took_ms", "normalise_ms", "cache_store_ms",
)


class RequestTimings:
    def __init__(self):
        self.start = perf_counter()
        self.values: dict[str, float] = {}
        self.is_opensearch = False
        self._lock = Lock()

    def add(self, name: str, milliseconds: float) -> None:
        with self._lock:
            self.values[name] = self.values.get(name, 0) + milliseconds

    def snapshot(self) -> dict:
        with self._lock:
            values = {name: round(self.values[name], 3) if name in self.values else None for name in STAGES}
        return {**values, "total_ms": round((perf_counter() - self.start) * 1_000, 3)}


_current: ContextVar[RequestTimings | None] = ContextVar("opensearch_request_timings", default=None)
_active: ContextVar[frozenset[str]] = ContextVar("opensearch_active_stages", default=frozenset())


@contextmanager
def bind(request: RequestTimings):
    token = _current.set(request)
    try:
        yield
    finally:
        _current.reset(token)


def track_request(function):
    @wraps(function)
    async def tracked(*args, **kwargs):
        request = RequestTimings()
        async with aclosing(function(*args, **kwargs)) as events:
            while True:
                # Release the binding before yielding to the caller's context.
                with bind(request):
                    event = await anext(events, None)
                if event is None:
                    return
                yield event
    return tracked


@contextmanager
def stage(name: str):
    request = _current.get()
    active = _active.get()
    if request is None or name in active:
        yield
        return
    token = _active.set(active | {name})
    started = perf_counter()
    try:
        yield
    finally:
        request.add(name, (perf_counter() - started) * 1_000)
        _active.reset(token)


def measure(name: str):
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def async_measured(*args, **kwargs):
                with stage(name):
                    return await function(*args, **kwargs)
            return async_measured

        @wraps(function)
        def measured(*args, **kwargs):
            with stage(name):
                return function(*args, **kwargs)
        return measured
    return decorate


def mark_opensearch() -> None:
    if (request := _current.get()) is not None:
        request.is_opensearch = True


def record_took(body: dict | None) -> None:
    took = body.get("took") if isinstance(body, dict) else None
    if type(took) is int and took >= 0 and (request := _current.get()) is not None:
        request.add("opensearch_took_ms", took)


def finish(payload: dict, invocation_id: str) -> None:
    """Attach only this request's timings, including when an answer was cached."""
    request = _current.get()
    if request is None:
        return
    result = payload.get("result", {})
    entries = result.get("results") if isinstance(result, dict) else None
    if not isinstance(entries, list):
        entries = [result]
    blocks = [entry for entry in entries if isinstance(entry, dict)
              and str(entry.get("data_source", "")).strip().lower() == "opensearch"]
    if not blocks and not request.is_opensearch:
        return
    values = request.snapshot()
    for entry in blocks:
        execution = entry.get("execution")
        if isinstance(execution, dict):
            if not isinstance(execution.get("metadata"), dict):
                execution["metadata"] = {}
            execution["metadata"]["timings"] = values.copy()
    logger.info("opensearch_request %s", json.dumps({"invocation_id": invocation_id, "timings": values}))
