import base64
import io
import json
import random
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from aiops.api.app import PRICING_FILE, create_app
from aiops.core.config import LLMConfig, RunConfig
from aiops.core.convert import split_pages, strip_fence
from aiops.core.db import Database
from aiops.core.engine import Engine
from aiops.core.extract import ExtractError, analyze, image_type, page_status
from aiops.core.library import Library
from aiops.core.metered import BudgetExceeded, MeteredLLM
from aiops.core.pricing import load_prices
from aiops.core.store import RunStore
from aiops.core.summary import summarize
from aiops.dbio import export_dir, import_dir
from aiops.llm.base import LLMResult
from aiops.llm.hchat import HChatClient
from aiops.workflows import REGISTRY
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"
SCAN = (FIX / "scanned_like.pdf").read_bytes()
GARBLED = (FIX / "garbled_like.pdf").read_bytes()
GOOD = (FIX / "sample_ko.pdf").read_bytes()
PNG = (FIX / "jd_screenshot.png").read_bytes()
HAIKU = "claude-haiku-4-5"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def pdf_of(*pages_from: tuple[bytes, int]) -> bytes:
    w = PdfWriter()
    for data, i in pages_from:
        w.add_page(PdfReader(io.BytesIO(data)).pages[i])
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


MIXED = pdf_of((GOOD, 0), (SCAN, 0), (GARBLED, 0))  # page 1 usable, 2 empty, 3 garbled


class Scripted:
    """Stands in for the gateway: records every call, answers with a fixed text."""

    model = HAIKU

    def __init__(self, text="", stop_reason="end_turn", fail=None, tokens=(1000, 300)):
        self.text, self.stop_reason, self.fail, self.tokens, self.calls = text, stop_reason, fail, tokens, []

    def complete(self, *, agent, purpose, prompt, meta, attachments=None):
        self.calls.append({"purpose": purpose, "prompt": prompt, "attachments": attachments, "meta": meta})
        if self.fail:
            raise self.fail
        return LLMResult(self.text, *self.tokens, model=HAIKU, stop_reason=self.stop_reason)


def make(tmp_path, client):
    eng = Engine(RunStore(tmp_path), REGISTRY, llm_factory=lambda cfg: client, prices=load_prices(PRICING_FILE))
    return TestClient(create_app(tmp_path, engine=eng)), eng


def upload(c, name, data, **kw):
    return c.post("/api/library/resources/upload", json={"filename": name, "data": b64(data), **kw})


def convert_runs(eng):
    return [r for r in eng.store.list_runs() if eng.store.read(r)[0]["workflow"] == "convert"]


# ---- telling a usable text layer from an unusable one ----
def test_page_status_separates_real_text_from_look_alikes():
    assert page_status("") == page_status(" \n\t ") == "empty"
    assert page_status("프로젝트: 로그 수집 파이프라인 구축 성과: 처리 시간을 8시간에서 2시간으로 단축") == "ok"
    assert page_status("Led a team of five engineers to build a log pipeline in Python and SQL.") == "ok"
    assert page_status("스킬: Python, SQL, Spark, Kafka, 클라우드 인프라 설계") == "ok"  # short: not enough Hangul to judge
    assert page_status("  normal") == "garbled"  # glyph codes without a Unicode map
    assert page_status("����� ab") == "garbled"
    assert page_status("프로젝트 성과 처리 시간 단축".encode("cp949").decode("latin-1")) == "garbled"  # Korean bytes read as Latin-1
    rnd = random.Random(3)
    syll = [chr(c) for c in range(0xAC00, 0xD7A4)]
    assert page_status("".join(rnd.choice(syll) for _ in range(200))) == "garbled"  # valid Hangul in the wrong order


def test_analyze_classifies_every_page():
    states = lambda name, data: [(p["n"], p["status"]) for p in analyze(name, data)["pages"]]
    assert states("a.pdf", GOOD) == [(1, "ok"), (2, "ok")]
    assert states("a.pdf", SCAN) == [(1, "empty")]
    assert states("a.pdf", GARBLED) == [(1, "garbled")]
    assert states("a.pdf", MIXED) == [(1, "ok"), (2, "empty"), (3, "garbled")]
    a = analyze("jd.png", PNG)
    assert a["kind"] == "image" and a["media_type"] == "image/png" and a["pages"][0]["status"] == "image"


