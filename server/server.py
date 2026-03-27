import asyncio
import json
import threading
import logging
import re
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Request, Form, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
from typing import Optional
import os
import uuid

from PIL import Image
import io

from auth import (
    LOGIN_HTML, SESSION_COOKIE, CUSTOMER_SESSION_COOKIE, SESSION_MAX_AGE,
    is_public_path, is_valid_session, check_password, create_session,
    create_customer_session, validate_customer_session, invalidate_customer_session,
)
from database import init_db, upsert_machine, update_machine_heartbeat, mark_offline_machines
from database import sync_files_for_machine, start_printing, complete_job
from database import get_all_machines, get_jobs_for_machine, get_all_active_jobs
from database import search_jobs, get_completed_jobs, get_daily_stats, get_report
from database import create_nest, unnest, start_nest_printing, complete_nest
from database import get_job_by_id, get_jobs_by_nest, delete_machine
from database import update_machine_warehouse, get_warehouses, create_warehouse, delete_warehouse
from database import (
    create_customer, verify_customer_password, get_customer_by_id, get_all_customers,
    update_customer, deactivate_customer,
    get_customer_balance, add_credit, deduct_credit, get_credit_history,
    create_customer_file, get_customer_files, get_customer_file_by_id,
    delete_customer_file, assign_customer_file_to_machine,
    update_customer_file_status, get_customer_file_by_job_id,
    update_customer_file_copies, get_pending_inches,
)
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


# ── Customer WebSocket manager ──

class CustomerConnectionManager:
    def __init__(self):
        self.active: dict[str, list[WebSocket]] = {}  # customer_id -> [ws]

    async def connect(self, ws: WebSocket, customer_id: str):
        await ws.accept()
        if customer_id not in self.active:
            self.active[customer_id] = []
        self.active[customer_id].append(ws)

    def disconnect(self, ws: WebSocket, customer_id: str):
        if customer_id in self.active:
            if ws in self.active[customer_id]:
                self.active[customer_id].remove(ws)
            if not self.active[customer_id]:
                del self.active[customer_id]

    async def send_to_customer(self, customer_id: str, data: dict):
        if customer_id not in self.active:
            return
        message = json.dumps(data)
        disconnected = []
        for ws in self.active[customer_id]:
            try:
                await ws.send_text(message)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            self.active[customer_id].remove(ws)


customer_manager = CustomerConnectionManager()

# Upload directory
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


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
        # Customer API paths — check customer session
        if path.startswith("/api/customer/"):
            token = request.cookies.get(CUSTOMER_SESSION_COOKIE)
            customer_id = validate_customer_session(token)
            if customer_id:
                request.state.customer_id = customer_id
                return await call_next(request)
            return JSONResponse({"error": "Not authenticated"}, status_code=401)
        # Customer WebSocket
        if path == "/ws/customer":
            return await call_next(request)
        # Admin paths — check admin session cookie
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
        response = RedirectResponse("/admin", status_code=302)
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


class CreateCustomerRequest(BaseModel):
    name: str
    email: str
    password: str
    initial_credit_inches: float = 0


