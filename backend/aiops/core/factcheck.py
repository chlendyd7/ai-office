"""The fact guard: numbers and English terms in a draft that none of the allowed texts contain.

A model asked to make a draft "more concrete" invents concrete things: figures, tools, scales. The reviewer (also a model) tends to
reward them. This check does not ask a model: it looks for each number and each English term of the draft in the texts the draft
may draw on, and reports those it cannot find. Cheap, deterministic, and deliberately narrow:

- it catches invented figures and tool names, the most damaging kind of invention in a cover letter
- it does NOT catch Korean proper nouns, or a fact reworded so that its number changes form ("두 배" -> "200%" is caught, "200%"
  -> "두 배" is not): those are left to the reviewer and to the human who approves
"""
from __future__ import annotations

import re

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_TERM = re.compile(r"[A-Za-z][A-Za-z0-9+#\-]*[A-Za-z0-9+#]|[A-Za-z]{2}")
_PLACEHOLDER = re.compile(r"\[[^\]\n]*\]")  # "[수치 입력 필요]": a blank the writer was told to leave, not a claim
_LIST_MARK = re.compile(r"(?m)^\s*\d+[.)]\s")  # "1. " at the start of a line is numbering, not a figure
COMMON_WORDS = {"a", "an", "the", "and", "or", "of", "to", "in", "for", "on", "with", "at", "by", "vs", "etc", "is", "as", "be", "it"}


def _clean(text: str) -> str:
    return _LIST_MARK.sub("", _PLACEHOLDER.sub(" ", text))


def _norm(number: str) -> str:
    return number.replace(",", "").rstrip(".")


_SENTENCE = re.compile(r"[.!?。\n]+")


def _derivable(texts: list[str]) -> list[float]:
    """Percentages and multiples that follow from two numbers of the SAME sentence of the material ("8시간에서 2시간으로" -> 75%, 300%, 4배).
    A computed figure is not an invented one. Only a single sentence counts: numbers from different places combine into too many
    coincidences, and an invented figure could pass by luck."""
    values: list[float] = []
    for text in texts:
        for sentence in _SENTENCE.split(_clean(text)):
            nums = sorted({float(_norm(m)) for m in _NUMBER.findall(sentence)})
            for i, small in enumerate(nums):
                for big in nums[i + 1:]:
                    if small > 0:
                        values += [(big - small) / big * 100, (big - small) / small * 100, big / small]
    return values


def _near(n: str, values: list[float]) -> bool:
    """`n` equals one of the values as written: an integer within rounding, a decimal to one place."""
    x, tol = float(n), (0.05 if "." in n else 0.5)
    return any(abs(x - v) <= tol for v in values)


def find_unsupported(draft: str, allowed: list[str]) -> dict:
    """-> {"numbers": [...], "terms": [...]} (+ "derived": [...] when figures were allowed because they follow from the material):
    what the draft says that no allowed text says, in order of first appearance."""
    pool = _clean("\n".join(allowed))
    have_n = {_norm(m) for m in _NUMBER.findall(pool)}
    have_t = {t.lower() for t in _TERM.findall(pool)}
    out: dict[str, list[str]] = {"numbers": [], "terms": []}
    body = _clean(draft)
    derivable = _derivable(allowed)
    derived: list[str] = []
    for m in _NUMBER.findall(body):
        n = _norm(m)
        if n in have_n or n in out["numbers"] or n in derived:
            continue
        (derived if _near(n, derivable) else out["numbers"]).append(n)
    seen: set[str] = set()
    for t in _TERM.findall(body):
        low = t.lower()
        if low not in COMMON_WORDS and low not in have_t and low not in seen:
            seen.add(low)
            out["terms"].append(t)
    if derived:
        out["derived"] = derived  # allowed, but recorded: a human reading the run can see which figures were computed, not quoted
    return out


def describe(found: dict) -> str:
    """What the writer is told when its draft failed the check (empty when it passed)."""
    parts = []
    if found["numbers"]:
        parts.append("근거 자료에 없는 수치: " + ", ".join(found["numbers"]))
    if found["terms"]:
        parts.append("근거 자료에 없는 영문 용어: " + ", ".join(found["terms"]))
    return " / ".join(parts) + (" → 근거에 있는 사실만 쓰고, 없으면 지우거나 [입력 필요]로 비우세요" if parts else "")