def test_images_are_recognised_by_content_and_limited():
    assert image_type(PNG) == "image/png" and image_type(b"\xff\xd8\xff\xe0abc") == "image/jpeg" and image_type(b"GIF89a") == "image/gif"
    assert image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp" and image_type(b"BM....") is None
    with pytest.raises(ExtractError, match="이미지 형식"):
        analyze("scan.png", b"BM this is a bmp pretending to be a png")
    with pytest.raises(ExtractError, match="이미지가 너무 큽니다"):
        analyze("big.png", PNG + b"\x00" * (5 * 1024 * 1024))
    assert analyze("renamed.txt", PNG)["kind"] == "image"  # content wins over the extension


# ---- asking before spending ----
def test_a_scanned_pdf_is_only_reported_nothing_is_called_or_stored(tmp_path):
    client = Scripted("should never be asked")
    c, eng = make(tmp_path, client)
    r = upload(c, "scan.pdf", SCAN).json()
    assert r["needs_conversion"] is True and r["pages"] == {"ok": [], "empty": [1], "garbled": [], "image": []}
    assert r["can_skip"] is False and r["model"] == HAIKU
    assert r["max_usd"] == pytest.approx(1 * 3.0 * 0.001 + 2.0 * 0.005, abs=1e-4)  # 3k input + 2k output tokens at the verified haiku price
    assert client.calls == [] and c.get("/api/library/resources").json() == [] and convert_runs(eng) == []


# ---- converting ----
def test_conversion_is_metered_stored_unverified_and_sends_only_the_bad_pages(tmp_path):
    client = Scripted("=== 쪽 1 ===\n# 스캔 문서\n성과: 8시간에서 2시간으로 단축")
    c, eng = make(tmp_path, client)
    r = upload(c, "스캔.pdf", SCAN, on_bad_pages="llm").json()
    assert r["converted_by"] == "llm" and r["review_status"] == "unverified" and r["convert_run_id"]
    row = c.get(f"/api/library/resources/{r['id']}").json()
    assert row["text"] == "# 스캔 문서\n성과: 8시간에서 2시간으로 단축" and row["review_status"] == "unverified"  # the page marker is not part of the text

    (call,) = client.calls
    att = call["attachments"][0]
    assert att["kind"] == "pdf" and att["media_type"] == "application/pdf" and att["est_tokens"] == 3000
    assert len(PdfReader(io.BytesIO(base64.b64decode(att["data"]))).pages) == 1
    assert "한 글자도 바꾸지 말고" in call["prompt"] and "`=== 쪽 N ===`" in call["prompt"]

    ev = eng.store.read(r["convert_run_id"])
    s = summarize(ev)
    assert s["workflow"] == "convert" and s["status"] == "converted" and s["totals"]["calls"] == 1
    assert s["totals"]["cost_usd"] == pytest.approx(1000 * 1 / 1e6 + 300 * 5 / 1e6)  # tokens x verified price, like any other call
    rec = next(e for e in ev if e["type"] == "llm_call")
    assert rec["attachments"][0]["kind"] == "pdf" and rec["attachments"][0]["bytes"] > 0 and "data" not in rec["attachments"][0]
    assert b64(SCAN)[:60] not in json.dumps(ev)  # the file content itself is never written to the run record
    assert r["cost_usd"] == s["totals"]["cost_usd"]


def test_a_mixed_pdf_keeps_the_good_page_and_converts_only_the_others(tmp_path):
    client = Scripted("=== 쪽 1 ===\n두 번째 쪽 변환문\n=== 쪽 2 ===\n세 번째 쪽 변환문")
    c, _ = make(tmp_path, client)
    ask = upload(c, "mixed.pdf", MIXED).json()
    assert ask["pages"] == {"ok": [1], "empty": [2], "garbled": [3], "image": []} and ask["can_skip"] is True
    r = upload(c, "mixed.pdf", MIXED, on_bad_pages="llm").json()
    assert r["converted_by"] == "mixed" and r["review_status"] == "unverified"
    text = c.get(f"/api/library/resources/{r['id']}").json()["text"]
    page1 = analyze("x.pdf", MIXED)["pages"][0]["text"]
    assert text == f"{page1}\n\n두 번째 쪽 변환문\n\n세 번째 쪽 변환문"  # page order; page 1 is the text layer, not the model's version
    assert len(PdfReader(io.BytesIO(base64.b64decode(client.calls[0]["attachments"][0]["data"]))).pages) == 2  # only pages 2 and 3 were sent
    assert client.calls[0]["attachments"][0]["est_tokens"] == 6000


