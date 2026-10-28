"""
tests/test_review_catalog_script.py

scripts/review_catalog.py end to end against a tmp catalog/skills/reviews and
a mocked Prometheus: listing, approval, rejection, clear, idempotence,
refusals, offline operations and failure handling.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.catalog import reconciler as rc
from app.catalog import reviews as rv
from app.catalog.schema import Catalog, CatalogEntry, Priority
from app.config import Settings
from scripts import review_catalog as cli

REF = "references/node-exporter/cpu.md"


@pytest.fixture
def env(tmp_path):
    skills = tmp_path / "skills"
    (skills / "references" / "node-exporter").mkdir(parents=True)
    (skills / "references" / "node-exporter" / "overview.md").write_text("o")
    (skills / "references" / "node-exporter" / "cpu.md").write_text("c")
    cat = Catalog("1.0", "t", (CatalogEntry(
        name="node_load1", type="gauge", category="CPU", priority=Priority.REVIEW.value,
        exporter="node-exporter", status="approved", help="load", reference_path=REF),))
    (tmp_path / "catalog.json").write_text(json.dumps(cat.to_dict(), indent=2) + "\n")
    settings = Settings(
        gemini_api_key="x", catalog_path=tmp_path / "catalog.json",
        catalog_reviews_path=tmp_path / "reviews.json", skills_root=skills,
        prometheus_url="http://prom.test:9090", catalog_discovery_exclude_patterns=["go_*"],
    )
    return tmp_path, settings


def _resp(data):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = {"status": "success", "data": data}
    return r


def _session(names=("node_load1", "acme_new", "acme_untyped", "go_goroutines"), metadata=None, fail=None):
    metadata = metadata if metadata is not None else {
        "acme_new": [{"type": "gauge", "help": "A new gauge"}],
        "go_goroutines": [{"type": "gauge", "help": "g"}],
    }
    def _get(url, timeout=None):
        if fail:
            raise fail
        return _resp(list(names) if url.endswith("/api/v1/label/__name__/values") else metadata)
    s = MagicMock()
    s.get.side_effect = _get
    return s


def _cli(env, argv, session=None):
    _, settings = env
    out: list[str] = []
    with patch.object(rc, "_get_session", return_value=session or _session()):
        code = cli.run(argv, settings, emit=out.append)
    return code, "\n".join(out)


def _approve_args(name="acme_new", **kw):
    args = ["approve", name, "--category", kw.get("category", "Queues"),
            "--exporter", kw.get("exporter", "node-exporter"),
            "--reference-path", kw.get("ref", REF)]
    return args + (["--replace"] if kw.get("replace") else [])


def _reviews(env):
    p = env[0] / "reviews.json"
    return json.loads(p.read_text()) if p.exists() else None


# ---- list -------------------------------------------------------------------


def test_list_pending_shows_discoveries_with_prometheus_type_and_skips_out_of_scope_and_untyped(env):
    code, text = _cli(env, ["list"])
    assert code == cli.EXIT_OK
    assert "Pending review: 1" in text and "acme_new (gauge) A new gauge" in text
    assert "go_goroutines" not in text.split("(not listed")[0]
    assert "1 out of scope, 1 with no usable type" in text


def test_list_approved_and_rejected_need_no_prometheus(env):
    code, text = _cli(env, ["list", "--status", "all"], session=_session(fail=requests.exceptions.ConnectionError()))
    assert code == cli.EXIT_DISCOVERY_FAILED and "DISCOVERY FAILED" in text
    code, text = _cli(env, ["list", "--status", "approved"], session=_session(fail=requests.exceptions.ConnectionError()))
    assert code == cli.EXIT_OK and "Approved by review: 0" in text


# ---- approve ------------------------------------------------------------------


def test_approve_persists_prometheus_type_plus_human_metadata_and_never_touches_catalog_json(env):
    before = (env[0] / "catalog.json").read_bytes()
    code, text = _cli(env, _approve_args())
    assert code == cli.EXIT_OK and "added" in text
    rec = _reviews(env)["decisions"][0]
    assert rec["name"] == "acme_new" and rec["decision"] == "approved"
    assert (rec["type"], rec["help"]) == ("gauge", "A new gauge")
    assert (rec["category"], rec["exporter"], rec["reference_path"]) == ("Queues", "node-exporter", REF)
    assert (env[0] / "catalog.json").read_bytes() == before
    # It is no longer pending, and is listed as approved.
    assert "Pending review: 0" in _cli(env, ["list"])[1]
    assert "acme_new" in _cli(env, ["list", "--status", "approved"])[1]


def test_repeating_an_approval_changes_nothing(env):
    _cli(env, _approve_args())
    first = (env[0] / "reviews.json").read_bytes()
    code, text = _cli(env, _approve_args())
    assert code == cli.EXIT_OK and "unchanged" in text
    assert (env[0] / "reviews.json").read_bytes() == first


def test_approve_requires_the_metadata_flags(env):
    with pytest.raises(SystemExit) as e:
        _cli(env, ["approve", "acme_new", "--category", "Q"])
    assert e.value.code == 2
    assert _reviews(env) is None


@pytest.mark.parametrize("kw,why", [
    ({"ref": "references/node-exporter/nope.md"}, "does not exist"),
    ({"exporter": "dcgm-exporter"}, "does not match"),
    ({"category": "uncategorized"}, "placeholder"),
])
def test_approve_refuses_invalid_metadata_and_writes_nothing(env, kw, why):
    code, text = _cli(env, _approve_args(**kw))
    assert code == cli.EXIT_REFUSED and "REFUSED" in text and why in text
    assert _reviews(env) is None


@pytest.mark.parametrize("name,why", [
    ("acme_untyped", "no usable single type"),
    ("go_goroutines", "discovery scope"),
    ("acme_missing", "not currently exposed"),
])
def test_approve_explains_why_a_metric_is_not_reviewable(env, name, why):
    code, text = _cli(env, _approve_args(name=name))
    assert code == cli.EXIT_REFUSED and why in text
    assert _reviews(env) is None


def test_approving_a_vendor_metric_is_refused_without_contacting_prometheus(env):
    session = _session(fail=requests.exceptions.ConnectionError())
    code, text = _cli(env, _approve_args(name="node_load1"), session=session)
    assert code == cli.EXIT_REFUSED and "vendor catalog" in text
    session.get.assert_not_called()


def test_approve_with_discovery_failure_exits_1_and_writes_nothing(env):
    code, text = _cli(env, _approve_args(), session=_session(fail=requests.exceptions.Timeout()))
    assert code == cli.EXIT_DISCOVERY_FAILED and _reviews(env) is None


# ---- reject / replace / clear ----------------------------------------------------


def test_rejection_persists_and_stays_in_force_across_repeated_listing(env):
    code, text = _cli(env, ["reject", "acme_new"])
    assert code == cli.EXIT_OK and "added" in text
    assert _reviews(env)["decisions"][0]["decision"] == "rejected"
    for _ in range(2):  # repeated reconciliation: still not pending
        assert "Pending review: 0" in _cli(env, ["list"])[1]
    assert "acme_new" in _cli(env, ["list", "--status", "rejected"])[1]


def test_rejecting_a_vendor_metric_works_offline(env):
    code, _ = _cli(env, ["reject", "node_load1"], session=_session(fail=requests.exceptions.ConnectionError()))
    assert code == cli.EXIT_OK
    assert _reviews(env)["decisions"][0] == {
        "name": "node_load1", "decision": "rejected", "decided_at": _reviews(env)["decisions"][0]["decided_at"]}


def test_rejecting_an_unknown_metric_is_refused_so_typos_leave_no_record(env):
    code, text = _cli(env, ["reject", "acme_typo"])
    assert code == cli.EXIT_REFUSED and _reviews(env) is None


def test_flipping_a_rejection_to_an_approval_needs_replace(env):
    _cli(env, ["reject", "acme_new"])
    code, text = _cli(env, _approve_args())
    assert code == cli.EXIT_REFUSED and "--replace" in text
    assert _reviews(env)["decisions"][0]["decision"] == "rejected"
    code, text = _cli(env, _approve_args(replace=True))
    assert code == cli.EXIT_OK and "replaced" in text
    assert _reviews(env)["decisions"][0]["decision"] == "approved"


def test_clear_lifts_a_rejection_and_the_metric_is_pending_again(env):
    _cli(env, ["reject", "acme_new"])
    code, text = _cli(env, ["clear", "acme_new"])
    assert code == cli.EXIT_OK and "removed the rejected decision" in text
    assert "acme_new" in _cli(env, ["list"])[1]
    assert _cli(env, ["clear", "acme_new"])[0] == cli.EXIT_REFUSED


# ---- store safety --------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["list", "--status", "approved"], ["reject", "node_load1"], ["clear", "node_load1"]])
def test_malformed_store_refuses_every_command_and_is_never_overwritten(env, argv):
    (env[0] / "reviews.json").write_text("{broken")
    code, text = _cli(env, argv)
    assert code == cli.EXIT_REFUSED and "not valid JSON" in text
    assert (env[0] / "reviews.json").read_text() == "{broken"


def test_a_failed_write_is_reported_and_leaves_the_store_intact(env):
    _cli(env, ["reject", "acme_new"])
    before = (env[0] / "reviews.json").read_bytes()
    with patch("app.catalog.reviews.os.replace", side_effect=OSError("read-only filesystem")):
        code, text = _cli(env, ["reject", "node_load1"])
    assert code == cli.EXIT_REFUSED and "read-only filesystem" in text
    assert (env[0] / "reviews.json").read_bytes() == before
