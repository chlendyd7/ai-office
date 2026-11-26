"""Document -> text with a model, for what the text layer cannot give: picture-only PDF pages, garbled text layers, images.

Only the pages that need it are sent (a PDF is cut down to those pages first). The call goes through MeteredLLM and is
recorded as a run of the pseudo workflow `convert`, so its tokens and cost are visible in Ops like any other spend, and it
has its own budget. The result is never trusted on its own: the caller stores it as "unverified" until a human has read it
next to the original, because a model that "transcribes" can also summarize or invent.
"""
from __future__ import annotations

import base64
import io
import re

from .config import Criterion, LLMConfig, Pricing, RunConfig
from .extract import bad_pages, check_length, ok_text
from .metered import BudgetExceeded, MeteredLLM
from .research import single_model_pricing
from .store import RunStore
from .summary import summarize

DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_MAX_COST_USD = 0.10
PAGE_TOKENS_WORST = 3000  # input tokens one PDF page may cost (text layer + page picture); measured: ~800-1,700 on small pages
IMAGE_TOKENS_WORST = 3000
OUT_TOKENS_PER_PAGE = 1500

PROMPT = (
    "첨부한 문서({k}쪽)의 글자를 **한 글자도 바꾸지 말고 그대로** 옮겨 적으세요.\n"
    "- 요약, 교정, 번역, 설명, 덧붙임을 하지 마세요. 보이는 글자만 옮깁니다.\n"
    "- 제목은 마크다운 제목(#)으로, 목록은 - 로, 표는 마크다운 표로, 구조만 살리세요.\n"
    "- 읽을 수 없는 글자는 추측하지 말고 [판독불가]로 표시하세요.\n"
    "- 각 쪽의 내용 앞에 `=== 쪽 N ===` 줄을 넣으세요 (N은 1부터 {k}까지, 첨부 문서의 쪽 순서).\n"
    "- 코드 블록(```)으로 감싸지 말고 본문만 출력하세요."
)
_MARK = re.compile(r"^=== 쪽 (\d+) ===[ \t]*$", re.M)


class ConvertError(ValueError):
    """The conversion did not produce usable text; the message says why."""


def estimate_max_usd(kind: str, n_pages: int, model: str, prices: dict[str, dict]) -> float:
    """Worst case, the same way the budget guard counts it (not a prediction)."""
    p = prices.get(model) or {"in_per_1k": 0.003, "out_per_1k": 0.015}  # the default estimate when a price is not verified
    per_page = PAGE_TOKENS_WORST if kind == "pdf" else IMAGE_TOKENS_WORST
    return round(n_pages * per_page / 1000 * p["in_per_1k"] + min(8000, OUT_TOKENS_PER_PAGE * n_pages + 500) / 1000 * p["out_per_1k"], 4)


