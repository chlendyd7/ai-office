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
from ..core.store import RunStore
from ..core.summary import summarize
from ..llm.hchat import HChatClient
from ..office.view import activity, office_view
from ..workflows import REGISTRY

BACKEND = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).parent / "static"


class StartReq(BaseModel):
    workflow: str
    input: dict
    overrides: dict = Field(default_factory=dict)


class RerunReq(BaseModel):
    overrides: dict = Field(default_factory=dict)


class LLMTestReq(BaseModel):
    model: str


class DecisionReq(BaseModel):
    decision: str
    comment: str = ""
    actor: str = "human"


def _sse(name: str, data) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def create_app(runs_dir: str | Path = BACKEND / "runs", engine: Engine | None = None) -> FastAPI:
    eng = engine or Engine(RunStore(runs_dir), REGISTRY)
    store = eng.store
    app = FastAPI(title="AI Ops")

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
        return [{k: v[k] for k in ("run_id", "workflow", "status", "stop_reason", "job", "score", "rounds")} for v in views]

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
            run_id = eng.create(req.workflow, req.input, req.overrides)
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
            new_id = eng.create(started["workflow"], started["input"], deep_merge(started["config"], req.overrides), parent=run_id)
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
app = create_app()
