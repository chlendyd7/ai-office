"""Turns an uploaded file (TXT, MD, PDF) into plain text. Nothing here talks to a model: what comes out is stored and shown
to the user, who can correct it, because a mis-read resume line would otherwise become "evidence" for the writer."""
from __future__ import annotations

import io
from pathlib import PurePath

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_CHARS = 300_000  # per resource
MAX_PAGES = 150
TEXT_EXT = {".txt": "txt", ".md": "md", ".markdown": "md"}


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


def _pdf_text(data: bytes) -> tuple[str, int, list[str]]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ExtractError("암호가 걸린 PDF입니다 (암호를 풀어서 다시 올려 주세요)")
        n = len(reader.pages)
        if n > MAX_PAGES:
            raise ExtractError(f"PDF가 너무 깁니다 ({n}쪽, 최대 {MAX_PAGES}쪽)")
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
    except ExtractError:
        raise
    except (PdfReadError, ValueError, KeyError, OSError) as e:
        raise ExtractError(f"PDF를 읽을 수 없습니다 ({type(e).__name__})")
    empty = [i + 1 for i, t in enumerate(pages) if not t]
    if len(empty) == n:
        raise ExtractError("글자가 들어 있지 않은 PDF입니다 (스캔본·이미지로 보임). 글자를 복사할 수 있는 PDF나 TXT로 올려 주세요")
    warnings = [f"{', '.join(map(str, empty))}쪽에는 글자가 없어 건너뜀 (그림만 있는 쪽일 수 있음)"] if empty else []
    return "\n\n".join(t for t in pages if t), n, warnings


def extract_text(filename: str, data: bytes) -> dict:
    """-> {"text", "source_type", "pages", "warnings"}. Raises ExtractError."""
    if not data:
        raise ExtractError("빈 파일입니다")
    if len(data) > MAX_FILE_BYTES:
        raise ExtractError(f"파일이 너무 큽니다 (최대 {MAX_FILE_BYTES // 1024 // 1024}MB)")
    ext = PurePath(filename).suffix.lower()
    pages, warnings = None, []
    if ext == ".pdf" or data[:5] == b"%PDF-":
        text, pages, warnings = _pdf_text(data)
        kind = "pdf"
    elif ext in TEXT_EXT:
        text, kind = decode_text(data), TEXT_EXT[ext]
    else:
        raise ExtractError(f"지원하지 않는 형식입니다 ({ext or '확장자 없음'}). TXT, MD, PDF만 올릴 수 있습니다")
    text = clean(text)
    if not text:
        raise ExtractError("글자가 없는 파일입니다")
    if len(text) > MAX_CHARS:
        raise ExtractError(f"글자가 너무 많습니다 ({len(text):,}자, 최대 {MAX_CHARS:,}자). 나눠서 올려 주세요")
    return {"text": text, "source_type": kind, "pages": pages, "warnings": warnings}
