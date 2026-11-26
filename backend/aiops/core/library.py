"""The editable library: datasets (inputs to run) and versioned prompt templates.

Principle: the library is free to change, a run is a snapshot. A run copies the input and the prompt text it used into
its own record, so editing or deleting library rows never alters what a past run did.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from .db import Database, open_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")  # import compares these: seconds would miss quick edits


def _split_ref(ref: str) -> tuple[str, int | None]:
    name, _, v = ref.partition("@")
    return name, (int(v) if v else None)


RESOURCE_FIELDS = ("id", "name", "source_name", "source_type", "pages", "text", "created_at", "updated_at",
                   "review_status", "converted_by", "convert_run_id", "meta", "owner_project_id")
RESOURCE_DEFAULTS = {"review_status": "verified", "converted_by": "text", "convert_run_id": "", "meta": {}, "owner_project_id": ""}  # for older exports
CONVERTED_BY = ("text", "llm", "mixed")
REVIEW_STATUS = ("verified", "unverified")


def _check_resource(name: str, text: str) -> tuple[str, str]:
    from .extract import MAX_CHARS

    name, text = name.strip(), text.strip()
    if not name:
        raise ValueError("resource name is empty")
    if not text:
        raise ValueError("resource text is empty")
    if len(text) > MAX_CHARS:
        raise ValueError(f"resource text is too long ({len(text):,} chars, max {MAX_CHARS:,})")
    return name, text


class Library:
    def __init__(self, db: "str | Database"):
        self.db = open_db(db)

    # ---- datasets ----
    def create_dataset(self, name: str, workflow: str) -> str:
        name = name.strip()
        if not name:
            raise ValueError("dataset name is empty")
        did = uuid.uuid4().hex[:10]
        try:
            with self.db.lock:
                self.db.conn.execute("INSERT INTO datasets (id, name, workflow, created_at) VALUES (?, ?, ?, ?)", (did, name, workflow, _now()))
        except sqlite3.IntegrityError:
            raise ValueError(f"a dataset named {name!r} already exists")
        return did

    def list_datasets(self) -> list[dict]:
        with self.db.lock:
            rows = self.db.conn.execute(
                "SELECT d.id, d.name, d.workflow, d.created_at, COUNT(i.id) FROM datasets d LEFT JOIN items i ON i.dataset_id = d.id "
                "GROUP BY d.id ORDER BY d.created_at, d.name").fetchall()
        return [{"id": a, "name": b, "workflow": c, "created_at": d, "items": n} for a, b, c, d, n in rows]

    def get_dataset(self, dataset_id: str) -> dict:
        with self.db.lock:
            r = self.db.conn.execute("SELECT id, name, workflow, created_at FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
        if not r:
            raise KeyError(dataset_id)
        return {"id": r[0], "name": r[1], "workflow": r[2], "created_at": r[3]}

    def delete_dataset(self, dataset_id: str) -> None:
        self.get_dataset(dataset_id)
        with self.db.lock:
            self.db.conn.execute("BEGIN")
            try:
                self.db.conn.execute("DELETE FROM items WHERE dataset_id = ?", (dataset_id,))
                self.db.conn.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))
                self.db.conn.execute("COMMIT")
            except BaseException:
                self.db.conn.execute("ROLLBACK")
                raise

    # ---- items ----
    def add_items(self, dataset_id: str, items: list[dict]) -> list[str]:
        """items: [{"data": {...}, "label": "..."}]. All or nothing."""
        self.get_dataset(dataset_id)
        ids, now = [], _now()
        with self.db.lock:
            self.db.conn.execute("BEGIN")
            try:
                for it in items:
                    iid = uuid.uuid4().hex[:10]
                    self.db.conn.execute("INSERT INTO items (id, dataset_id, label, data, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                                         (iid, dataset_id, it.get("label", ""), json.dumps(it["data"], ensure_ascii=False), now, now))
                    ids.append(iid)
                self.db.conn.execute("COMMIT")
            except BaseException:
                self.db.conn.execute("ROLLBACK")
                raise
        return ids

    @staticmethod
    def _item(r) -> dict:
        return {"id": r[0], "dataset_id": r[1], "label": r[2], "data": json.loads(r[3]), "created_at": r[4], "updated_at": r[5]}

    def list_items(self, dataset_id: str) -> list[dict]:
        self.get_dataset(dataset_id)
        with self.db.lock:
            rows = self.db.conn.execute("SELECT id, dataset_id, label, data, created_at, updated_at FROM items WHERE dataset_id = ? "
                                        "ORDER BY created_at, rowid", (dataset_id,)).fetchall()
        return [self._item(r) for r in rows]

    def get_item(self, item_id: str) -> dict:
        with self.db.lock:
            r = self.db.conn.execute("SELECT id, dataset_id, label, data, created_at, updated_at FROM items WHERE id = ?", (item_id,)).fetchone()
        if not r:
            raise KeyError(item_id)
        return self._item(r)

    def update_item(self, item_id: str, data: dict, label: str | None = None) -> dict:
        cur = self.get_item(item_id)
        with self.db.lock:
            self.db.conn.execute("UPDATE items SET data = ?, label = ?, updated_at = ? WHERE id = ?",
                                 (json.dumps(data, ensure_ascii=False), cur["label"] if label is None else label, _now(), item_id))
        return self.get_item(item_id)

    def delete_item(self, item_id: str) -> None:
        self.get_item(item_id)
        with self.db.lock:
            self.db.conn.execute("DELETE FROM items WHERE id = ?", (item_id,))

    # ---- resources: the user's own material (resume, experience notes), stored as text so it can be corrected ----
    def add_resource(self, name: str, text: str, source_name: str = "", source_type: str = "paste", pages: int | None = None,
                     converted_by: str = "text", convert_run_id: str = "", meta: dict | None = None, owner_project_id: str = "") -> str:
        """Text a model produced (converted_by 'llm' or 'mixed') starts "unverified": it cannot be used as evidence until a
        human has checked it against the original. `owner_project_id`: '' = my library; otherwise the resource belongs to that
        project only (a job description, a company research) and is not listed in the library."""
        if converted_by not in CONVERTED_BY:
            raise ValueError(f"converted_by must be one of {CONVERTED_BY}")
        name, text = _check_resource(name, text)
        rid, now = uuid.uuid4().hex[:10], _now()
        row = (rid, name, source_name, source_type, pages, text, now, now, "verified" if converted_by == "text" else "unverified",
               converted_by, convert_run_id, json.dumps(meta or {}, ensure_ascii=False), owner_project_id)
        with self.db.lock:
            self.db.conn.execute(f"INSERT INTO resources ({', '.join(RESOURCE_FIELDS)}) VALUES ({', '.join('?' * len(RESOURCE_FIELDS))})", row)
        return rid

    def list_resources(self, owner_project_id: str = "") -> list[dict]:
        """Without the text (it can be long): id, name, type, chars, review state. By default only my library ('');
        pass a project id for the resources that belong to that project alone."""
        with self.db.lock:
            rows = self.db.conn.execute("SELECT id, name, source_name, source_type, pages, length(text), created_at, updated_at, "
                                        "review_status, converted_by, convert_run_id, owner_project_id FROM resources "
                                        "WHERE owner_project_id = ? ORDER BY created_at, rowid", (owner_project_id,)).fetchall()
        return [dict(zip(("id", "name", "source_name", "source_type", "pages", "chars", "created_at", "updated_at",
                          "review_status", "converted_by", "convert_run_id", "owner_project_id"), r)) for r in rows]

    def promote_resource(self, resource_id: str) -> dict:
        """A project's own resource (say, a company research) goes into my library so later projects can use it."""
        self.get_resource(resource_id)
        with self.db.lock:
            self.db.conn.execute("UPDATE resources SET owner_project_id = '', updated_at = ? WHERE id = ?", (_now(), resource_id))
        return self.get_resource(resource_id)

    def require_verified(self, resource_ids: list[str]) -> list[dict]:
        """The resources a task may use as evidence: all must exist and be verified. Returns them (with text)."""
        out, pending = [], []
        for rid in resource_ids:
            r = self.get_resource(rid)  # KeyError for an unknown id
            (out if r["review_status"] == "verified" else pending).append(r)
        if pending:
            raise ValueError("검수 전 리소스는 근거로 쓸 수 없습니다 (원본과 대조 후 '검수 완료'로 표시하세요): "
                             + ", ".join(f"{r['name']}" for r in pending))
        return out

    def get_resource(self, resource_id: str) -> dict:
        with self.db.lock:
            r = self.db.conn.execute(f"SELECT {', '.join(RESOURCE_FIELDS)} FROM resources WHERE id = ?", (resource_id,)).fetchone()
        if not r:
            raise KeyError(resource_id)
        out = dict(zip(RESOURCE_FIELDS, r))
        out["meta"] = json.loads(out["meta"])
        return out

    def update_resource(self, resource_id: str, name: str | None = None, text: str | None = None, verified: bool | None = None) -> dict:
        """Editing the text never changes the review state: only an explicit `verified` does."""
        cur = self.get_resource(resource_id)
        name, text = _check_resource(cur["name"] if name is None else name, cur["text"] if text is None else text)
        status = cur["review_status"] if verified is None else ("verified" if verified else "unverified")
        with self.db.lock:
            self.db.conn.execute("UPDATE resources SET name = ?, text = ?, review_status = ?, updated_at = ? WHERE id = ?",
                                 (name, text, status, _now(), resource_id))
        return self.get_resource(resource_id)

    def delete_resource(self, resource_id: str) -> None:
        """Refused while a project selects it (the message names the projects): remove it from them first."""
        self.get_resource(resource_id)
        with self.db.lock:
            using = [r[0] for r in self.db.conn.execute(
                "SELECT p.title FROM project_resources pr JOIN projects p ON p.id = pr.project_id WHERE pr.resource_id = ? ORDER BY p.title", (resource_id,))]
            if using:
                raise ValueError("프로젝트가 이 리소스를 쓰고 있어 삭제할 수 없습니다 (먼저 프로젝트에서 빼세요): " + ", ".join(using))
            self.db.conn.execute("DELETE FROM resources WHERE id = ?", (resource_id,))

    # ---- prompts ----
    def save_prompt(self, workflow: str, role: str, name: str, body: str, note: str = "") -> int:
        """Saves `body` as the next version of `name`. A name belongs to one workflow role for good."""
        name = name.strip()
        if not name or "@" in name:
            raise ValueError("prompt name must be non-empty and must not contain '@'")
        if not body.strip():
            raise ValueError("prompt body is empty")
        with self.db.lock:
            row = self.db.conn.execute("SELECT workflow, role, MAX(version) FROM prompts WHERE name = ? GROUP BY name", (name,)).fetchone()
            if row and (row[0], row[1]) != (workflow, role):
                raise ValueError(f"prompt {name!r} already exists for {row[0]}/{row[1]}")
            version = (row[2] if row else 0) + 1
            self.db.conn.execute("INSERT INTO prompts (name, version, workflow, role, body, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (name, version, workflow, role, body, note, _now()))
        return version

    def list_prompts(self, workflow: str | None = None) -> list[dict]:
        q = "SELECT name, version, workflow, role, note, created_at FROM prompts" + (" WHERE workflow = ?" if workflow else "") + " ORDER BY name, version"
        with self.db.lock:
            rows = self.db.conn.execute(q, (workflow,) if workflow else ()).fetchall()
        out: dict[str, dict] = {}
        for name, v, wf, role, note, ts in rows:
            p = out.setdefault(name, {"name": name, "workflow": wf, "role": role, "versions": []})
            p["versions"].append({"version": v, "note": note, "created_at": ts})
        return list(out.values())

    def get_prompt(self, name: str, version: int | None = None) -> dict:
        with self.db.lock:
            if version is None:
                r = self.db.conn.execute("SELECT name, version, workflow, role, body, note, created_at FROM prompts WHERE name = ? "
                                         "ORDER BY version DESC LIMIT 1", (name,)).fetchone()
            else:
                r = self.db.conn.execute("SELECT name, version, workflow, role, body, note, created_at FROM prompts WHERE name = ? AND version = ?",
                                         (name, version)).fetchone()
        if not r:
            raise KeyError(f"{name}@{version}" if version else name)
        return dict(zip(("name", "version", "workflow", "role", "body", "note", "created_at"), r))

    def resolve(self, workflow: str, refs: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
        """{role: "name" | "name@3"} -> ({role: body}, {role: "name@3"}). `name` alone means the latest version now."""
        bodies, pinned = {}, {}
        for role, ref in refs.items():
            name, version = _split_ref(ref)
            try:
                p = self.get_prompt(name, version)
            except KeyError:
                raise ValueError(f"unknown prompt: {ref}")
            if (p["workflow"], p["role"]) != (workflow, role):
                raise ValueError(f"prompt {ref} is for {p['workflow']}/{p['role']}, not {workflow}/{role}")
            bodies[role], pinned[role] = p["body"], f"{p['name']}@{p['version']}"
        return bodies, pinned


MAX_IMPORT_ROWS = 500


def parse_import(fmt: str, text: str) -> list[dict]:
    """CSV (header row) or JSON (a list of objects, or {"items": [...]}) -> [{"label": str, "raw": {...}}].
    A JSON row may be flat ({"job": ...}) or {"label": ..., "data": {...}}. Raises ValueError with the row number."""
    import csv
    import io

    if fmt == "csv":
        reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
        if not reader.fieldnames:
            raise ValueError("csv needs a header row")
        rows = [{(k or "").strip(): v for k, v in r.items()} for r in reader]
    elif fmt == "json":
        try:
            rows = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid json: {e}")
        if isinstance(rows, dict):
            rows = rows.get("items")
        if not isinstance(rows, list):
            raise ValueError("json must be a list of objects or {\"items\": [...]}")
    else:
        raise ValueError("format must be 'csv' or 'json'")
    if not rows:
        raise ValueError("no rows to import")
    if len(rows) > MAX_IMPORT_ROWS:
        raise ValueError(f"at most {MAX_IMPORT_ROWS} rows per import (got {len(rows)})")
    out = []
    for n, r in enumerate(rows, 1):
        if not isinstance(r, dict):
            raise ValueError(f"row {n}: expected an object")
        if isinstance(r.get("data"), dict):
            out.append({"label": str(r.get("label") or ""), "raw": r["data"]})
        else:
            raw = {k: v for k, v in r.items() if k != "label"}
            out.append({"label": str(r.get("label") or ""), "raw": raw})
    return out