def subset_pdf(data: bytes, page_numbers: list[int]) -> bytes:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        reader.decrypt("")
    writer = PdfWriter()
    for n in page_numbers:
        writer.add_page(reader.pages[n - 1])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```") and t.endswith("```") and t.count("```") == 2:
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip()


def split_pages(text: str, k: int) -> list[str] | None:
    """The text of each of the k pages, or None if the page markers are not exactly 1..k in order."""
    marks = list(_MARK.finditer(text))
    if [int(m.group(1)) for m in marks] != list(range(1, k + 1)):
        return None
    return [text[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(text)].strip() for i, m in enumerate(marks)]


def convert(*, store: RunStore, llm_factory, prices: dict[str, dict], filename: str, data: bytes, analysis: dict,
            model: str = DEFAULT_MODEL, mode: str = "hchat", max_cost_usd: float = DEFAULT_MAX_COST_USD) -> dict:
    """Converts the pages of `analysis` that have no trusted text layer. Returns
    {"text", "warnings", "run_id", "converted_by": "llm"|"mixed", "pages_converted", "cost_usd", "tokens_in", "tokens_out"}.
    Raises ConvertError. Every attempt leaves a `convert` run behind, even a refused or failed one."""
    pages = bad_pages(analysis)
    if not pages:
        raise ConvertError("변환할 쪽이 없습니다")
    kind, k = analysis["kind"], len(pages)
    if kind == "pdf":
        payload, media_type, a_kind, est = subset_pdf(data, [p["n"] for p in pages]), "application/pdf", "pdf", k * PAGE_TOKENS_WORST
    else:
        payload, media_type, a_kind, est = data, analysis["media_type"], "image", k * IMAGE_TOKENS_WORST
    cfg = RunConfig(
        workflow="convert", rubric=[Criterion(name="-")], budget_usd=max_cost_usd,
        llm=LLMConfig(mode=mode, model=model, max_tokens=min(8000, OUT_TOKENS_PER_PAGE * k + 500), timeout_s=180, max_retries=1),
        **single_model_pricing(model, prices),
    )
    run_id = store.new_run_id()
    store.append(run_id, "run_started", run_id=run_id, workflow="convert", config=cfg.model_dump(), parent=None,
                 input={"filename": filename, "kind": kind, "pages": [p["n"] for p in pages], "total_pages": len(analysis["pages"])})

    def fail(message: str) -> ConvertError:
        store.append(run_id, "error", message=message)
        store.append(run_id, "run_finished", status="failed", reason="error")
        return ConvertError(message)

    try:
        llm = MeteredLLM(llm_factory(cfg.llm), store, run_id, cfg)
        raw = llm.complete(
            agent="converter", purpose="convert", round=1, prompt=PROMPT.format(k=k), meta={"pages": k},
            attachments=[{"kind": a_kind, "media_type": media_type, "data": base64.b64encode(payload).decode(), "est_tokens": est,
                          "pages": [p["n"] for p in pages]}],
        )
    except BudgetExceeded as e:  # refused before anything was sent
        raise fail(f"변환 비용 상한(${max_cost_usd})을 넘을 수 있어 호출하지 않았습니다: {e}")
    except Exception as e:
        raise fail(f"LLM 변환 호출이 실패했습니다: {type(e).__name__}: {e}")
    if llm.last_stop_reason == "max_tokens":  # a cut-off transcription would silently lose the end of the document
        raise fail("변환 결과가 중간에 잘렸습니다 (출력 한도 도달). 문서를 나눠서 올려 주세요")

    text = strip_fence(raw)
    chunks = split_pages(text, k)
    warnings: list[str] = []
    if chunks is None:  # the model ignored the page markers: keep the text, but the position of each page is unknown
        chunks = [_MARK.sub("", text).strip()] + [""] * (k - 1)
        warnings.append("쪽 구분 표시가 맞지 않아 변환된 글자를 한 덩어리로 넣었습니다. 쪽 순서를 확인하세요")
    by_page = {p["n"]: c for p, c in zip(pages, chunks)}
    parts = []
    for p in analysis["pages"]:  # page order; usable text layers are kept as they are, converted pages come from the model
        t = p["text"] if p["status"] == "ok" else by_page.get(p["n"], "")
        if t:
            parts.append(t)
    merged = "\n\n".join(parts)
    if "[판독불가]" in text:
        warnings.append(f"읽을 수 없는 글자가 {text.count('[판독불가]')}곳 있습니다 ([판독불가] 표시). 원본을 보고 채워 넣으세요")
    if len(text) < 20 * k:
        warnings.append("변환된 글자가 지나치게 적습니다. 원본과 비교해 빠진 부분이 없는지 확인하세요")
    try:
        check_length(merged)
    except ValueError as e:
        raise fail(str(e))
    store.append(run_id, "run_finished", status="converted", reason="converted")
    s = summarize(store.read(run_id))
    return {"text": merged, "warnings": warnings, "run_id": run_id, "converted_by": "mixed" if ok_text(analysis) else "llm",
            "pages_converted": [p["n"] for p in pages], "cost_usd": s["totals"]["cost_usd"],
            "tokens_in": s["totals"]["tokens_in"], "tokens_out": s["totals"]["tokens_out"]}
