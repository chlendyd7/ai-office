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