def test_skipping_keeps_only_the_good_pages_and_says_what_was_dropped(tmp_path):
    client = Scripted("unused")
    c, eng = make(tmp_path, client)
    r = upload(c, "mixed.pdf", MIXED, on_bad_pages="skip").json()
    assert r["converted_by"] == "text" and r["review_status"] == "verified" and r["convert_run_id"] == ""
    assert any("2쪽" in w for w in r["warnings"]) and any("3쪽" in w and "깨져" in w for w in r["warnings"])
    assert "예시 이력서" in c.get(f"/api/library/resources/{r['id']}").json()["text"]
    assert client.calls == [] and convert_runs(eng) == []


def test_an_image_is_converted_like_a_picture_only_pdf(tmp_path):
    client = Scripted("=== 쪽 1 ===\n직무: 데이터 엔지니어\n자격요건: SQL, Python 3년 이상")
    c, _ = make(tmp_path, client)
    ask = upload(c, "공고.png", PNG).json()
    assert ask["needs_conversion"] and ask["kind"] == "image" and ask["can_skip"] is False
    r = upload(c, "공고.png", PNG, on_bad_pages="llm").json()
    att = client.calls[0]["attachments"][0]
    assert att["kind"] == "image" and att["media_type"] == "image/png" and base64.b64decode(att["data"]) == PNG
    row = c.get(f"/api/library/resources/{r['id']}").json()
    assert row["source_type"] == "image" and row["review_status"] == "unverified" and "SQL, Python" in row["text"]


# ---- when it goes wrong, nothing wrong is stored ----
def test_a_cut_off_transcription_is_refused(tmp_path):
    client = Scripted("=== 쪽 1 ===\n앞부분만 있고", stop_reason="max_tokens")
    c, eng = make(tmp_path, client)
    r = upload(c, "scan.pdf", SCAN, on_bad_pages="llm")
    assert r.status_code == 400 and "잘렸" in r.json()["detail"]
    assert c.get("/api/library/resources").json() == []
    (rid,) = convert_runs(eng)
    s = summarize(eng.store.read(rid))
    assert s["status"] == "failed" and s["totals"]["cost_usd"] > 0  # the money was spent, and the record says so


def test_the_cost_cap_refuses_before_anything_is_sent(tmp_path):
    client = Scripted("unused")
    c, eng = make(tmp_path, client)
    r = upload(c, "scan.pdf", SCAN, on_bad_pages="llm", max_cost_usd=0.001)  # the worst case for one page is ~$0.013
    assert r.status_code == 400 and "상한" in r.json()["detail"]
    assert client.calls == [] and c.get("/api/library/resources").json() == []
    (rid,) = convert_runs(eng)
    s = summarize(eng.store.read(rid))
    assert s["status"] == "failed" and s["totals"]["calls"] == 0


def test_a_gateway_failure_is_reported_and_recorded(tmp_path):
    client = Scripted(fail=RuntimeError("HTTP 500"))
    c, eng = make(tmp_path, client)
    r = upload(c, "scan.pdf", SCAN, on_bad_pages="llm")
    assert r.status_code == 400 and "HTTP 500" in r.json()["detail"] and c.get("/api/library/resources").json() == []
    (rid,) = convert_runs(eng)
    s = summarize(eng.store.read(rid))
    assert s["status"] == "failed" and s["failed_calls"] == 1


def test_warnings_for_unreadable_text_missing_markers_and_suspiciously_short_output(tmp_path):
    c, _ = make(tmp_path, Scripted("=== 쪽 1 ===\n" + "가나다라마바사 " * 10 + "[판독불가] 그리고 [판독불가]"))
    w = upload(c, "scan.pdf", SCAN, on_bad_pages="llm").json()["warnings"]
    assert any("2곳" in x and "판독불가" in x for x in w)

    c, _ = make(tmp_path / "b", Scripted("마커 없이 그냥 본문입니다. " * 5))
    w = upload(c, "scan.pdf", SCAN, on_bad_pages="llm").json()["warnings"]
    assert any("쪽 구분" in x for x in w)

    c, _ = make(tmp_path / "c", Scripted("=== 쪽 1 ===\n짧음"))
    w = upload(c, "scan.pdf", SCAN, on_bad_pages="llm").json()["warnings"]
    assert any("지나치게 적습니다" in x for x in w)


