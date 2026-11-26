"""Which passages of the user's material go into a prompt.

Sending every resource in full on every call costs tokens on every round, and most of a resume is irrelevant to one question.
So the material is cut into passages and the ones that overlap most with the question (and the job description) are kept. The
selection is deterministic and free (no model): the same inputs give the same passages, and the run record says which passage
of which resource was used, so a surprising draft can be traced to what it was given.
"""
from __future__ import annotations

import math
import re

MAX_PASSAGE_CHARS = 900
_TOKEN = re.compile(r"\w+")
_SENTENCE_END = re.compile(r"(?<=[.!?。])\s+|\n")


def passages_of(text: str) -> list[str]:
    """Paragraphs (blank-line separated); a markdown heading starts a new one; an overlong paragraph is cut at sentence ends."""
    out: list[str] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        current = ""
        for line in block.split("\n"):
            if line.lstrip().startswith("#") and current.strip():  # a heading opens its own passage
                out.append(current.strip())
                current = ""
            current += line + "\n"
        block = current.strip()
        if len(block) <= MAX_PASSAGE_CHARS:
            if block:
                out.append(block)
            continue
        piece = ""
        for sentence in _SENTENCE_END.split(block):
            if piece and len(piece) + len(sentence) + 1 > MAX_PASSAGE_CHARS:
                out.append(piece.strip())
                piece = ""
            piece += sentence + " "
        if piece.strip():
            out.append(piece.strip())
    return out


def _bigrams(text: str) -> set[str]:
    """Character pairs inside each word: Korean has no spaces between particles and nouns, so words are matched by their parts."""
    grams: set[str] = set()
    for token in _TOKEN.findall(text.lower()):
        if len(token) == 1:
            continue
        grams.update(token[i:i + 2] for i in range(len(token) - 1))
    return grams


def select_evidence(resources: list[dict], query: str, max_chars: int = 6000) -> dict:
    """resources: [{"id", "name", "text"}] -> {"passages": [{"resource_id", "name", "index", "text", "score"}], "chars", "total_chars", "all_used"}.
    Everything is used when it fits. Otherwise the best-matching passages are kept, up to `max_chars`, and put back in their
    original order (resource order, then position) so the text still reads as the user wrote it."""
    all_passages = [{"resource_id": r["id"], "name": r["name"], "index": i, "text": t}
                    for r in resources for i, t in enumerate(passages_of(r["text"]))]
    total = sum(len(p["text"]) for p in all_passages)
    if total <= max_chars:
        return {"passages": [{**p, "score": None} for p in all_passages], "chars": total, "total_chars": total, "all_used": True}

    q = _bigrams(query)
    grams = [_bigrams(p["text"]) for p in all_passages]
    df: dict[str, int] = {}
    for g in grams:
        for b in g:
            df[b] = df.get(b, 0) + 1
    n = len(all_passages)
    scored = [(sum(math.log(1 + n / (1 + df[b])) for b in q & g), -k) for k, g in enumerate(grams)]  # rarer shared pairs count more; ties: earlier first
    chosen, used = [], 0
    for rank in sorted(range(n), key=lambda k: scored[k], reverse=True):
        size = len(all_passages[rank]["text"])
        if used + size > max_chars:
            continue
        chosen.append(rank)
        used += size
    chosen.sort()
    return {"passages": [{**all_passages[k], "score": round(scored[k][0], 3)} for k in chosen], "chars": used, "total_chars": total, "all_used": False}


def format_evidence(passages: list[dict]) -> str:
    """The block a prompt carries. Each passage says where it came from, so the writer (and a human reading the record) can tell."""
    return "\n\n".join(f"[자료: {p['name']} #{p['index'] + 1}]\n{p['text']}" for p in passages)
