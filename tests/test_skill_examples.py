"""Every JSON example in the OpenSearch skill files (and in docs/SKILL_AUTHOR_CONTRACT.md) is a valid query.

Each ```json block in skills/references/opensearch-logs/*.md, and each concrete OpenSearch example in
skills/SKILL.md, is parsed and run through the friend's guard (guard.check_entry) against the SYNTHETIC
fixture mappings. A block is either a bare DSL body (checked against the file's own pattern_id) or a
contract fragment carrying `pattern_id` (and possibly `intent` / `analysis`). If an example in a skill
file stops being accepted -- a renamed field, a changed rule, a typo -- this test fails, so the model is
never shown an example the system would refuse."""
import json
import re
from pathlib import Path

import pytest

from app.opensearch_catalog import guard

SKILLS = Path(__file__).resolve().parent.parent / "skills"
FAMILY_FILES = {
    "fabric_health": "references/opensearch-logs/fabric-health.md",
    "controller_log": "references/opensearch-logs/controller-log.md",
    "ilo_events": "references/opensearch-logs/ilo-events.md",
}
_BLOCK_RE = re.compile(r"```json\s*\n(.*?)```", re.S)


def _blocks(rel: str) -> list[str]:
    return _BLOCK_RE.findall((SKILLS / rel).read_text(encoding="utf-8"))


def _examples():
    out = []
    for pid, rel in FAMILY_FILES.items():
        for i, block in enumerate(_blocks(rel)):
            out.append(pytest.param(rel, pid, block, id=f"{pid}-{i}"))
    for i, block in enumerate(_blocks("references/opensearch-logs/overview.md")):
        out.append(pytest.param("references/opensearch-logs/overview.md", None, block, id=f"overview-{i}"))
    for i, block in enumerate(_blocks("SKILL.md")):
        # SKILL.md mostly holds shape templates with <placeholders>; only concrete OpenSearch examples run.
        if '"opensearch"' in block and "<" not in block:
            out.append(pytest.param("SKILL.md", None, block, id=f"SKILL.md-{i}"))
    contract = SKILLS.parent / "docs" / "SKILL_AUTHOR_CONTRACT.md"
    if contract.exists():
        for i, block in enumerate(_BLOCK_RE.findall(contract.read_text(encoding="utf-8"))):
            out.append(pytest.param("docs/SKILL_AUTHOR_CONTRACT.md", None, block, id=f"contract-{i}"))
    return out


EXAMPLES = _examples()


def test_examples_were_found_in_every_family_file_and_in_skill_md():
    ids = [p.id for p in EXAMPLES]
    for pid in FAMILY_FILES:
        assert any(i.startswith(pid) for i in ids), pid
    assert any(i.startswith("SKILL.md") for i in ids)
    assert sum(i.startswith("contract") for i in ids) == 3
    assert len(EXAMPLES) >= 12


@pytest.mark.parametrize("rel,file_pid,block", EXAMPLES)
def test_skill_file_example_passes_the_guard(rel, file_pid, block, os_ctx):
    obj = json.loads(block)                                    # must be real JSON, not a sketch
    if "pattern_id" in obj:
        pid = obj["pattern_id"]
        assert file_pid in (None, pid), f"{rel}: an example names {pid!r} inside the {file_pid!r} file"
        entry = {"status": "ok", "data_source": "opensearch", **obj}
    else:
        assert file_pid is not None, f"{rel}: a bare DSL example needs a family file"
        pid, entry = file_pid, {"status": "ok", "data_source": "opensearch", "pattern_id": file_pid, "query": obj}
    assert "index" not in entry, f"{rel}: examples never write an index"
    ctx = os_ctx(pid)
    result = guard.check_entry(entry, ctx)
    assert result.ok, f"{rel}: example rejected ({result.code}): {result.reason}"
    # canonical form is stable: what the system would run passes again unchanged
    again = guard.check_entry({**entry, "query": result.query, "analysis": result.analysis}
                              if result.analysis is not None else {**entry, "query": result.query}, ctx)
    assert again.ok and again.query == result.query and again.analysis == result.analysis
    if "analysis" in obj:
        assert obj.get("intent") in (None, obj["analysis"]["type"])