def test_page_markers_and_code_fences():
    assert split_pages("=== 쪽 1 ===\nA\n=== 쪽 2 ===\nB", 2) == ["A", "B"]
    for bad in ("A\nB", "=== 쪽 2 ===\nA\n=== 쪽 1 ===\nB", "=== 쪽 1 ===\nA"):
        assert split_pages(bad, 2) is None  # wrong, reordered or missing markers are never guessed at
    assert strip_fence("```markdown\n# 제목\n본문\n```") == "# 제목\n본문"
    assert strip_fence("본문 속 ``` 하나") == "본문 속 ``` 하나"


# ---- the review gate ----
def test_text_changes_do_not_verify_and_unverified_resources_cannot_be_evidence(tmp_path):
    c, eng = make(tmp_path, Scripted("=== 쪽 1 ===\n" + "변환된 글자입니다 " * 10))
    rid = upload(c, "scan.pdf", SCAN, on_bad_pages="llm").json()["id"]
    lib = Library(eng.store.db)
    with pytest.raises(ValueError, match="검수 전"):
        lib.require_verified([rid])

    c.put(f"/api/library/resources/{rid}", json={"text": "원본을 보고 고친 글자입니다 " * 5})
    assert c.get(f"/api/library/resources/{rid}").json()["review_status"] == "unverified"  # fixing text is not the same as signing off
    with pytest.raises(ValueError, match="검수 전"):
        lib.require_verified([rid])

    assert c.put(f"/api/library/resources/{rid}", json={"verified": True}).json()["review_status"] == "verified"
    assert [r["id"] for r in lib.require_verified([rid])] == [rid]
    assert c.put(f"/api/library/resources/{rid}", json={"verified": False}).json()["review_status"] == "unverified"
    with pytest.raises(KeyError):
        lib.require_verified(["nope"])


def test_text_files_and_text_pdfs_need_no_review(tmp_path):
    c, _ = make(tmp_path, Scripted("unused"))
    for name, data in (("a.txt", "메모".encode()), ("a.pdf", GOOD)):
        r = upload(c, name, data).json()
        assert r["review_status"] == "verified" and r["converted_by"] == "text" and r["cost_usd"] == 0


# ---- housekeeping runs stay out of the Office ----
def test_conversion_runs_are_visible_in_ops_but_not_in_the_office(tmp_path):
    c, eng = make(tmp_path, Scripted("=== 쪽 1 ===\n" + "변환된 글자입니다 " * 10))
    rid = upload(c, "scan.pdf", SCAN, on_bad_pages="llm").json()["convert_run_id"]
    assert rid in [r["run_id"] for r in c.get("/api/runs").json()]
    row = next(r for r in c.get("/api/runs").json() if r["run_id"] == rid)
    assert row["workflow"] == "convert" and row["status"] == "converted" and row["cost_usd"] > 0
    assert c.get("/api/office/runs").json() == []


def test_a_conversion_interrupted_by_a_restart_is_closed_as_failed(tmp_path):
    eng = Engine(RunStore(tmp_path), REGISTRY)
    eng.store.append("conv1", "run_started", run_id="conv1", workflow="convert", config=RunConfig(workflow="convert", rubric=[{"name": "-"}]).model_dump(),
                     input={}, parent=None)
    assert eng.recover_interrupted() == ["conv1"] and summarize(eng.store.read("conv1"))["status"] == "failed"


# ---- the pieces underneath ----
def test_the_budget_guard_counts_attachments_the_prompt_text_does_not_show(tmp_path):
    store = RunStore(tmp_path)
    cfg = RunConfig(workflow="convert", rubric=[{"name": "-"}], budget_usd=0.01, llm=LLMConfig(mode="fake", max_tokens=16),
                    pricing_models={"x": {"in_per_1k": 0.001, "out_per_1k": 0.005}})
    store.append("r", "run_started", run_id="r", workflow="convert", config=cfg.model_dump(), input={}, parent=None)
    llm = MeteredLLM(Scripted("ok"), store, "r", cfg)
    assert llm.complete(agent="a", purpose="convert", round=1, prompt="짧은 프롬프트") == "ok"  # cheap without attachments
    with pytest.raises(BudgetExceeded):  # 200k tokens of attachment at the worst price: ~$0.20 > $0.01, though the prompt is tiny
        llm.complete(agent="a", purpose="convert", round=1, prompt="짧은 프롬프트", attachments=[{"kind": "pdf", "media_type": "application/pdf", "data": "AAAA", "est_tokens": 200_000}])


