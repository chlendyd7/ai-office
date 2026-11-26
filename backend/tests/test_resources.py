import base64
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from aiops.api.app import create_app
from aiops.core.db import Database
from aiops.core.extract import MAX_CHARS, MAX_FILE_BYTES, ExtractError, decode_text, extract_text
from aiops.core.library import Library
from aiops.dbio import export_dir, import_dir

FIX = Path(__file__).parent / "fixtures"
KO = "프로젝트: 로그 수집 파이프라인\n성과: 처리 시간을 8시간에서 2시간으로 단축"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path))


# ---- text files ----
def test_decoding_utf8_bom_and_korean_legacy_encodings():
    assert decode_text(KO.encode("utf-8")) == KO
    assert decode_text(b"\xef\xbb\xbf" + KO.encode("utf-8")) == KO  # BOM
    assert decode_text(KO.encode("cp949")) == KO  # what Excel / old Korean tools save
    with pytest.raises(ExtractError, match="인코딩"):
        decode_text(b"\x81\x20\x81\x20")  # invalid in both: refuse rather than guess


def test_text_and_markdown_are_normalized():
    r = extract_text("이력서.MD", b"# \xed\x97\xa4\xeb\x8d\x94\r\n\r\n\xeb\x82\xb4\xec\x9a\xa9\x00\r\n")
    assert r["source_type"] == "md" and r["text"] == "# 헤더\n\n내용" and r["pages"] is None
    assert extract_text("a.txt", b" hello \n")["text"] == "hello"


def test_refusals_say_why():
    for name, data, msg in (("a.docx", b"x", "TXT, MD, PDF만"), ("noext", b"x", "확장자 없음"), ("a.txt", b"", "빈 파일"),
                            ("a.txt", b" \n\t ", "글자가 없는"), ("a.txt", b"x" * (MAX_FILE_BYTES + 1), "너무 큽니다"),
                            ("a.txt", ("가" * (MAX_CHARS + 1)).encode(), "너무 많습니다")):
        with pytest.raises(ExtractError, match=msg):
            extract_text(name, data)


# ---- pdf ----
def test_korean_text_pdf_is_extracted_page_by_page():
    r = extract_text("이력서.pdf", (FIX / "sample_ko.pdf").read_bytes())
    assert r["source_type"] == "pdf" and r["pages"] == 2 and r["warnings"] == []
    assert "예시 이력서" in r["text"] and "8시간에서 2시간으로 단축" in r["text"] and "SQL, Python, Airflow" in r["text"]


def test_a_pdf_without_a_text_layer_is_refused_as_scanned():
    with pytest.raises(ExtractError, match="스캔본"):
        extract_text("scan.pdf", (FIX / "scanned_like.pdf").read_bytes())


def test_blank_pages_are_skipped_with_a_warning():
    w = PdfWriter()
    w.add_page(PdfReader(FIX / "sample_ko.pdf").pages[0])
    w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    r = extract_text("mixed.pdf", buf.getvalue())
    assert r["pages"] == 2 and "예시 이력서" in r["text"] and "2쪽" in r["warnings"][0]


def test_broken_and_encrypted_pdfs_are_refused():
    with pytest.raises(ExtractError, match="읽을 수 없습니다"):
        extract_text("x.pdf", b"%PDF-1.4 this is not a pdf")
    w = PdfWriter()
    w.add_page(PdfReader(FIX / "sample_ko.pdf").pages[0])
    w.encrypt("secret")
    buf = io.BytesIO()
    w.write(buf)
    with pytest.raises(ExtractError, match="암호"):
        extract_text("locked.pdf", buf.getvalue())


def test_a_pdf_is_recognised_by_content_even_with_a_wrong_extension():
    assert extract_text("resume.txt", (FIX / "sample_ko.pdf").read_bytes())["source_type"] == "pdf"


