"""HTTP API + Ops console + Office. Run: python -m uvicorn aiops.api.app:app --host 127.0.0.1 --port 8765"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from ..core.config import LLMConfig, deep_merge
from ..core.engine import Engine, GateError
from ..core.env import load_env_file
from ..core.jobqueue import JobQueue, Worker
from ..core.library import Library, parse_import
from ..core.pricing import load_prices, read_pricing_file
from ..core.store import RunStore
from ..core.summary import summarize
from ..llm.hchat import HChatClient
from ..office.view import activity, office_view
from ..workflows import REGISTRY

BACKEND = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).parent / "static"
PRICING_FILE = BACKEND / "pricing.json"


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

    @app.get("/office")
    def office_page():
        return FileResponse(STATIC / "office.html", headers={"Cache-Control": "no-store"})

    # ---- Office surface: limited view + decision only (no process data) ----
    @app.get("/api/office/runs")
    def office_runs():
        views = [office_view(summarize(store.read(rid))) for rid in reversed(store.list_runs())]
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