class UpdateCustomerRequest(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None


class CreditRequest(BaseModel):
    amount: float
    reason: str = "manual_adjustment"


class AssignFileRequest(BaseModel):
    machine_id: str


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
        # Handle customer file credit deduction for auto-completed jobs
        for pj in prev_printing_jobs:
            await _handle_customer_file_completion(pj)

    # Update customer file status to 'printing' for the new job
    job = get_job_by_id(job_id)
    if job and job.get("customer_file_id"):
        update_customer_file_status(job["customer_file_id"], "printing")
        await customer_manager.send_to_customer(
            get_customer_file_by_id(job["customer_file_id"])["customer_id"],
            {"type": "file_update", "file_id": job["customer_file_id"], "status": "printing"}
        )

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

        # Handle customer file credit deduction
        await _handle_customer_file_completion(job)

    state = build_dashboard_state()
    await manager.broadcast({"type": "state_update", "machines": state})

    return {"status": "ok"}


async def _handle_customer_file_completion(job: dict):
    """If the completed job is linked to a customer file, update status and deduct credit."""
    customer_file_id = job.get("customer_file_id")
    if not customer_file_id:
        return
    cf = get_customer_file_by_id(customer_file_id)
    if not cf:
        return
    # Update customer file status
    update_customer_file_status(customer_file_id, "completed")
    # Deduct credit: 1 inch = 1 credit
    inches = cf["print_inches"] * cf.get("copies", 1)
    deduct_credit(cf["customer_id"], inches, reference_id=str(job.get("id", "")))
    # Notify customer via WebSocket
    balance = get_customer_balance(cf["customer_id"])
    await customer_manager.send_to_customer(cf["customer_id"], {
        "type": "file_update",
        "file_id": customer_file_id,
        "status": "completed",
        "credit_deducted": inches,
        "balance": balance,
    })


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


# ── Helper: parse copies from filename ──

def _parse_copies(filename: str) -> int:
    match = re.search(r'\((\d+)\s*x\)', filename)
    return int(match.group(1)) if match else 1


# ── Helper: read image info with Pillow ──

def _read_image_info(file_bytes: bytes, filename: str) -> dict:
    try:
        img = Image.open(io.BytesIO(file_bytes))
        width, height = img.size
        dpi = img.info.get("dpi", (300, 300))
        dpi_x = float(dpi[0]) if dpi[0] else 300.0
        dpi_y = float(dpi[1]) if dpi[1] else 300.0
        print_inches = height / dpi_y if dpi_y > 0 else height / 300.0
        copies = _parse_copies(filename)
        return {
            "width_px": width, "height_px": height,
            "dpi_x": dpi_x, "dpi_y": dpi_y,
            "print_inches": round(print_inches, 2),
            "copies": copies,
        }
    except Exception:
        return {
            "width_px": 0, "height_px": 0,
            "dpi_x": 300.0, "dpi_y": 300.0,
            "print_inches": 0, "copies": 1,
        }


# ── Admin Customer Management API ──

@app.post("/api/admin/customers")
async def create_customer_endpoint(req: CreateCustomerRequest):
    try:
        customer = create_customer(req.name, req.email, req.password)
        # Auto-create initial credit if provided
        if req.initial_credit_inches > 0:
            add_credit(customer["id"], req.initial_credit_inches, "manual_adjustment")
        return {"status": "ok", "customer": customer}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/admin/customers")
async def list_customers_endpoint():
    customers = get_all_customers()
    result = []
    for c in customers:
        c["balance"] = get_customer_balance(c["id"])
        # Count files not yet completed
        files = get_customer_files(customer_id=c["id"])
        c["pending_file_count"] = len([f for f in files if f["status"] != "completed"])
        # Remove sensitive fields
        c.pop("password_hash", None)
        c.pop("password_salt", None)
        result.append(c)
    return result


@app.get("/api/admin/customers/{customer_id}")
async def get_customer_detail(customer_id: str):
    customer = get_customer_by_id(customer_id)
    if not customer:
        return JSONResponse({"error": "Customer not found"}, status_code=404)
    customer["balance"] = get_customer_balance(customer_id)
    customer.pop("password_hash", None)
    customer.pop("password_salt", None)
    customer["files"] = get_customer_files(customer_id=customer_id)
    customer["credit_history"] = get_credit_history(customer_id)
    return customer


@app.put("/api/admin/customers/{customer_id}")
async def update_customer_endpoint(customer_id: str, req: UpdateCustomerRequest):
    customer = update_customer(
        customer_id,
        name=req.name, email=req.email,
        password=req.password
    )
    if not customer:
        return JSONResponse({"error": "Customer not found"}, status_code=404)
    customer.pop("password_hash", None)
    customer.pop("password_salt", None)
    return {"status": "ok", "customer": customer}


@app.delete("/api/admin/customers/{customer_id}")
async def delete_customer_endpoint(customer_id: str):
    deactivate_customer(customer_id)
    return {"status": "ok"}


@app.post("/api/admin/customers/{customer_id}/credit")
async def adjust_credit_endpoint(customer_id: str, req: CreditRequest):
    result = add_credit(customer_id, req.amount, req.reason)
    # Notify customer
    await customer_manager.send_to_customer(customer_id, {
        "type": "credit_update",
        "balance": result["balance_after"],
        "amount": req.amount,
        "reason": req.reason,
    })
    return {"status": "ok", **result}


@app.get("/api/admin/customer-files")
async def list_customer_files(
    customer_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None)
):
    return get_customer_files(customer_id=customer_id, status=status)


