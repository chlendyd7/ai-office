"""What foreign keys would have guaranteed, checked on demand instead: rows that point at something that is gone.

There are no foreign keys on purpose (single user, local file, a handful of relations: cascades and delete ordering cost more
than they protect). Deletes go through one function that removes a whole family of rows in one transaction, and this audit
finds anything that slipped through (a crash mid-way, an import of data from an older machine, a bug).
"""
from __future__ import annotations

from .db import Database, open_db

# (message with {0} {1} {2} = the selected columns, query)
CHECKS = [
    ("deliverable {0} belongs to project {1}, which does not exist",
     "SELECT id, project_id FROM deliverables WHERE project_id NOT IN (SELECT id FROM projects)"),
    ("project {0} links to resource {1}, but the project does not exist",
     "SELECT project_id, resource_id FROM project_resources WHERE project_id NOT IN (SELECT id FROM projects)"),
    ("project {0} links to resource {1}, which does not exist",
     "SELECT project_id, resource_id FROM project_resources WHERE resource_id NOT IN (SELECT id FROM resources)"),
    ("resource {0} belongs to project {1}, which does not exist",
     "SELECT id, owner_project_id FROM resources WHERE owner_project_id != '' AND owner_project_id NOT IN (SELECT id FROM projects)"),
    ("project {0} uses resource {1}, which belongs to project {2}",
     "SELECT pr.project_id, pr.resource_id, r.owner_project_id FROM project_resources pr JOIN resources r ON r.id = pr.resource_id "
     "WHERE r.owner_project_id != '' AND r.owner_project_id != pr.project_id"),
    ("dataset item {0} belongs to dataset {1}, which does not exist",
     "SELECT id, dataset_id FROM items WHERE dataset_id NOT IN (SELECT id FROM datasets)"),
    ("resource {0} names conversion run {1}, which has no record",
     "SELECT id, convert_run_id FROM resources WHERE convert_run_id != '' AND convert_run_id NOT IN (SELECT run_id FROM events)"),
    ("deliverable {0} names run {1} as its final's source, which has no record",
     "SELECT id, final_run_id FROM deliverables WHERE final_run_id != '' AND final_run_id NOT IN (SELECT run_id FROM events)"),
]


def integrity_problems(db: "str | Database") -> list[str]:
    db = open_db(db)
    out = []
    with db.lock:
        for message, query in CHECKS:
            for row in db.conn.execute(query):
                out.append(message.format(*row))
    return out
