"""Tests for the server-side retention of the ID token.

The ID token is needed for the RP-initiated logout (``id_token_hint``) and
must never reach the browser: the session cookie only carries the handle the
token is stored under.
"""
from __future__ import annotations

import time

from app.auth.id_token_store import IdTokenStore, get_id_token_store


def test_a_token_is_stored_under_an_opaque_handle():
    store = IdTokenStore(ttl_seconds=60)
    handle = store.put("a.b.c", issuer="http://issuer", subject="user-1")

    assert handle
    assert handle not in ("a.b.c", "user-1")
    assert store.get(handle) == "a.b.c"


def test_an_unknown_handle_has_no_token():
    store = IdTokenStore(ttl_seconds=60)
    assert store.get("nothing-here") is None
    assert store.get(None) is None
    assert store.get("") is None


def test_a_token_is_forgotten_when_it_is_deleted():
    store = IdTokenStore(ttl_seconds=60)
    handle = store.put("a.b.c")
    store.delete(handle)
    assert store.get(handle) is None


def test_an_expired_token_is_not_returned():
    store = IdTokenStore(ttl_seconds=0)
    handle = store.put("a.b.c")
    time.sleep(0.01)
    assert store.get(handle) is None


def test_the_store_is_shared_by_the_process():
    assert get_id_token_store() is get_id_token_store()