# ---- library ----
def test_resource_crud_and_listing_hides_the_text(tmp_path):
    lib = Library(Database(tmp_path / "x.db"))
    rid = lib.add_resource(" 경력 ", KO, source_name="a.txt", source_type="txt")
    row = lib.list_resources()[0]
    assert row["name"] == "경력" and row["chars"] == len(KO) and "text" not in row
    assert lib.get_resource(rid)["text"] == KO
    lib.update_resource(rid, text=KO.replace("8시간", "9시간"))
    assert "9시간" in lib.get_resource(rid)["text"] and lib.get_resource(rid)["name"] == "경력"
    lib.delete_resource(rid)
    with pytest.raises(KeyError):
        lib.get_resource(rid)
    for bad in (("", "x"), ("n", "  "), ("n", "가" * (MAX_CHARS + 1))):
        with pytest.raises(ValueError):
            lib.add_resource(*bad)


# ---- api ----
def test_upload_correct_and_delete_through_the_api(client):
    r = client.post("/api/library/resources/upload", json={"filename": "경력 정리.txt", "data": b64(KO.encode("cp949"))})
    assert r.status_code == 200
    got = r.json()
    assert got["name"] == "경력 정리" and got["source_type"] == "txt" and got["chars"] == len(KO)
    rid = got["id"]
    assert client.get("/api/library/resources").json()[0]["chars"] == len(KO)
    assert client.get(f"/api/library/resources/{rid}").json()["text"] == KO  # EUC-KR came out as proper Korean

    fixed = KO.replace("8시간에서 2시간", "10시간에서 3시간")  # the user corrects what was extracted
    assert client.put(f"/api/library/resources/{rid}", json={"text": fixed}).json()["text"] == fixed
    assert client.put(f"/api/library/resources/{rid}", json={"text": "  "}).status_code == 400
    assert client.get(f"/api/library/resources/{rid}").json()["text"] == fixed  # a refused edit changed nothing

    assert client.delete(f"/api/library/resources/{rid}").status_code == 200
    assert client.get(f"/api/library/resources/{rid}").status_code == 404
    assert client.delete(f"/api/library/resources/{rid}").status_code == 404


def test_pdf_upload_and_refusals_through_the_api(client):
    ok = client.post("/api/library/resources/upload", json={"filename": "이력서.pdf", "data": b64((FIX / "sample_ko.pdf").read_bytes())}).json()
    assert ok["pages"] == 2 and ok["source_type"] == "pdf"
    scan = client.post("/api/library/resources/upload", json={"filename": "s.pdf", "data": b64((FIX / "scanned_like.pdf").read_bytes())})
    assert scan.status_code == 400 and "스캔본" in scan.json()["detail"]
    assert client.post("/api/library/resources/upload", json={"filename": "a.docx", "data": b64(b"x")}).status_code == 400
    assert client.post("/api/library/resources/upload", json={"filename": "a.txt", "data": "***not base64***"}).status_code == 400
    assert len(client.get("/api/library/resources").json()) == 1  # nothing was stored for the refused files


def test_paste_a_resource(client):
    rid = client.post("/api/library/resources", json={"name": "붙여넣기", "text": KO}).json()["id"]
    r = client.get(f"/api/library/resources/{rid}").json()
    assert r["source_type"] == "paste" and r["text"] == KO and r["source_name"] == ""
    assert client.post("/api/library/resources", json={"name": "", "text": KO}).status_code == 400


# ---- travelling with the export ----
def test_resources_travel_with_the_export_and_the_newer_edit_wins(tmp_path):
    a, b = Library(Database(tmp_path / "a.db")), Library(Database(tmp_path / "b.db"))
    rid = a.add_resource("경력", KO, source_name="a.pdf", source_type="pdf", pages=2)
    export_dir(a.db, tmp_path / "out")
    assert (tmp_path / "out" / "resources.jsonl").exists()
    assert import_dir(b.db, tmp_path / "out")["resources"] == 1 and b.get_resource(rid) == a.get_resource(rid)

    a.update_resource(rid, text=KO + "\n추가")
    export_dir(a.db, tmp_path / "out")
    assert import_dir(b.db, tmp_path / "out")["resources"] == 1 and b.get_resource(rid)["text"].endswith("추가")
    b.update_resource(rid, text="B에서 더 늦게 고침")
    assert import_dir(b.db, tmp_path / "out")["resources"] == 0 and b.get_resource(rid)["text"] == "B에서 더 늦게 고침"