@app.get("/api/admin/customer-files/{file_id}/download")
async def admin_download_customer_file(file_id: str):
    cf = get_customer_file_by_id(file_id)
    if not cf:
        return JSONResponse({"error": "File not found"}, status_code=404)
    filepath = os.path.join(UPLOAD_DIR, cf["customer_id"], cf["stored_filename"])
    if not os.path.exists(filepath):
        return JSONResponse({"error": "File missing from disk"}, status_code=404)
    return FileResponse(filepath, filename=cf["original_filename"])


@app.put("/api/admin/customer-files/{file_id}/assign")
async def assign_file_endpoint(file_id: str, req: AssignFileRequest):
    # Check credit (soft warning)
    cf = get_customer_file_by_id(file_id)
    if not cf:
        return JSONResponse({"error": "File not found"}, status_code=404)
    balance = get_customer_balance(cf["customer_id"])
    needed = cf["print_inches"] * cf.get("copies", 1)
    warning = None
    if balance < needed:
        warning = f"Insufficient credit: {balance:.1f} inches available, {needed:.1f} needed"

    result = assign_customer_file_to_machine(file_id, req.machine_id)
    if not result:
        return JSONResponse({"error": "Assignment failed"}, status_code=400)

    # Notify customer
    await customer_manager.send_to_customer(cf["customer_id"], {
        "type": "file_update", "file_id": file_id, "status": "queued"
    })

    # Broadcast updated state
    state = build_dashboard_state()
    await manager.broadcast({"type": "state_update", "machines": state})

    return {"status": "ok", "warning": warning, **result}


# ── Customer Portal API ──

@app.post("/api/customer/auth/login")
async def customer_login(request: Request):
    body = await request.json()
    email = body.get("email", "")
    password = body.get("password", "")
    customer = verify_customer_password(email, password)
    if not customer:
        return JSONResponse({"error": "Invalid email or password"}, status_code=401)
    token = create_customer_session(customer["id"])
    response = JSONResponse({
        "status": "ok",
        "customer": {
            "id": customer["id"],
            "name": customer["name"],
            "email": customer["email"],
        }
    })
    response.set_cookie(
        CUSTOMER_SESSION_COOKIE, token,
        max_age=SESSION_MAX_AGE, httponly=True, samesite="lax"
    )
    return response


@app.post("/api/customer/auth/logout")
async def customer_logout(request: Request):
    token = request.cookies.get(CUSTOMER_SESSION_COOKIE)
    if token:
        invalidate_customer_session(token)
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(CUSTOMER_SESSION_COOKIE)
    return response


@app.get("/api/customer/me")
async def customer_me(request: Request):
    customer_id = request.state.customer_id
    customer = get_customer_by_id(customer_id)
    if not customer:
        return JSONResponse({"error": "Not found"}, status_code=404)
    balance = get_customer_balance(customer_id)
    pending = get_pending_inches(customer_id)
    customer["balance"] = balance
    customer["pending_inches"] = round(pending, 1)
    customer["available_balance"] = round(balance - pending, 1)
    customer.pop("password_hash", None)
    customer.pop("password_salt", None)
    return customer


@app.get("/api/customer/credits")
async def customer_credits(request: Request):
    customer_id = request.state.customer_id
    return get_credit_history(customer_id)


@app.get("/api/customer/files")
async def customer_files(request: Request):
    customer_id = request.state.customer_id
    return get_customer_files(customer_id=customer_id)


@app.post("/api/customer/files/upload")
async def customer_upload_file(request: Request, file: UploadFile = File(...)):
    customer_id = request.state.customer_id
    # Validate file type
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in (".png", ".tiff", ".tif"):
        return JSONResponse({"error": "Only PNG and TIFF files are allowed"}, status_code=400)

    # Read file
    file_bytes = await file.read()
    file_size = len(file_bytes)

    # Max 50MB
    if file_size > 50 * 1024 * 1024:
        return JSONResponse({"error": "File too large (max 50MB)"}, status_code=400)

    # Read image info (don't parse copies from filename for customer uploads)
    info = _read_image_info(file_bytes, file.filename)
    info["copies"] = 1  # Customer will set copies manually after upload

    # Check available credit
    balance = get_customer_balance(customer_id)
    pending = get_pending_inches(customer_id)
    available = balance - pending
    file_inches = info["print_inches"] * info["copies"]
    credit_warning = None
    if file_inches > available:
        credit_warning = f"Insufficient credit: {available:.1f} inches available, {file_inches:.1f} needed"

    # Save to disk
    customer_dir = os.path.join(UPLOAD_DIR, customer_id)
    os.makedirs(customer_dir, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex[:12]}{ext}"
    filepath = os.path.join(customer_dir, stored_name)
    with open(filepath, "wb") as f:
        f.write(file_bytes)

    # Save to database
    cf = create_customer_file(
        customer_id=customer_id,
        original_filename=file.filename,
        stored_filename=stored_name,
        file_size=file_size,
        width_px=info["width_px"], height_px=info["height_px"],
        dpi_x=info["dpi_x"], dpi_y=info["dpi_y"],
        print_inches=info["print_inches"], copies=info["copies"],
    )

    # Notify admin dashboard
    await manager.broadcast({
        "type": "customer_file_uploaded",
        "file": cf,
    })

    return {"status": "ok", "file": cf, "credit_warning": credit_warning}


