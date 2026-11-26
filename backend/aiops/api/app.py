"""HTTP API + Ops console + Office. Run: python -m uvicorn aiops.api.app:app --host 127.0.0.1 --port 8765"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time
from pathlib import Path, PurePath
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from ..core.config import LLMConfig, deep_merge
from ..core.engine import Engine, GateError
from ..core.env import load_env_file
from ..core.convert import DEFAULT_MAX_COST_USD, DEFAULT_MODEL, convert, estimate_max_usd
from ..core.extract import ExtractError, analyze, bad_pages, check_length, ok_text, skip_warnings
from ..core.jobqueue import JobQueue, Worker
from ..core.library import Library, parse_import
from ..core.pricing import load_prices, read_pricing_file
from ..core.projects import Projects
from ..core.store import RunStore
from ..core.summary import summarize
from ..llm.hchat import HChatClient
from ..offices import OFFICES
from ..office.view import activity, office_view
from ..workflows import REGISTRY

BACKEND = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).parent / "static"
PRICING_FILE = BACKEND / "pricing.json"
INTERNAL_WORKFLOWS = {"convert"}  # runs that record a model call for housekeeping (document conversion): not decisions, not for the Office


class StartReq(BaseModel):
    workflow: str
    input: dict
    overrides: dict = Field(default_factory=dict)
    prompt_refs: dict[str, str] = Field(default_factory=dict)  # role -> "name" (latest) or "name@3"; copied into the run


class RerunReq(BaseModel):
    overrides: dict = Field(default_factory=dict)
    prompt_refs: dict[str, str] = Field(default_factory=dict)


class DatasetReq(BaseModel):
    name: str
    workflow: str = "cover_letter"


class ItemReq(BaseModel):
    data: dict
    label: str = ""


class ImportReq(BaseModel):
    format: str  # "csv" | "json"
    text: str = Field(max_length=2_000_000)


class PromptReq(BaseModel):
    workflow: str = "cover_letter"
    role: str
    name: str
    body: str = Field(max_length=100_000)
    note: str = ""


class ResourceReq(BaseModel):  # pasted text
    name: str
    text: str = Field(max_length=400_000)


class ResourceUpdateReq(BaseModel):
    name: str | None = None
    text: str | None = Field(default=None, max_length=400_000)
    verified: bool | None = None  # an explicit "I checked this against the original"; editing the text never sets it


class UploadReq(BaseModel):  # the file travels as base64 inside JSON: no multipart dependency
    filename: str = Field(max_length=300)
    data: str = Field(max_length=15_000_000)
    # What to do with pages that have no usable text layer. "ask" (default) only reports them: no model is called and nothing is
    # stored, because a conversion costs money and its result needs a human check.
    on_bad_pages: Literal["ask", "llm", "skip"] = "ask"
    mode: str = "hchat"  # LLM for a conversion ("fake" only for tests and demos)
    model: str = DEFAULT_MODEL
    max_cost_usd: float = Field(default=DEFAULT_MAX_COST_USD, gt=0, le=1.0)


class ProjectReq(BaseModel):
    office: str
    brief: dict = Field(default_factory=dict)
    notes: str = ""
    settings: dict = Field(default_factory=dict)
    title: str | None = None  # default: the office builds it from the brief ("회사 · 직무")


class ProjectUpdateReq(BaseModel):
    brief: dict | None = None
    settings: dict | None = None
    notes: str | None = None
    title: str | None = None


class DraftReq(BaseModel):
    confirm_real: bool = False  # a run with the real gateway costs money: it must be asked for on purpose


class DeliverableReq(BaseModel):
    kind: str
    title: str = Field(max_length=2000)
    spec: dict = Field(default_factory=dict)


class DeliverableUpdateReq(BaseModel):
    title: str | None = Field(default=None, max_length=2000)
    spec: dict | None = None


class OrderReq(BaseModel):
    ids: list[str]


class FinalReq(BaseModel):
    content: str = Field(max_length=200_000)


class AttachReq(BaseModel):
    resource_id: str
    role: str = "evidence"


class OwnPasteReq(BaseModel):  # text pasted for this project alone (a job description)
    name: str
    text: str = Field(max_length=400_000)
    role: str


class OwnUploadReq(UploadReq):  # a file for this project alone: same conversion rules as the library
    role: str


class DatasetRunReq(BaseModel):
    overrides: dict = Field(default_factory=dict)
    prompt_refs: dict[str, str] = Field(default_factory=dict)
    item_ids: list[str] | None = None  # None = every item in the dataset


class LLMTestReq(BaseModel):
    model: str


class DecisionReq(BaseModel):
    decision: str
    comment: str = ""
    actor: str = "human"


def _sse(name: str, data) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class JobIn(BaseModel):
    workflow: str = "cover_letter"
    input: dict
    overrides: dict = Field(default_factory=dict)
    prompt_refs: dict[str, str] = Field(default_factory=dict)


class EnqueueReq(BaseModel):
    jobs: list[JobIn] = Field(min_length=1, max_length=200)


class ControlReq(BaseModel):
    action: str  # "start" (fresh budget) | "resume" (same session after a manual pause) | "pause"
    budget_usd: float | None = None
    max_pending: int | None = None
    concurrency: int | None = None


def create_app(data_dir: str | Path = BACKEND / "data", engine: Engine | None = None,
               start_worker: bool = False, worker_interval: float = 0.5) -> FastAPI:
    eng = engine or Engine(RunStore(data_dir), REGISTRY, prices=load_prices(PRICING_FILE))
    store = eng.store
    eng.recover_interrupted()  # runs left "running" by a previous process would block the Office and the queue forever
    queue = JobQueue(store.db)  # run record and queue share one database file
    worker = Worker(eng, queue, interval=worker_interval)  # always starts paused, even if jobs are queued
    if start_worker:
        worker.start_thread()
    library = Library(store.db)
    projects = Projects(store.db)
    def file_result(run_id: str) -> None:
        """A human approved a run that came from a project's deliverable: its result becomes that deliverable's final, measured by the office."""
        events = store.read(run_id)
        did = events[0].get("deliverable_id")
        result = summarize(events)["result"]
        if not did or not result:
            return
        try:
            d = projects.get_deliverable(did)
            office = OFFICES[projects.get_project(d["project_id"])["office"]]
        except KeyError:  # the deliverable (or project) was deleted while the run was waiting: the approval stands, there is nowhere to file it
            return
        projects.set_final(did, result["content"], office.measure(d["kind"], d["spec"], result["content"]), run_id)

    eng.on_approved = file_result
    app = FastAPI(title="AI Ops")
    app.state.worker, app.state.queue = worker, queue

    def with_prompts(workflow: str, overrides: dict, refs: dict[str, str]) -> dict:
        """Resolve library prompt references into the run's own copy (text + name@version), so the run never depends on the library again."""
        if not refs:
            return overrides
        try:
            bodies, pinned = library.resolve(workflow, refs)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return deep_merge(overrides, {"prompts": bodies, "prompt_refs": pinned})

    def workflow_or_404(name: str):
        wf = eng.workflows.get(name)
        if not wf:
            raise HTTPException(404, f"unknown workflow: {name}")
        return wf

    def clean_input(workflow: str, raw: dict) -> dict:
        wf = workflow_or_404(workflow)
        try:
            return wf.clean_input(raw) if hasattr(wf, "clean_input") else raw
        except ValueError as e:
            raise HTTPException(400, str(e))

    def dataset_or_404(dataset_id: str) -> dict:
        try:
            return library.get_dataset(dataset_id)
        except KeyError:
            raise HTTPException(404, f"dataset not found: {dataset_id}")

    def item_or_404(item_id: str) -> dict:
        try:
            return library.get_item(item_id)
        except KeyError:
            raise HTTPException(404, f"item not found: {item_id}")

    def events_or_404(run_id: str) -> list[dict]:
        try:
            return store.read(run_id)
        except KeyError:
            raise HTTPException(404, f"run not found: {run_id}")
        except ValueError as e:
            raise HTTPException(400, str(e))

    def tail(run_id: str, request: Request, render):
        """SSE: replay the run record, then follow it. `render(new_events, all_events)` -> list of SSE chunks."""
        events_or_404(run_id)

        async def gen():
            sent, idle = 0, 0
            while not await request.is_disconnected():
                ev = await asyncio.to_thread(store.read, run_id)
                if len(ev) > sent:
                    for chunk in render(ev[sent:], ev):
                        yield chunk
                    sent, idle = len(ev), 0
                elif (idle := idle + 1) % 60 == 0:
                    yield ": ping\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(
            gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )

    @app.get("/")
    def root():
        return RedirectResponse("/office")

    @app.get("/ops")
    def ops_page():
        return FileResponse(STATIC / "ops.html", headers={"Cache-Control": "no-store"})

    @app.get("/projects")
    def projects_page():
        return FileResponse(STATIC / "projects.html", headers={"Cache-Control": "no-store"})

    @app.get("/office")
    def office_page():
        return FileResponse(STATIC / "office.html", headers={"Cache-Control": "no-store"})

    # ---- Office surface: limited view + decision only (no process data) ----
    @app.get("/api/office/runs")
    def office_runs():
        views = [v for v in (office_view(summarize(store.read(rid))) for rid in reversed(store.list_runs())) if v["workflow"] not in INTERNAL_WORKFLOWS]
        return [{**{k: v[k] for k in ("run_id", "workflow", "status", "stop_reason", "job", "score", "rounds")},
                 "tokens": v["usage"]["tokens_in"] + v["usage"]["tokens_out"], "cost_usd": v["usage"]["cost_usd"]} for v in views]

    @app.get("/api/office/runs/{run_id}")
    def office_run(run_id: str):
        return office_view(summarize(events_or_404(run_id)))

    @app.get("/api/office/runs/{run_id}/activity")
    async def office_activity(run_id: str, request: Request):
        def render(new, _all):
            return [_sse("activity", a) for a in map(activity, new) if a]

        return tail(run_id, request, render)

    @app.post("/api/office/runs/{run_id}/decision")
    def office_decide(run_id: str, req: DecisionReq):
        decide(run_id, DecisionReq(decision=req.decision, comment=req.comment, actor="office"))
        return office_view(summarize(store.read(run_id)))

    # ---- continuous operation (queue) ----
    @app.get("/api/queue/status")
    def queue_status():
        return worker.status()

    @app.get("/api/queue")
    def queue_list():
        jobs = queue.jobs()
        queued = [j for j in jobs if j["status"] == "queued"]
        rest = [j for j in jobs if j["status"] != "queued"][-30:]
        return {"control": worker.status(), "jobs": queued + list(reversed(rest))}

    @app.post("/api/queue")
    def queue_add(req: EnqueueReq):
        for i, j in enumerate(req.jobs, 1):  # validate everything first: all or nothing, and fail before any money is at stake
            j.overrides, j.prompt_refs = with_prompts(j.workflow, j.overrides, j.prompt_refs), {}
            try:
                eng.validate(j.workflow, j.overrides)
            except KeyError:
                raise HTTPException(400, f"job {i}: unknown workflow {j.workflow!r}")
            except (ValidationError, ValueError) as e:
                raise HTTPException(400, f"job {i}: {e}")
        return {"job_ids": queue.enqueue([j.model_dump() for j in req.jobs])}

    @app.post("/api/queue/control")
    def queue_control(req: ControlReq):
        if req.action == "start":
            try:
                worker.start(budget_usd=req.budget_usd, max_pending=req.max_pending, concurrency=req.concurrency)
            except ValueError as e:
                raise HTTPException(400, str(e))
        elif req.action == "resume":
            try:
                worker.resume()
            except ValueError as e:
                raise HTTPException(409, str(e))
        elif req.action == "pause":
            worker.pause()
        else:
            raise HTTPException(400, "action must be 'start', 'resume' or 'pause'")
        return worker.status()

    @app.post("/api/queue/clear")
    def queue_clear():
        return {"cancelled": queue.cancel_all()}

    @app.post("/api/queue/{job_id}/cancel")
    def queue_cancel(job_id: str):
        if not queue.cancel(job_id):
            raise HTTPException(404, "no such queued job")
        return {"ok": True}

    @app.get("/api/pricing")
    def pricing():
        return read_pricing_file(PRICING_FILE)

    # ---- LLM connectivity (values of secrets are never returned) ----
    @app.get("/api/llm/status")
    def llm_status():
        missing = [n for n in ("H_CHAT_API_KEY", "H_CHAT_BASE_URL") if not os.environ.get(n)]
        return {"hchat": {"configured": not missing, "missing": missing}}

    @app.post("/api/llm/test")
    def llm_test(req: LLMTestReq):
        """One tiny real call (~20 tokens) so you can check key/URL/model before a full run. Not recorded in any run."""
        try:
            client = HChatClient(LLMConfig(mode="hchat", model=req.model, max_tokens=16, max_retries=0, timeout_s=20))
            t0 = time.perf_counter()
            res = client.complete(agent="test", purpose="test", prompt="Reply with the single word OK.", meta={})
            return {"ok": True, "model": res.model, "tokens_in": res.tokens_in, "tokens_out": res.tokens_out,
                    "latency_ms": int((time.perf_counter() - t0) * 1000), "reply": res.text[:60]}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ---- Ops surface: everything ----
    @app.get("/api/workflows")
    def workflows():
        return {name: wf.default_config() for name, wf in eng.workflows.items()}

    @app.get("/api/workflows/{name}/spec")
    def workflow_spec(name: str):
        """What the screens need to build a form and a prompt editor for this workflow."""
        wf = workflow_or_404(name)
        return {"input_fields": getattr(wf, "input_fields", []), "prompt_roles": getattr(wf, "prompt_roles", {})}

    # ---- library: datasets and versioned prompts ----
    @app.get("/api/library/datasets")
    def datasets_list():
        return library.list_datasets()

    @app.post("/api/library/datasets")
    def datasets_create(req: DatasetReq):
        workflow_or_404(req.workflow)
        try:
            return {"id": library.create_dataset(req.name, req.workflow)}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/library/datasets/{dataset_id}")
    def datasets_delete(dataset_id: str):
        dataset_or_404(dataset_id)
        library.delete_dataset(dataset_id)
        return {"ok": True}

    @app.get("/api/library/datasets/{dataset_id}/items")
    def items_list(dataset_id: str):
        dataset_or_404(dataset_id)
        return library.list_items(dataset_id)

    @app.post("/api/library/datasets/{dataset_id}/items")
    def items_add(dataset_id: str, req: ItemReq):
        ds = dataset_or_404(dataset_id)
        return {"id": library.add_items(dataset_id, [{"data": clean_input(ds["workflow"], req.data), "label": req.label}])[0]}

    @app.post("/api/library/datasets/{dataset_id}/import")
    def items_import(dataset_id: str, req: ImportReq):
        ds = dataset_or_404(dataset_id)
        try:
            rows = parse_import(req.format, req.text)
        except ValueError as e:
            raise HTTPException(400, str(e))
        cleaned = []
        for n, r in enumerate(rows, 1):  # all or nothing: one bad row imports nothing
            try:
                cleaned.append({"label": r["label"], "data": clean_input(ds["workflow"], r["raw"])})
            except HTTPException as e:
                raise HTTPException(400, f"row {n}: {e.detail}")
        return {"added": len(library.add_items(dataset_id, cleaned))}

    @app.put("/api/library/items/{item_id}")
    def items_update(item_id: str, req: ItemReq):
        item = item_or_404(item_id)
        ds = dataset_or_404(item["dataset_id"])
        return library.update_item(item_id, clean_input(ds["workflow"], req.data), req.label)

    @app.delete("/api/library/items/{item_id}")
    def items_delete(item_id: str):
        item_or_404(item_id)
        library.delete_item(item_id)
        return {"ok": True}

    @app.post("/api/library/datasets/{dataset_id}/enqueue")
    def datasets_enqueue(dataset_id: str, req: DatasetRunReq):
        """Queue one job per item (or per chosen item), all with the same settings and prompts. All or nothing."""
        ds = dataset_or_404(dataset_id)
        items = library.list_items(dataset_id)
        if req.item_ids is not None:
            wanted = set(req.item_ids)
            items = [i for i in items if i["id"] in wanted]
        if not items:
            raise HTTPException(400, "no items to queue")
        if len(items) > 200:
            raise HTTPException(400, f"at most 200 jobs per request (got {len(items)})")
        jobs = [JobIn(workflow=ds["workflow"], input=i["data"], overrides=req.overrides, prompt_refs=req.prompt_refs) for i in items]
        return queue_add(EnqueueReq(jobs=jobs))

    @app.get("/api/library/prompts")
    def prompts_list(workflow: str | None = None):
        return library.list_prompts(workflow)

    @app.get("/api/library/prompts/{name}")
    def prompts_get(name: str, version: int | None = None):
        try:
            return library.get_prompt(name, version)
        except KeyError:
            raise HTTPException(404, f"prompt not found: {name}" + (f"@{version}" if version else ""))

    @app.post("/api/library/prompts")
    def prompts_save(req: PromptReq):
        wf = workflow_or_404(req.workflow)
        spec = getattr(wf, "prompt_roles", {}).get(req.role)
        if not spec:
            raise HTTPException(400, f"unknown role {req.role!r} for {req.workflow}")
        from ..workflows.base import check_template  # same check a run applies, so a saved prompt can always be used

        try:
            check_template(req.role, req.body, spec["vars"], spec["required"])
            return {"name": req.name.strip(), "version": library.save_prompt(req.workflow, req.role, req.name, req.body, req.note)}
        except ValueError as e:
            raise HTTPException(400, str(e))

    # ---- resources: the user's own material, as text ----
    def resource_or_404(resource_id: str) -> dict:
        try:
            return library.get_resource(resource_id)
        except KeyError:
            raise HTTPException(404, f"resource not found: {resource_id}")

    @app.get("/api/library/resources")
    def resources_list(owner_project_id: str = ""):
        """My library by default; `owner_project_id` lists what belongs to that project alone (including what still awaits a check)."""
        return library.list_resources(owner_project_id)

    @app.get("/api/library/resources/{resource_id}")
    def resources_get(resource_id: str):
        return resource_or_404(resource_id)

    @app.post("/api/library/resources")
    def resources_paste(req: ResourceReq):
        try:
            return {"id": library.add_resource(req.name, req.text)}
        except ValueError as e:
            raise HTTPException(400, str(e))

    def ingest(req: UploadReq, owner_project_id: str = "", role: str | None = None) -> dict:
        """TXT / MD / PDF / image -> text, stored so the user can read and correct it. Free where the file has a text layer.
        Pages without one (scanned, picture, garbled) are not converted behind the user's back: the default answer lists them with
        a worst-case cost (`needs_conversion`), and the user then picks `llm` (stored as unverified) or `skip`."""
        try:
            raw = base64.b64decode(req.data, validate=True)
        except ValueError:
            raise HTTPException(400, "the file data is not valid base64")
        name = PurePath(req.filename).stem or "resource"
        try:
            a = analyze(req.filename, raw)
            bad = bad_pages(a)
            if not bad:
                text = ok_text(a) if a["kind"] == "pdf" else a["text"]
                pages, warnings, by, run_id, cost = (len(a["pages"]) if a["kind"] == "pdf" else None), [], "text", "", 0.0
            else:
                good = ok_text(a)
                if req.on_bad_pages == "ask":
                    n = {s: [p["n"] for p in a["pages"] if p["status"] == s] for s in ("ok", "empty", "garbled", "image")}
                    return {"needs_conversion": True, "kind": a["kind"], "total_pages": len(a["pages"]), "pages": n, "can_skip": bool(good),
                            "model": req.model, "mode": req.mode, "max_usd": estimate_max_usd(a["kind"], len(bad), req.model, eng.prices)}
                if req.on_bad_pages == "skip":
                    if not good:
                        raise ExtractError("건너뛰면 저장할 글자가 남지 않습니다. LLM 변환을 선택하거나 글자를 복사할 수 있는 파일로 올려 주세요")
                    text, pages, warnings, by, run_id, cost = good, len(a["pages"]), skip_warnings(a), "text", "", 0.0
                else:
                    c = convert(store=store, llm_factory=eng.llm_factory, prices=eng.prices, filename=req.filename, data=raw, analysis=a,
                                model=req.model, mode=req.mode, max_cost_usd=req.max_cost_usd)
                    text, pages, warnings, by, run_id, cost = c["text"], len(a["pages"]), c["warnings"], c["converted_by"], c["run_id"], c["cost_usd"]
            text = check_length(text)
            rid = library.add_resource(name, text, source_name=req.filename, source_type=a["kind"], pages=pages, converted_by=by, convert_run_id=run_id,
                                       owner_project_id=owner_project_id, meta={"intended_role": role} if role else None)
        except ValueError as e:  # ExtractError and ConvertError are ValueErrors
            raise HTTPException(400, str(e))
        return {"id": rid, "name": name, "chars": len(text), "pages": pages, "source_type": a["kind"], "warnings": warnings,
                "converted_by": by, "review_status": "verified" if by == "text" else "unverified", "convert_run_id": run_id, "cost_usd": cost}

    @app.post("/api/library/resources/upload")
    def resources_upload(req: UploadReq):
        return ingest(req)

    @app.put("/api/library/resources/{resource_id}")
    def resources_update(resource_id: str, req: ResourceUpdateReq):
        resource_or_404(resource_id)
        try:
            r = library.update_resource(resource_id, req.name, req.text, req.verified)
            role = r["meta"].get("intended_role")
            if req.verified and r["owner_project_id"] and role:  # a project's own resource, checked: it joins the project as it was meant to
                projects.attach_resource(r["owner_project_id"], resource_id, role)
                r["attached"] = True
            return r
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/library/resources/{resource_id}")
    def resources_delete(resource_id: str):
        resource_or_404(resource_id)
        try:
            library.delete_resource(resource_id)
        except ValueError as e:  # a project still uses it
            raise HTTPException(409, str(e))
        return {"ok": True}

    # ---- offices and their projects ----
    def project_or_404(project_id: str) -> dict:
        try:
            return projects.get_project(project_id)
        except KeyError:
            raise HTTPException(404, f"project not found: {project_id}")

    def office_or_400(key: str):
        office = OFFICES.get(key)
        if not office:
            raise HTTPException(400, f"unknown office {key!r} (offices: {', '.join(OFFICES)})")
        return office

    def deliverable_or_404(deliverable_id: str) -> dict:
        try:
            return projects.get_deliverable(deliverable_id)
        except KeyError:
            raise HTTPException(404, f"deliverable not found: {deliverable_id}")

    def project_view(project_id: str) -> dict:
        p = project_or_404(project_id)
        ds, rs = projects.list_deliverables(project_id), projects.list_project_resources(project_id)
        office = office_or_400(p["office"])
        runs: dict[str, list[dict]] = {}
        for rid in store.runs_with("project_id", project_id):  # newest first
            ev = store.read(rid)
            sm = summarize(ev)
            last = sm["scores"][-1] if sm["scores"] else None
            runs.setdefault(ev[0].get("deliverable_id", ""), []).append({
                "run_id": rid, "status": sm["status"], "stop_reason": sm["stop_reason"], "rounds": sm["rounds"], "cost_usd": sm["totals"]["cost_usd"],
                "score": last["total"] if last else None, "blocked_by": last["blocked_by"] if last else [], "error": sm["error"]})
        # the office says what is still missing; `status` is "draft" until the whole pipeline can run (derived from run records later)
        return {"project": p, "deliverables": ds, "resources": rs, "status": "draft", "runs": runs,
                "settings": office.clean_settings(p["settings"]), "readiness": office.check_project(p["brief"], ds, rs)}

    def clean(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/offices")
    def offices_list():
        return {k: o.declaration() for k, o in OFFICES.items()}

    @app.get("/api/projects")
    def projects_list(office: str | None = None):
        return projects.list_projects(office)

    @app.post("/api/projects")
    def projects_create(req: ProjectReq):
        office = office_or_400(req.office)
        brief = clean(office.clean_brief, req.brief)
        settings = clean(office.clean_settings, req.settings)
        pid = clean(projects.create_project, office.key, req.title or office.title_for(brief), brief, req.notes, settings)
        return project_view(pid)

    @app.get("/api/projects/{project_id}")
    def projects_get(project_id: str):
        return project_view(project_id)

    @app.put("/api/projects/{project_id}")
    def projects_update(project_id: str, req: ProjectUpdateReq):
        office = office_or_400(project_or_404(project_id)["office"])
        brief = clean(office.clean_brief, req.brief) if req.brief is not None else None
        title = req.title if req.title is not None else (office.title_for(brief) if brief is not None else None)
        settings = clean(office.clean_settings, {**project_or_404(project_id)["settings"], **req.settings}) if req.settings is not None else None
        clean(projects.update_project, project_id, title, brief, req.notes, settings)
        return project_view(project_id)

    @app.delete("/api/projects/{project_id}")
    def projects_delete(project_id: str):
        project_or_404(project_id)
        return {"deleted": projects.delete_project(project_id)}

    @app.post("/api/projects/{project_id}/deliverables")
    def deliverables_add(project_id: str, req: DeliverableReq):
        office = office_or_400(project_or_404(project_id)["office"])
        spec = clean(office.clean_spec, req.kind, req.spec)
        clean(projects.add_deliverable, project_id, req.kind, req.title, spec)
        return project_view(project_id)

    @app.post("/api/projects/{project_id}/deliverables/order")
    def deliverables_order(project_id: str, req: OrderReq):
        project_or_404(project_id)
        clean(projects.reorder, project_id, req.ids)
        return project_view(project_id)

    @app.put("/api/deliverables/{deliverable_id}")
    def deliverables_update(deliverable_id: str, req: DeliverableUpdateReq):
        d = deliverable_or_404(deliverable_id)
        office = office_or_400(project_or_404(d["project_id"])["office"])
        spec = clean(office.clean_spec, d["kind"], req.spec) if req.spec is not None else None
        clean(projects.update_deliverable, deliverable_id, req.title, spec)
        return project_view(d["project_id"])

    @app.delete("/api/deliverables/{deliverable_id}")
    def deliverables_delete(deliverable_id: str):
        d = deliverable_or_404(deliverable_id)
        projects.delete_deliverable(deliverable_id)
        return project_view(d["project_id"])

    @app.put("/api/deliverables/{deliverable_id}/final")
    def final_edit(deliverable_id: str, req: FinalReq):
        """The human reworks an approved final; the code re-measures it (a cover letter: its length by the question's counting rule)."""
        d = deliverable_or_404(deliverable_id)
        office = office_or_400(project_or_404(d["project_id"])["office"])
        clean(projects.edit_final, deliverable_id, req.content, clean(office.measure, d["kind"], d["spec"], req.content))
        return project_view(d["project_id"])

    @app.delete("/api/deliverables/{deliverable_id}/final")
    def final_clear(deliverable_id: str):
        d = deliverable_or_404(deliverable_id)
        projects.clear_final(deliverable_id)
        return project_view(d["project_id"])

    @app.post("/api/projects/{project_id}/deliverables/{deliverable_id}/draft")
    def deliverable_draft(project_id: str, deliverable_id: str, req: DraftReq):
        """Start writing one deliverable (a cover-letter answer) from the project's verified material, with the project's settings.
        Nothing starts if the office says the project is not ready; a run on the real gateway must be confirmed (`confirm_real`)."""
        p = project_or_404(project_id)
        office = office_or_400(p["office"])
        d = deliverable_or_404(deliverable_id)
        if d["project_id"] != project_id:
            raise HTTPException(404, f"deliverable not found in this project: {deliverable_id}")
        ready = office.check_project(p["brief"], projects.list_deliverables(project_id), projects.list_project_resources(project_id))
        if ready["blocking"]:
            raise HTTPException(400, "시작할 수 없습니다: " + "; ".join(ready["blocking"]))
        settings = clean(office.clean_settings, p["settings"])
        if settings["mode"] == "hchat" and not req.confirm_real:
            raise HTTPException(400, f"실제 LLM 호출은 비용이 듭니다 (문항 하나 예산 ${settings['budget_usd']}). confirm_real=true 로 확인해 주세요")
        workflow = clean(office.kind, d["kind"])["workflow"]
        inp = clean(office.input_for, p, d, clean(projects.snapshot_inputs, project_id))
        overrides = office.overrides_for(settings)
        refs = {role: settings[f"prompt_{role}"] for role in ("writer", "reviewer") if settings.get(f"prompt_{role}")}
        try:
            run_id = eng.create(workflow, inp, with_prompts(workflow, overrides, refs), link={"project_id": project_id, "deliverable_id": deliverable_id})
        except (ValidationError, ValueError) as e:
            raise HTTPException(400, str(e))
        threading.Thread(target=eng.run_pass, args=(run_id,), daemon=True).start()
        return {"run_id": run_id, "mode": settings["mode"], "budget_usd": settings["budget_usd"]}

    @app.post("/api/projects/{project_id}/resources")
    def project_attach(project_id: str, req: AttachReq):
        office = office_or_400(project_or_404(project_id)["office"])
        clean(office.check_role, req.role)
        try:
            projects.attach_resource(project_id, req.resource_id, req.role)
        except KeyError:
            raise HTTPException(404, f"resource not found: {req.resource_id}")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return project_view(project_id)

    @app.delete("/api/projects/{project_id}/resources/{resource_id}")
    def project_detach(project_id: str, resource_id: str):
        project_or_404(project_id)
        projects.detach_resource(project_id, resource_id)
        return project_view(project_id)

    @app.post("/api/projects/{project_id}/resources/paste")
    def project_paste(project_id: str, req: OwnPasteReq):
        office = office_or_400(project_or_404(project_id)["office"])
        clean(office.check_role, req.role)
        clean(projects.add_own_resource, project_id, req.name, req.text, req.role, meta={"intended_role": req.role})
        return project_view(project_id)

    @app.post("/api/projects/{project_id}/resources/upload")
    def project_upload(project_id: str, req: OwnUploadReq):
        """A file for this project alone (say, the job description as an image). Text it could read is attached at once; what a model
        converted is kept unverified and joins the project only after a human has checked it (see PUT /api/library/resources/{id})."""
        office = office_or_400(project_or_404(project_id)["office"])
        clean(office.check_role, req.role)
        out = ingest(req, owner_project_id=project_id, role=req.role)
        if out.get("id") and out["review_status"] == "verified":
            projects.attach_resource(project_id, out["id"], req.role)
        return {**out, "attached": bool(out.get("id")) and out["review_status"] == "verified"}

    @app.get("/api/runs")
    def list_runs():
        out = []
        for rid in reversed(store.list_runs()):
            s = summarize(store.read(rid))
            out.append({
                "run_id": rid, "workflow": s["workflow"], "status": s["status"], "stop_reason": s["stop_reason"],
                "rounds": s["rounds"], "tokens": s["totals"]["tokens_in"] + s["totals"]["tokens_out"],
                "cost_usd": s["totals"]["cost_usd"], "last_score": s["scores"][-1]["total"] if s["scores"] else None,
            })
        return out

    @app.post("/api/runs")
    def start_run(req: StartReq):
        try:
            run_id = eng.create(req.workflow, req.input, with_prompts(req.workflow, req.overrides, req.prompt_refs))
        except KeyError:
            raise HTTPException(404, f"unknown workflow: {req.workflow}")
        except (ValidationError, NotImplementedError, ValueError) as e:
            raise HTTPException(400, str(e))
        threading.Thread(target=eng.run_pass, args=(run_id,), daemon=True).start()
        return {"run_id": run_id}

    @app.post("/api/runs/{run_id}/rerun")
    def rerun(run_id: str, req: RerunReq):
        """Same input and settings snapshot (optionally with overrides) as a new run, to compare against the original."""
        started = events_or_404(run_id)[0]
        if started["workflow"] in INTERNAL_WORKFLOWS:
            raise HTTPException(400, "a document conversion is not re-run from here: upload the file again from the library")
        try:
            overrides = with_prompts(started["workflow"], req.overrides, req.prompt_refs)
            new_id = eng.create(started["workflow"], started["input"], deep_merge(started["config"], overrides), parent=run_id)
        except (ValidationError, ValueError) as e:
            raise HTTPException(400, str(e))
        threading.Thread(target=eng.run_pass, args=(new_id,), daemon=True).start()
        return {"run_id": new_id}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        ev = events_or_404(run_id)
        return {"summary": summarize(ev), "events": ev}

    @app.post("/api/runs/{run_id}/decision")
    def decide(run_id: str, req: DecisionReq):
        events_or_404(run_id)
        try:
            eng.decide(run_id, req.decision, req.comment, req.actor)  # a "revise" runs the next pass before returning
        except GateError as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return summarize(store.read(run_id))

    @app.get("/api/runs/{run_id}/stream")
    async def stream(run_id: str, request: Request):
        def render(new, all_events):
            return [_sse("event", e) for e in new] + [_sse("summary", summarize(all_events))]

        return tail(run_id, request, render)

    return app


load_env_file(BACKEND / ".env")  # optional local file (git-ignored); real environment variables win
app = create_app(start_worker=True)
