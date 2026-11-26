"""Projects and their deliverables: one commissioned piece of work (a job application, a feature, a planning document) and the
pieces it is made of. Domain-free on purpose: what a brief or a spec contains is declared by an office (aiops/offices) and
stored here as JSON.

Like the rest of the library, a project is freely editable until it runs; a run copies what it needs (`snapshot_inputs`) so
later edits never change what a past run did. There are no foreign keys: `delete_project` removes everything that belongs to
the project in one transaction, and `integrity.integrity_problems` audits what is left.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from .db import Database, open_db
from .library import Library, _now


def _j(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


def _need(value: str, what: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{what} is empty")
    return value


DELIVERABLE_FIELDS = ("id", "project_id", "position", "kind", "title", "spec", "final_content", "final_meta", "final_run_id",
                      "final_at", "created_at", "updated_at")


class Projects:
    def __init__(self, db: "str | Database"):
        self.db = open_db(db)
        self.library = Library(self.db)

    # ---- projects ----
    def create_project(self, office: str, title: str, brief: dict | None = None, notes: str = "", settings: dict | None = None) -> str:
        office, title = _need(office, "office"), _need(title, "project title")
        pid, now = uuid.uuid4().hex[:10], _now()
        with self.db.lock:
            self.db.conn.execute("INSERT INTO projects (id, office, title, brief, notes, settings, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                 (pid, office, title, _j(brief or {}), notes, _j(settings or {}), now, now))
        return pid

    def get_project(self, project_id: str) -> dict:
        with self.db.lock:
            r = self.db.conn.execute("SELECT id, office, title, brief, notes, created_at, updated_at, settings FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not r:
            raise KeyError(project_id)
        return {"id": r[0], "office": r[1], "title": r[2], "brief": json.loads(r[3]), "notes": r[4], "created_at": r[5], "updated_at": r[6],
                "settings": json.loads(r[7])}

    def list_projects(self, office: str | None = None) -> list[dict]:
        """Newest edit first, with how many deliverables there are and how many already have an approved final."""
        q = ("SELECT p.id, p.office, p.title, p.brief, p.created_at, p.updated_at, COUNT(d.id), COUNT(d.final_at) FROM projects p "
             "LEFT JOIN deliverables d ON d.project_id = p.id" + (" WHERE p.office = ?" if office else "") + " GROUP BY p.id ORDER BY p.updated_at DESC, p.rowid DESC")  # same millisecond: the later-created one first, not a random id
        with self.db.lock:
            rows = self.db.conn.execute(q, (office,) if office else ()).fetchall()
        return [{"id": a, "office": b, "title": c, "brief": json.loads(d), "created_at": e, "updated_at": f, "deliverables": n, "finals": fin}
                for a, b, c, d, e, f, n, fin in rows]

    def update_project(self, project_id: str, title: str | None = None, brief: dict | None = None, notes: str | None = None,
                       settings: dict | None = None) -> dict:
        cur = self.get_project(project_id)
        with self.db.lock:
            self.db.conn.execute("UPDATE projects SET title = ?, brief = ?, notes = ?, settings = ?, updated_at = ? WHERE id = ?",
                                 (_need(cur["title"] if title is None else title, "project title"),
                                  _j(cur["brief"] if brief is None else brief), cur["notes"] if notes is None else notes,
                                  _j(cur["settings"] if settings is None else settings), _now(), project_id))
        return self.get_project(project_id)

    def delete_project(self, project_id: str) -> dict:
        """The project, its deliverables (with their final text), its resource links and the resources that belong to it alone,
        in one transaction. Run records are history and stay. -> how many rows of each kind went."""
        self.get_project(project_id)
        gone = {}
        with self.db.lock:
            self.db.conn.execute("BEGIN")
            try:
                for name, sql in (("deliverables", "DELETE FROM deliverables WHERE project_id = ?"),
                                  ("project_resources", "DELETE FROM project_resources WHERE project_id = ?"),
                                  ("own_resources", "DELETE FROM resources WHERE owner_project_id = ?"),
                                  ("projects", "DELETE FROM projects WHERE id = ?")):
                    gone[name] = self.db.conn.execute(sql, (project_id,)).rowcount
                self.db.conn.execute("COMMIT")
            except BaseException:
                self.db.conn.execute("ROLLBACK")
                raise
        return gone

    def _touch(self, project_id: str) -> None:
        self.db.conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (_now(), project_id))

    # ---- deliverables ----
    @staticmethod
    def _deliverable(r) -> dict:
        d = dict(zip(DELIVERABLE_FIELDS, r))
        d["spec"], d["final_meta"] = json.loads(d["spec"]), json.loads(d["final_meta"])
        return d

    def add_deliverable(self, project_id: str, kind: str, title: str, spec: dict | None = None) -> str:
        self.get_project(project_id)
        kind, title = _need(kind, "deliverable kind"), _need(title, "deliverable title")
        did, now = uuid.uuid4().hex[:10], _now()
        with self.db.lock:
            pos = self.db.conn.execute("SELECT COALESCE(MAX(position) + 1, 0) FROM deliverables WHERE project_id = ?", (project_id,)).fetchone()[0]
            self.db.conn.execute("INSERT INTO deliverables (id, project_id, position, kind, title, spec, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                 (did, project_id, pos, kind, title, _j(spec or {}), now, now))
            self._touch(project_id)
        return did

    def get_deliverable(self, deliverable_id: str) -> dict:
        with self.db.lock:
            r = self.db.conn.execute(f"SELECT {', '.join(DELIVERABLE_FIELDS)} FROM deliverables WHERE id = ?", (deliverable_id,)).fetchone()
        if not r:
            raise KeyError(deliverable_id)
        return self._deliverable(r)

    def list_deliverables(self, project_id: str) -> list[dict]:
        self.get_project(project_id)
        with self.db.lock:
            rows = self.db.conn.execute(f"SELECT {', '.join(DELIVERABLE_FIELDS)} FROM deliverables WHERE project_id = ? ORDER BY position, id", (project_id,)).fetchall()
        return [self._deliverable(r) for r in rows]

    def update_deliverable(self, deliverable_id: str, title: str | None = None, spec: dict | None = None) -> dict:
        cur = self.get_deliverable(deliverable_id)
        with self.db.lock:
            self.db.conn.execute("UPDATE deliverables SET title = ?, spec = ?, updated_at = ? WHERE id = ?",
                                 (_need(cur["title"] if title is None else title, "deliverable title"), _j(cur["spec"] if spec is None else spec), _now(), deliverable_id))
            self._touch(cur["project_id"])
        return self.get_deliverable(deliverable_id)

    def delete_deliverable(self, deliverable_id: str) -> None:
        cur = self.get_deliverable(deliverable_id)
        with self.db.lock:
            self.db.conn.execute("DELETE FROM deliverables WHERE id = ?", (deliverable_id,))
            self._touch(cur["project_id"])

    def reorder(self, project_id: str, ordered_ids: list[str]) -> list[dict]:
        """Must name every deliverable of the project exactly once."""
        have = [d["id"] for d in self.list_deliverables(project_id)]
        if sorted(ordered_ids) != sorted(have):
            raise ValueError("the new order must list every deliverable of the project exactly once")
        with self.db.lock:
            self.db.conn.execute("BEGIN")
            try:
                for pos, did in enumerate(ordered_ids):
                    self.db.conn.execute("UPDATE deliverables SET position = ? WHERE id = ?", (pos, did))
                self._touch(project_id)
                self.db.conn.execute("COMMIT")
            except BaseException:
                self.db.conn.execute("ROLLBACK")
                raise
        return self.list_deliverables(project_id)

    # ---- the approved final of a deliverable: written when a human approves, editable by a human afterwards ----
    def set_final(self, deliverable_id: str, content: str, meta: dict | None = None, run_id: str = "") -> dict:
        """`meta`: what the code measured at approval (cover letter: the character count). Approving again replaces the final."""
        cur = self.get_deliverable(deliverable_id)
        content = _need(content, "final content")
        with self.db.lock:
            self.db.conn.execute("UPDATE deliverables SET final_content = ?, final_meta = ?, final_run_id = ?, final_at = ?, updated_at = ? WHERE id = ?",
                                 (content, _j(meta or {}), run_id, _now(), _now(), deliverable_id))
            self._touch(cur["project_id"])
        return self.get_deliverable(deliverable_id)

    def edit_final(self, deliverable_id: str, content: str, meta: dict | None = None) -> dict:
        """The human reworks an approved final. Only possible once there is one; the run that produced it stays recorded."""
        cur = self.get_deliverable(deliverable_id)
        if cur["final_content"] is None:
            raise ValueError("there is no approved final to edit yet")
        content = _need(content, "final content")
        with self.db.lock:
            self.db.conn.execute("UPDATE deliverables SET final_content = ?, final_meta = ?, updated_at = ? WHERE id = ?",
                                 (content, _j(cur["final_meta"] if meta is None else meta), _now(), deliverable_id))
            self._touch(cur["project_id"])
        return self.get_deliverable(deliverable_id)

    def clear_final(self, deliverable_id: str) -> dict:
        cur = self.get_deliverable(deliverable_id)
        with self.db.lock:
            self.db.conn.execute("UPDATE deliverables SET final_content = NULL, final_meta = '{}', final_run_id = '', final_at = NULL, updated_at = ? WHERE id = ?",
                                 (_now(), deliverable_id))
            self._touch(cur["project_id"])
        return self.get_deliverable(deliverable_id)

    # ---- the material a project works from ----
    def attach_resource(self, project_id: str, resource_id: str, role: str = "evidence") -> None:
        """Only verified resources, and only my library's or this project's own. (An LLM conversion is unverified until a human
        has checked it, so it cannot sneak in as evidence.)"""
        self.get_project(project_id)
        r = self.library.get_resource(resource_id)  # KeyError if unknown
        if r["owner_project_id"] not in ("", project_id):
            raise ValueError(f"'{r['name']}' belongs to another project")
        if r["review_status"] != "verified":
            raise ValueError(f"검수 전 리소스는 쓸 수 없습니다 (원본과 대조 후 '검수 완료'로 표시하세요): {r['name']}")
        with self.db.lock:
            self.db.conn.execute("INSERT INTO project_resources (project_id, resource_id, role, added_at) VALUES (?, ?, ?, ?) "
                                 "ON CONFLICT(project_id, resource_id) DO UPDATE SET role = excluded.role", (project_id, resource_id, _need(role, "role"), _now()))
            self._touch(project_id)

    def detach_resource(self, project_id: str, resource_id: str) -> None:
        with self.db.lock:
            self.db.conn.execute("DELETE FROM project_resources WHERE project_id = ? AND resource_id = ?", (project_id, resource_id))
            self._touch(project_id)

    def list_project_resources(self, project_id: str) -> list[dict]:
        """What the project uses, without the text: id, name, role, type, chars, review state, whose it is."""
        self.get_project(project_id)
        with self.db.lock:
            rows = self.db.conn.execute(
                "SELECT r.id, r.name, pr.role, r.source_type, length(r.text), r.review_status, r.owner_project_id FROM project_resources pr "
                "JOIN resources r ON r.id = pr.resource_id WHERE pr.project_id = ? ORDER BY pr.added_at, r.id", (project_id,)).fetchall()
        return [dict(zip(("id", "name", "role", "source_type", "chars", "review_status", "owner_project_id"), r)) for r in rows]

    def add_own_resource(self, project_id: str, name: str, text: str, role: str, **kw) -> str:
        """A resource that belongs to this project alone (a job description, a company research) and is attached to it."""
        self.get_project(project_id)
        rid = self.library.add_resource(name, text, owner_project_id=project_id, **kw)
        if self.library.get_resource(rid)["review_status"] == "verified":
            self.attach_resource(project_id, rid, role)
        else:  # unverified (an LLM conversion): stored for the user to check, attached once verified
            with self.db.lock:
                self._touch(project_id)
        return rid

    def projects_using(self, resource_id: str) -> list[dict]:
        with self.db.lock:
            rows = self.db.conn.execute("SELECT p.id, p.title FROM project_resources pr JOIN projects p ON p.id = pr.project_id "
                                        "WHERE pr.resource_id = ? ORDER BY p.title", (resource_id,)).fetchall()
        return [{"id": a, "title": b} for a, b in rows]

    # ---- what a run takes with it ----
    def snapshot_inputs(self, project_id: str) -> dict:
        """Everything a run needs, copied: the project, its deliverables in order, and the text of every attached resource with a
        hash. Refuses if any attached resource is no longer verified (it may have been un-verified after it was attached)."""
        project = self.get_project(project_id)
        resources = []
        attached = self.list_project_resources(project_id)
        self.library.require_verified([a["id"] for a in attached])
        for a in attached:
            text = self.library.get_resource(a["id"])["text"]
            resources.append({"id": a["id"], "name": a["name"], "role": a["role"], "text": text, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
        return {"project": project, "deliverables": [{k: d[k] for k in ("id", "position", "kind", "title", "spec")} for d in self.list_deliverables(project_id)],
                "resources": resources}
