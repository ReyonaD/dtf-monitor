import asyncio
import json
import threading
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Request, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
from typing import Optional
import os

from auth import (
    LOGIN_HTML, SESSION_COOKIE, SESSION_MAX_AGE,
    is_public_path, is_valid_session, check_password, create_session,
)
from database import init_db, upsert_machine, update_machine_heartbeat, mark_offline_machines
from database import sync_files_for_machine, start_printing, complete_job
from database import get_all_machines, get_jobs_for_machine, get_all_active_jobs
from database import search_jobs, get_completed_jobs, get_daily_stats, get_report
from database import create_nest, unnest, start_nest_printing, complete_nest
from database import get_job_by_id, get_jobs_by_nest, delete_machine
from database import update_machine_warehouse, get_warehouses, create_warehouse, delete_warehouse
from google_sheets import update_orders_for_jobs

logger = logging.getLogger(__name__)


# ── WebSocket manager ──

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)

    async def broadcast(self, data: dict):
        message = json.dumps(data)
        disconnected = []
        for ws in self.active:
            try:
                await ws.send_text(message)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            self.active.remove(ws)


manager = ConnectionManager()


# ── Background task: mark machines offline ──

async def offline_checker():
    while True:
        await asyncio.sleep(15)
        mark_offline_machines(timeout_seconds=30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(offline_checker())
    yield
    task.cancel()


app = FastAPI(title="DTF Floor Monitor", lifespan=lifespan)


# ── Auth middleware ──

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Let public paths (agent APIs, login) through
        if is_public_path(path):
            return await call_next(request)
        # Check session cookie
        token = request.cookies.get(SESSION_COOKIE)
        if is_valid_session(token):
            return await call_next(request)
        # Not authenticated → redirect to login
        return RedirectResponse("/login", status_code=302)

app.add_middleware(AuthMiddleware)


# ── Login endpoints ──

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return LOGIN_HTML


@app.post("/login")
async def login_submit(password: str = Form(...)):
    if check_password(password):
        token = create_session()
        response = RedirectResponse("/", status_code=302)
        response.set_cookie(
            SESSION_COOKIE, token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
        )
        return response
    return RedirectResponse("/login?error=1", status_code=302)


# ── Pydantic models ──

class FileInfo(BaseModel):
    filename: str
    filepath: str = ""
    width_px: int = 0
    height_px: int = 0
    dpi_x: float = 0
    dpi_y: float = 0
    print_inches: float = 0
    copies: int = 1
    nest_group: Optional[str] = None
    source: Optional[str] = None


class HeartbeatRequest(BaseModel):
    machine_id: str
    machine_name: str
    watched_folder: str = ""
    operator: str = ""
    files: list[FileInfo] = []


class PrintActionRequest(BaseModel):
    machine_id: str


class NestRequest(BaseModel):
    machine_id: str
    job_ids: list[int]


class UnnestRequest(BaseModel):
    machine_id: str
    nest_group: str


class WarehouseRequest(BaseModel):
    warehouse: str


# ── Helper to build full dashboard state ──

def build_dashboard_state(warehouse: Optional[str] = None):
    machines = get_all_machines(warehouse)
    stats = get_daily_stats(warehouse=warehouse)
    stats_map = {s["id"]: s for s in stats}

    result = []
    for m in machines:
        jobs = get_jobs_for_machine(m["id"])
        s = stats_map.get(m["id"], {"total_jobs": 0, "total_inches": 0})
        printing = [j for j in jobs if j["status"] == "printing"]
        queued = [j for j in jobs if j["status"] == "queued"]
        total_queued_inches = sum(j["print_inches"] * j.get("copies", 1) for j in queued)
        printing_inches = sum(j["print_inches"] * j.get("copies", 1) for j in printing)

        # Check if printing jobs are a nest
        printing_nest = None
        if printing and printing[0].get("nest_group"):
            printing_nest = printing[0]["nest_group"]

        result.append({
            "machine": m,
            "printing": printing[0] if printing else None,
            "printing_all": printing,  # All printing jobs (for nest display)
            "printing_nest": printing_nest,
            "queued": queued,
            "total_queued_inches": round(total_queued_inches + printing_inches, 1),
            "today_jobs": s["total_jobs"],
            "today_inches": round(s["total_inches"], 1),
        })
    return result


# ── API endpoints ──

@app.post("/api/heartbeat")
async def heartbeat(req: HeartbeatRequest):
    upsert_machine(req.machine_id, req.machine_name, req.watched_folder, req.operator)
    sync_files_for_machine(req.machine_id, [f.model_dump() for f in req.files])
    update_machine_heartbeat(req.machine_id)

    # Broadcast updated state to all dashboard clients
    state = build_dashboard_state()
    await manager.broadcast({"type": "state_update", "machines": state})

    # Return current jobs for this machine (so agent can show them)
    jobs = get_jobs_for_machine(req.machine_id)
    return {"status": "ok", "jobs": jobs}


@app.post("/api/jobs/{job_id}/start")
async def start_job(job_id: int, req: PrintActionRequest):
    # Get info about currently printing jobs BEFORE starting new one
    # (these will be auto-completed)
    prev_printing = get_jobs_for_machine(req.machine_id)
    prev_printing_jobs = [j for j in prev_printing if j["status"] == "printing"]

    prev_id = start_printing(job_id, req.machine_id)

    # Update Google Sheet for auto-completed jobs
    if prev_printing_jobs:
        machines = get_all_machines()
        machine_info = next((m for m in machines if m["id"] == req.machine_id), {})
        threading.Thread(
            target=update_orders_for_jobs,
            args=(prev_printing_jobs, machine_info.get("name", ""), machine_info.get("operator", "")),
            daemon=True,
        ).start()

    state = build_dashboard_state()
    await manager.broadcast({"type": "state_update", "machines": state})

    return {"status": "ok", "auto_completed_job_id": prev_id}


@app.post("/api/jobs/{job_id}/complete")
async def complete_job_endpoint(job_id: int):
    # Get job info BEFORE completing (need filename + machine for sheet update)
    job = get_job_by_id(job_id)
    complete_job(job_id)

    # Update Google Sheet
    if job:
        # Get operator from machine
        machines = get_all_machines()
        machine_info = next((m for m in machines if m["id"] == job.get("machine_id")), {})
        operator = machine_info.get("operator", "")

        # If part of a nest, update all jobs in the nest
        if job.get("nest_group"):
            nest_jobs = get_jobs_by_nest(job["nest_group"])
            jobs_to_update = nest_jobs
        else:
            jobs_to_update = [job]
        threading.Thread(
            target=update_orders_for_jobs,
            args=(jobs_to_update, job.get("machine_name", ""), operator),
            daemon=True,
        ).start()

    state = build_dashboard_state()
    await manager.broadcast({"type": "state_update", "machines": state})

    return {"status": "ok"}


@app.post("/api/nest")
async def create_nest_endpoint(req: NestRequest):
    nest_id = create_nest(req.job_ids, req.machine_id)
    if not nest_id:
        return {"status": "error", "message": "Need at least 2 jobs to nest"}

    state = build_dashboard_state()
    await manager.broadcast({"type": "state_update", "machines": state})

    jobs = get_jobs_for_machine(req.machine_id)
    return {"status": "ok", "nest_group": nest_id, "jobs": jobs}


@app.post("/api/unnest")
async def unnest_endpoint(req: UnnestRequest):
    unnest(req.nest_group, req.machine_id)

    state = build_dashboard_state()
    await manager.broadcast({"type": "state_update", "machines": state})

    jobs = get_jobs_for_machine(req.machine_id)
    return {"status": "ok", "jobs": jobs}


@app.get("/api/machines")
async def list_machines(warehouse: Optional[str] = Query(None)):
    return build_dashboard_state(warehouse)


@app.get("/api/search")
async def search(q: str = Query(..., min_length=1), warehouse: Optional[str] = Query(None)):
    results = search_jobs(q, warehouse)
    return {"results": results}


@app.get("/api/history")
async def history(limit: int = 50, warehouse: Optional[str] = Query(None)):
    return get_completed_jobs(limit, warehouse)


@app.get("/api/stats/daily")
async def daily_stats():
    return get_daily_stats()


@app.get("/api/reports")
async def reports(start: str = Query(...), end: str = Query(...), warehouse: Optional[str] = Query(None)):
    return get_report(start, end, warehouse)


@app.delete("/api/machines/{machine_id}")
async def delete_machine_endpoint(machine_id: str):
    delete_machine(machine_id)
    state = build_dashboard_state()
    await manager.broadcast({"type": "state_update", "machines": state})
    return {"status": "ok"}


@app.get("/api/warehouses")
async def list_warehouses():
    return get_warehouses()


@app.post("/api/warehouses")
async def create_warehouse_endpoint(req: WarehouseRequest):
    create_warehouse(req.warehouse)
    return {"status": "ok", "warehouses": get_warehouses()}


@app.delete("/api/warehouses/{warehouse_name}")
async def delete_warehouse_endpoint(warehouse_name: str):
    delete_warehouse(warehouse_name)
    state = build_dashboard_state()
    await manager.broadcast({"type": "state_update", "machines": state})
    return {"status": "ok", "warehouses": get_warehouses()}


@app.put("/api/machines/{machine_id}/warehouse")
async def set_machine_warehouse(machine_id: str, req: WarehouseRequest):
    update_machine_warehouse(machine_id, req.warehouse)
    state = build_dashboard_state()
    await manager.broadcast({"type": "state_update", "machines": state})
    return {"status": "ok"}


# ── WebSocket for live dashboard ──

@app.websocket("/ws/dashboard")
async def dashboard_ws(ws: WebSocket):
    await manager.connect(ws)
    try:
        # Send initial state
        state = build_dashboard_state()
        await ws.send_text(json.dumps({"type": "state_update", "machines": state}))
        # Keep alive — listen for pings
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ── Serve dashboard static files ──

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.get("/")
async def serve_dashboard():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