@app.put("/api/customer/files/{file_id}/copies")
async def customer_set_copies(file_id: str, request: Request):
    customer_id = request.state.customer_id
    body = await request.json()
    copies = int(body.get("copies", 1))
    if copies < 1:
        copies = 1
    cf = get_customer_file_by_id(file_id)
    if not cf or cf["customer_id"] != customer_id:
        return JSONResponse({"error": "File not found"}, status_code=404)
    if cf["status"] != "uploaded":
        return JSONResponse({"error": "Cannot change copies after file is in process"}, status_code=400)
    update_customer_file_copies(file_id, copies)
    # Check credit after copies change
    updated = get_customer_file_by_id(file_id)
    balance = get_customer_balance(customer_id)
    pending = get_pending_inches(customer_id)
    available = balance - pending
    warning = None
    if available < 0:
        warning = f"Warning: Exceeds available credit by {abs(available):.1f} inches"
    return {"status": "ok", "copies": copies, "credit_warning": warning}


@app.delete("/api/customer/files/{file_id}")
async def customer_delete_file(file_id: str, request: Request):
    customer_id = request.state.customer_id
    cf = get_customer_file_by_id(file_id)
    if not cf or cf["customer_id"] != customer_id:
        return JSONResponse({"error": "File not found"}, status_code=404)
    if cf["status"] != "uploaded":
        return JSONResponse({"error": "Cannot delete file that is already in process"}, status_code=400)

    # Delete from disk
    filepath = os.path.join(UPLOAD_DIR, customer_id, cf["stored_filename"])
    if os.path.exists(filepath):
        os.remove(filepath)

    delete_customer_file(file_id, customer_id)
    return {"status": "ok"}


# ── Agent customer file download ──

@app.get("/api/agent/customer-files/{file_id}/download")
async def agent_download_customer_file(file_id: str):
    cf = get_customer_file_by_id(file_id)
    if not cf:
        return JSONResponse({"error": "File not found"}, status_code=404)
    filepath = os.path.join(UPLOAD_DIR, cf["customer_id"], cf["stored_filename"])
    if not os.path.exists(filepath):
        return JSONResponse({"error": "File missing"}, status_code=404)
    return FileResponse(filepath, filename=cf["original_filename"])


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


# ── WebSocket for customer portal ──

@app.websocket("/ws/customer")
async def customer_ws(ws: WebSocket):
    # Validate customer session from cookie
    token = ws.cookies.get(CUSTOMER_SESSION_COOKIE)
    customer_id = validate_customer_session(token)
    if not customer_id:
        await ws.close(code=4001)
        return
    await customer_manager.connect(ws, customer_id)
    try:
        # Send initial state
        files = get_customer_files(customer_id=customer_id)
        balance = get_customer_balance(customer_id)
        pending = get_pending_inches(customer_id)
        await ws.send_text(json.dumps({
            "type": "initial_state",
            "files": files,
            "balance": balance,
            "pending_inches": round(pending, 1),
            "available_balance": round(balance - pending, 1),
        }))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        customer_manager.disconnect(ws, customer_id)


# ── Serve dashboard static files ──

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.get("/")
async def serve_landing():
    return FileResponse(os.path.join(STATIC_DIR, "landing.html"))


@app.get("/admin")
async def serve_dashboard():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/customer")
async def serve_customer_portal():
    return FileResponse(os.path.join(STATIC_DIR, "customer.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