def test_the_gateway_client_sends_attachments_as_document_and_image_blocks():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 10, "output_tokens": 2},
                                         "stop_reason": "max_tokens", "model": HAIKU})

    client = HChatClient(LLMConfig(mode="hchat", model=HAIKU), env={"H_CHAT_API_KEY": "k", "H_CHAT_BASE_URL": "http://gw/api/v3"},
                         transport=httpx.MockTransport(handler))
    res = client.complete(agent="converter", purpose="convert", prompt="옮겨 적어라", meta={},
                          attachments=[{"kind": "pdf", "media_type": "application/pdf", "data": "UERG"}, {"kind": "image", "media_type": "image/png", "data": "UE5H"}])
    blocks = seen["body"]["messages"][0]["content"]
    assert blocks[0] == {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "UERG"}}
    assert blocks[1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "UE5H"}}
    assert blocks[2] == {"type": "text", "text": "옮겨 적어라"}
    assert res.stop_reason == "max_tokens"  # surfaced, so a cut-off answer can be recognised
    client.complete(agent="w", purpose="write", prompt="plain", meta={})
    assert seen["body"]["messages"][0]["content"] == "plain"  # no attachments: the request is exactly what it was before


# ---- data that already exists, and data on the move ----
def test_an_existing_database_gets_the_new_columns_and_keeps_its_rows(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE resources (id TEXT PRIMARY KEY, name TEXT NOT NULL, source_name TEXT NOT NULL DEFAULT '', source_type TEXT NOT NULL, "
                "pages INTEGER, text TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    old.execute("INSERT INTO resources VALUES ('r1', '옛 리소스', 'a.txt', 'txt', NULL, '옛 내용', 't', 't')")
    old.commit()
    old.close()
    lib = Library(Database(path))
    r = lib.get_resource("r1")
    assert (r["name"], r["text"], r["review_status"], r["converted_by"], r["convert_run_id"]) == ("옛 리소스", "옛 내용", "verified", "text", "")


def test_review_state_travels_with_the_export_and_old_exports_still_import(tmp_path):
    a, b = Library(Database(tmp_path / "a.db")), Library(Database(tmp_path / "b.db"))
    rid = a.add_resource("변환본", "내용", source_type="pdf", pages=1, converted_by="llm", convert_run_id="run-1")
    export_dir(a.db, tmp_path / "out")
    assert import_dir(b.db, tmp_path / "out")["resources"] == 1
    got = b.get_resource(rid)
    assert (got["review_status"], got["converted_by"], got["convert_run_id"]) == ("unverified", "llm", "run-1")

    a.update_resource(rid, verified=True)  # a later sign-off on A reaches B
    export_dir(a.db, tmp_path / "out")
    import_dir(b.db, tmp_path / "out")
    assert b.get_resource(rid)["review_status"] == "verified"

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "resources.jsonl").write_text(json.dumps({"id": "old1", "name": "옛", "source_name": "", "source_type": "paste", "pages": None,
                                                         "text": "t", "created_at": "t", "updated_at": "t"}, ensure_ascii=False) + "\n", encoding="utf-8")
    c = Library(Database(tmp_path / "c.db"))
    assert import_dir(c.db, legacy)["resources"] == 1 and c.get_resource("old1")["review_status"] == "verified"


def test_a_conversion_run_cannot_be_re_run_or_decided_like_a_cover_letter(tmp_path):
    c, _ = make(tmp_path, Scripted("=== 쪽 1 ===\n" + "변환된 글자입니다 " * 10))
    rid = upload(c, "scan.pdf", SCAN, on_bad_pages="llm").json()["convert_run_id"]
    assert c.post(f"/api/runs/{rid}/rerun", json={}).status_code == 400
    assert c.post(f"/api/runs/{rid}/decision", json={"decision": "approve"}).status_code == 409
