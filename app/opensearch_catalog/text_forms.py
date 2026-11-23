"""
text_forms.py

Word-form (tense / plural) tolerance for free-text search on a log family's
text field. Deterministic, dependency-free, no LLM.

Why: `match_phrase` on an analysed text field compares whole TOKENS. The
standard analyser lowercases but does not stem, so the phrase "System
Overheated" can never match a message that says "System Overheating", and the
two wordings of the same question returned different answers (8 rows vs.
empty). The model copies the person's wording, and which tense the BMC firmware
happens to use is not something the person (or the model) can know.

What: `phrase_variants("System Overheated")` returns every phrase obtained by
replacing the varied word(s) with their other forms:

    ["System Overheat", "System Overheated", "System Overheating", "System Overheats"]

and returns the SAME list for "System Overheating", "System Overheat" or
"System Overheats", so equivalent wordings canonicalise to the same query.

Rules (kept deliberately simple and predictable):
  * Only plain alphabetic words of 4+ letters, written lowercase or Titlecase,
    are varied. ALLCAPS (IPMI, RMCP), CamelCase (LinkStatus), anything with a
    digit or punctuation, and a short stop list are left exactly as written.
  * A word's forms are base, base+s, base+ed, base+ing, with the usual English
    spelling rules (drop/restore e, y -> ies/ied, double a one-syllable final
    consonant). The rules over-generate a little: a non-word such as "Downed"
    or "logined" can appear. That is harmless, since a phrase containing a
    non-word simply matches no document. More than 6 forms for one word means
    the rules are unsure, and the word is left exactly as written.
  * At most `cap` phrases are produced. If varying every eligible word exceeds
    that, only the words carrying an inflection (-ed/-ing/-s) are varied, then
    only the last inflected word, then only the last eligible word.
  * `is_variant_group` recognises an already-expanded list (it equals
    `phrase_variants` of one of its members), so the guard accepts its own
    output unchanged: canonicalisation stays idempotent.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from functools import lru_cache
from itertools import product

DEFAULT_MAX_VARIANTS = 8
_MAX_WORD_FORMS = 6
_MIN_WORD_LEN = 4

_VOWELS = "aeiou"
_WORD_RE = re.compile(r"^[A-Za-z]+$")
_SPLIT_RE = re.compile(r"(\s+)")
_SEARCH_SPLIT_RE = re.compile(r"[\s,;!?()\[\]{}\"“”]+")
_SEARCH_STOP_WORDS = frozenset({
    "a", "am", "an", "and", "are", "as", "at", "be", "been", "being", "by", "do", "does",
    "for", "from", "had", "has", "have", "in", "is", "it", "of", "on", "or", "that", "the",
    "these", "this", "those", "to", "was", "were", "which", "who", "with",
})

# Words that look inflected but are not (or whose variants would only add noise).
_STOP = frozenset({
    "this", "thus", "always", "during", "nothing", "something", "anything", "everything",
    "status", "address", "access", "process", "success", "less", "unless", "across",
    "previous", "various", "bios", "news", "series", "species", "within", "without",
    "from", "with", "that", "than", "then", "when", "where", "which", "while", "have",
    "been", "were", "into", "onto", "upon", "over", "under", "after", "before", "about",
    "string", "thing", "things", "morning", "evening", "ceiling",
})


def _has_vowel(s: str) -> bool:
    return any(c in _VOWELS or c == "y" for c in s)


def _syllables(s: str) -> int:
    groups, prev = 0, False
    for i, c in enumerate(s):
        v = c in _VOWELS or (c == "y" and i > 0)
        if v and not prev:
            groups += 1
        prev = v
    return groups


def _cvc(s: str) -> bool:
    """consonant-vowel-consonant ending, final consonant not w/x/y (stop, updat, power)."""
    return (len(s) >= 3 and s[-1] not in _VOWELS + "wxy" and s[-2] in _VOWELS
            and s[-3] not in _VOWELS)


def _bases_after_strip(core: str) -> list[str] | None:
    """Candidate base form(s) once -ed/-ing is removed; None = not really a suffix."""
    if len(core) < 3 or not _has_vowel(core):
        return None
    if core[-1] == core[-2] and core[-1] not in _VOWELS + "lsz":
        return [core[:-1]]                                   # stopp -> stop, flapp -> flap
    if core.endswith(("ll", "ss", "zz")):
        return [core]                                        # install, pass
    if core[-1] in "vcz" or core.endswith(("bl", "cl", "dl", "fl", "gl", "kl", "pl", "tl", "zl",
                                           "dg", "rg", "lg", "ns", "rs", "ps", "ls")):
        return [core + "e"]                                  # disabl -> disable, merg -> merge
    if _cvc(core) or core.endswith(("ng", "us")):
        return [core, core + "e"]                            # power/powere, updat/update
    return [core]                                            # overheat, fail, clear


def _bases(w: str) -> list[str]:
    """Candidate base forms of a lowercase word (the word itself when it has no suffix)."""
    if w in _STOP:
        return [w]
    if len(w) > 4 and w.endswith(("ied", "ies")):
        return [w[:-3] + "y"]
    if w.endswith("eed"):
        return [w]
    if len(w) >= 6 and w.endswith("ing"):
        b = _bases_after_strip(w[:-3])
        if b:
            return b
    if len(w) >= 5 and w.endswith("ed"):
        b = _bases_after_strip(w[:-2])
        if b:
            return b
    if len(w) >= 5 and w.endswith("es"):
        stem = w[:-2]
        return [stem] if stem.endswith(("s", "x", "z", "ch", "sh")) else [w[:-1]]
    if len(w) >= 4 and w.endswith("s") and not w.endswith(("ss", "us", "is", "os")):
        return [w[:-1]]
    return [w]


def _forms(b: str) -> set[str]:
    out = {b}
    consonant_y = len(b) > 1 and b[-1] == "y" and b[-2] not in _VOWELS
    if b.endswith(("s", "x", "z", "ch", "sh")):
        out.add(b + "es")
    elif consonant_y:
        out.add(b[:-1] + "ies")
    else:
        out.add(b + "s")
    if b.endswith("ee"):
        out |= {b + "d", b + "ing"}
    elif b.endswith("e"):
        out |= {b + "d", b[:-1] + "ing"}
    elif consonant_y:
        out |= {b[:-1] + "ied", b + "ing"}
    elif _cvc(b) and _syllables(b) == 1:
        out |= {b + b[-1] + "ed", b + b[-1] + "ing"}
    else:
        out |= {b + "ed", b + "ing"}
    return out


def _eligible(word: str) -> bool:
    return (len(word) >= _MIN_WORD_LEN and bool(_WORD_RE.match(word))
            and (word.islower() or word.istitle()) and word.lower() not in _STOP)


@lru_cache(maxsize=2048)
def word_forms(word: str) -> tuple[str, ...]:
    """Sorted forms of `word` in its own casing (always including `word`), or
    `(word,)` when it is not varied."""
    if not _eligible(word):
        return (word,)
    w = word.lower()
    forms = {w}
    for b in _bases(w):
        forms |= _forms(b)
    if len(forms) < 2 or len(forms) > _MAX_WORD_FORMS:
        return (word,)
    cased = {f.capitalize() if word.istitle() else f for f in forms}
    cased.add(word)
    return tuple(sorted(cased, key=lambda s: (s.lower(), s)))


def _is_inflected(word: str) -> bool:
    return _eligible(word) and _bases(word.lower()) != [word.lower()]


def _raw_variants(text: str, cap: int) -> tuple[str, ...]:
    parts = _SPLIT_RE.split(text)
    word_idx = [i for i in range(0, len(parts), 2) if parts[i]]
    forms = {i: word_forms(parts[i]) for i in word_idx}
    eligible = [i for i in word_idx if len(forms[i]) > 1]
    if not eligible:
        return (text,)
    inflected = [i for i in eligible if _is_inflected(parts[i])]
    for chosen in (eligible, inflected, inflected[-1:], eligible[-1:]):
        if not chosen:
            continue
        n = 1
        for i in chosen:
            n *= len(forms[i])
        if 1 < n <= cap:
            out = set()
            for combo in product(*(forms[i] for i in chosen)):
                p = list(parts)
                for i, f in zip(chosen, combo):
                    p[i] = f
                out.add("".join(p))
            return tuple(sorted(out, key=lambda s: (s.lower(), s)))
    return (text,)


@lru_cache(maxsize=2048)
def phrase_variants(text: str, cap: int = DEFAULT_MAX_VARIANTS) -> tuple[str, ...]:
    """Sorted, de-duplicated word-form variants of `text` (always containing
    `text`), or `(text,)` when nothing is varied. Every form of the same word
    in the same position gives the same tuple (overheat/overheated/overheating/
    overheats), so equivalent wordings produce identical queries."""
    if not isinstance(text, str) or not text.strip():
        return (text,)
    out = _raw_variants(text, cap)
    return out if len(out) >= 2 and text in out else (text,)


def is_variant_group(texts: list[str], cap: int = DEFAULT_MAX_VARIANTS) -> bool:
    """True when `texts` is exactly what `phrase_variants` produces for one of its
    members: an already-expanded group, which the guard accepts unchanged
    (this is what keeps canonicalisation idempotent)."""
    if len(texts) < 2 or not all(isinstance(t, str) for t in texts):
        return False
    target = tuple(texts)
    return any(phrase_variants(t, cap) == target for t in texts)


def threshold_keywords(text: str) -> tuple[str, ...]:
    """Clean an unordered search before OpenSearch applies its field analyzer."""
    words: dict[str, str] = {}
    for part in _SEARCH_SPLIT_RE.split(text):
        word = part.strip(".'‘’")
        if word.endswith(":") and not word.endswith("::"):
            word = word.rstrip(".:'‘’")
        key = word.casefold()
        if key in _SEARCH_STOP_WORDS or not any(c.isalnum() for c in word):
            continue
        words.setdefault(key, word)
    return tuple(words.values())


def _query_objects(node: object) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _query_objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from _query_objects(value)


def text_searches(node: object) -> Iterator[tuple[str, str, int | None]]:
    """Yield the operator, search text, and term minimum from each text clause."""
    for obj in _query_objects(node):
        for kind in ("match", "match_phrase"):
            field = obj.get(kind)
            if isinstance(field, dict) and len(field) == 1:
                (value,) = field.values()
                if isinstance(value, str):
                    yield kind, value, None
                elif isinstance(value, dict) and isinstance(value.get("query"), str):
                    yield kind, value["query"], value.get("minimum_should_match")


def multi_signal_minima(node: object) -> set[int]:
    """Boolean minima above one. Variant and severity alternatives typically use one."""
    minima = set()
    for obj in _query_objects(node):
        group = obj.get("bool")
        minimum = group.get("minimum_should_match") if isinstance(group, dict) else None
        if type(minimum) is int and minimum > 1:
            minima.add(minimum)
    return minima
