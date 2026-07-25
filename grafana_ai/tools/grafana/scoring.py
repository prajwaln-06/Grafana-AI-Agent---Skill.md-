import re
from .utils import logger
from .discovery import ensure_panel_index

_CONFIDENCE_THRESHOLD = 3
_AMBIGUITY_RATIO = 0.85

_STOP_WORDS = {
    "show", "get", "current", "usage", "the", "of", "a", "an",
    "metrics", "metric", "dashboard", "panel", "display", "find", "me",
    "what", "is",
}

_DOMAIN_ALIASES = [
    {"kubernetes", "k8s", "kube"},
    {"postgres", "postgresql"},
    {"mongodb", "mongo"},
    {"opensearch"},
    {"elasticsearch"},
    {"gpu", "nvidia"},
    {"docker"}, {"redis"}, {"mysql"}, {"nginx"}, {"cadvisor"},
    {"loki"}, {"tempo"}, {"promtail"}
]

_QUALIFIER_MAP: dict[str, set[str]] = {}
for alias_set in _DOMAIN_ALIASES:
    for alias in alias_set:
        _QUALIFIER_MAP[alias] = alias_set


def score_entry(tokens: list[str], full_keyword: str, entry: dict) -> int:
    """Score a panel entry based on natural language tokens."""
    panel = entry["panel_title"].lower()
    dash = entry["dashboard_title"].lower()
    desc = entry.get("panel_description", "").lower()
    query = entry["query"].lower()
    tags = " ".join(t.lower() for t in entry.get("dashboard_tags", []))

    query_tokens = set(re.findall(r'[A-Za-z_][A-Za-z0-9_]*', query))

    score = 0
    if full_keyword in panel:
        score += 10
        
    for tok in tokens:
        if len(tok) < 2 or tok in _STOP_WORDS:
            continue
            
        aliases_to_check = _QUALIFIER_MAP.get(tok, {tok})
        
        best_tok_score = 0
        matched = False
        
        for alias in aliases_to_check:
            alias_score = 0
            if alias in panel:
                alias_score += 4
            if alias in desc:
                alias_score += 3
            if alias in tags:
                alias_score += 2
            if any(alias in qt for qt in query_tokens):
                alias_score += 1
            if alias in dash:
                alias_score += 1
                
            if alias_score > 0:
                matched = True
                best_tok_score = max(best_tok_score, alias_score)
                
        score += best_tok_score
            
        if not matched and tok in _QUALIFIER_MAP:
            return 0

    if not panel.strip():
        score = max(0, score - 5)
    return score


async def score_and_select(keyword: str) -> dict | str | None:
    """Find the best-matching panel entry for a natural-language keyword."""
    index = await ensure_panel_index()
    if not index:
        return None

    kw_lower = keyword.lower().strip()
    tokens = kw_lower.replace("_", " ").split()

    scored: list[tuple[int, dict]] = []
    for entry in index:
        s = score_entry(tokens, kw_lower, entry)
        if s >= _CONFIDENCE_THRESHOLD:
            scored.append((s, entry))

    if not scored:
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best = scored[0]

    if len(scored) >= 2:
        second_score, second = scored[1]
        different_dash = best["dashboard_uid"] != second["dashboard_uid"]
        if different_dash and second_score / best_score >= _AMBIGUITY_RATIO:
            seen_uids: set[str] = set()
            candidates: list[str] = []
            for _, e in scored[:5]:
                if e["dashboard_uid"] not in seen_uids:
                    seen_uids.add(e["dashboard_uid"])
                    candidates.append(
                        f"- '{e['panel_title']}' in dashboard '{e['dashboard_title']}'"
                    )
            logger.info(f"Discovery: ambiguous match for '{keyword}', {len(candidates)} candidates")
            return (
                f"Multiple panels match '{keyword}':\n"
                + "\n".join(candidates)
                + "\n\nPlease be more specific about which metric you want."
            )

    logger.info(
        f"Discovery: '{keyword}' → panel '{best['panel_title']}' "
        f"in '{best['dashboard_title']}' (score={best_score}) → {best['query'][:100]}"
    )
    return best
