"""Turns an uploaded file (TXT, MD, PDF, image) into plain text, as far as the file itself allows.

`analyze` looks at a file page by page and says which pages have a usable text layer. Nothing here talks to a model: what a
text layer gives is free and exact. Pages that have none (a scanned page, a picture) or whose text layer is garbled are
reported as such, and `convert.py` can send just those pages to a model if the user agrees. Whatever comes out is stored and
shown to the user, who can correct it, because a mis-read resume line would otherwise become "evidence" for the writer."""
from __future__ import annotations

import io
from pathlib import PurePath

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_CHARS = 300_000  # per resource
MAX_PAGES = 150
TEXT_EXT = {".txt": "txt", ".md": "md", ".markdown": "md"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# The most frequent Korean syllables. Real Korean prose is ~25-35% these; a text layer whose glyphs map to the wrong characters
# still looks like valid Hangul but has almost none (random syllables: ~1%).
COMMON_SYLLABLES = set("이다의에는을가로하고서한지들기도리과어인으자대사수시것나아있를제해그면보")


class ExtractError(ValueError):
    """The file cannot be turned into text; the message says why, for the user."""


def decode_text(data: bytes) -> str:
    """UTF-8 (with or without BOM), else CP949/EUC-KR (what Excel and old Korean tools save). Never guesses silently."""
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ExtractError("글자 인코딩을 알 수 없습니다 (UTF-8 또는 EUC-KR(CP949)로 저장해 주세요)")


def clean(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").strip()


def page_status(text: str) -> str:
    """'ok' | 'empty' | 'garbled' for the text layer of one page. Garbled text is the worst case: it looks like text, and
    would be stored as evidence. Typical causes: a font without a Unicode map (private-use glyph codes) and Korean bytes
    read as Latin-1."""
    t = "".join(text.split())
    if not t:
        return "empty"
    bad = sum(1 for c in t if 0xE000 <= ord(c) <= 0xF8FF or c == "�" or ord(c) < 32 or 0x80 <= ord(c) <= 0x9F)
    latin1 = sum(1 for c in t if 0xA0 <= ord(c) <= 0xFF)
    if bad / len(t) > 0.1 or latin1 / len(t) > 0.2:
        return "garbled"
    hangul = [c for c in t if "가" <= c <= "힣"]
    if len(hangul) >= 30 and sum(c in COMMON_SYLLABLES for c in hangul) / len(hangul) < 0.08:
        return "garbled"
    return "ok"


def image_type(data: bytes) -> str | None:
    """Media type from the file's own bytes (the extension is only a hint)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _pdf_pages(data: bytes) -> list[dict]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ExtractError("암호가 걸린 PDF입니다 (암호를 풀어서 다시 올려 주세요)")
        n = len(reader.pages)
        if n > MAX_PAGES:
            raise ExtractError(f"PDF가 너무 깁니다 ({n}쪽, 최대 {MAX_PAGES}쪽)")
        texts = [(p.extract_text() or "").strip() for p in reader.pages]
    except ExtractError:
        raise
    except (PdfReadError, ValueError, KeyError, OSError) as e:
        raise ExtractError(f"PDF를 읽을 수 없습니다 ({type(e).__name__})")
    return [{"n": i + 1, "status": page_status(t), "text": t} for i, t in enumerate(texts)]


def analyze(filename: str, data: bytes) -> dict:
    """-> {"kind": "txt"|"md"|"pdf"|"image", "text" (txt/md), "pages" (pdf/image: [{"n", "status", "text"}]), "media_type" (image)}.
    A page's status is 'ok' (usable text layer), 'empty', 'garbled', or 'image'. Raises ExtractError."""
    if not data:
        raise ExtractError("빈 파일입니다")
    if len(data) > MAX_FILE_BYTES:
        raise ExtractError(f"파일이 너무 큽니다 (최대 {MAX_FILE_BYTES // 1024 // 1024}MB)")
    ext = PurePath(filename).suffix.lower()
    if ext == ".pdf" or data[:5] == b"%PDF-":
        return {"kind": "pdf", "pages": _pdf_pages(data)}
    if ext in IMAGE_EXT or image_type(data):
        mt = image_type(data)
        if not mt:
            raise ExtractError("이미지 형식을 알 수 없습니다 (PNG, JPG, WEBP, GIF만 지원)")
        if len(data) > MAX_IMAGE_BYTES:
            raise ExtractError(f"이미지가 너무 큽니다 (최대 {MAX_IMAGE_BYTES // 1024 // 1024}MB)")
        return {"kind": "image", "media_type": mt, "pages": [{"n": 1, "status": "image", "text": ""}]}
    if ext in TEXT_EXT:
        text = clean(decode_text(data))
        if not text:
            raise ExtractError("글자가 없는 파일입니다")
        return {"kind": TEXT_EXT[ext], "text": text}
    raise ExtractError(f"지원하지 않는 형식입니다 ({ext or '확장자 없음'}). TXT, MD, PDF, 이미지(PNG·JPG·WEBP·GIF)만 올릴 수 있습니다")


def bad_pages(a: dict) -> list[dict]:
    return [p for p in a.get("pages", []) if p["status"] != "ok"]


def ok_text(a: dict) -> str:
    """The text of the pages whose text layer can be trusted, in page order."""
    return "\n\n".join(p["text"] for p in a["pages"] if p["status"] == "ok")


def check_length(text: str) -> str:
    if not text:
        raise ExtractError("글자가 없는 파일입니다")
    if len(text) > MAX_CHARS:
        raise ExtractError(f"글자가 너무 많습니다 ({len(text):,}자, 최대 {MAX_CHARS:,}자). 나눠서 올려 주세요")
    return text


def skip_warnings(a: dict) -> list[str]:
    """What is lost when pages without a trusted text layer are left out."""
    out = []
    for status, why in (("empty", "글자가 없어 건너뜀 (그림만 있는 쪽일 수 있음)"), ("garbled", "글자 층이 깨져 있어 건너뜀")):
        pages = [str(p["n"]) for p in a["pages"] if p["status"] == status]
        if pages:
            out.append(f"{', '.join(pages)}쪽은 {why}")
    return out


def extract_text(filename: str, data: bytes) -> dict:
    """No model involved: text files as they are, PDFs from their text layer with unusable pages left out (and said so).
    -> {"text", "source_type", "pages", "warnings"}. Raises ExtractError."""
    a = analyze(filename, data)
    if a["kind"] == "image":
        raise ExtractError("이미지에는 글자 층이 없습니다. LLM으로 변환해야 글자로 읽을 수 있습니다")
    if a["kind"] != "pdf":
        return {"text": check_length(a["text"]), "source_type": a["kind"], "pages": None, "warnings": []}
    text = ok_text(a)
    if not text:
        garbled = any(p["status"] == "garbled" for p in a["pages"])
        raise ExtractError("PDF의 글자 층이 깨져 있습니다. LLM으로 변환하거나 글자를 복사할 수 있는 다른 파일로 올려 주세요" if garbled else
                           "글자가 들어 있지 않은 PDF입니다 (스캔본·이미지로 보임). 글자를 복사할 수 있는 PDF나 TXT로 올리거나 LLM으로 변환해 주세요")
    return {"text": check_length(text), "source_type": "pdf", "pages": len(a["pages"]), "warnings": skip_warnings(a)}
