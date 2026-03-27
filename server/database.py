import sqlite3
import os
import uuid
import hashlib
import secrets
from datetime import datetime, date
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "dtf_monitor.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS machines (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            watched_folder TEXT DEFAULT '',
            operator TEXT DEFAULT '',
            last_seen TEXT,
            is_online INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS print_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            machine_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            filepath TEXT DEFAULT '',
            width_px INTEGER DEFAULT 0,
            height_px INTEGER DEFAULT 0,
            dpi_x REAL DEFAULT 0,
            dpi_y REAL DEFAULT 0,
            print_inches REAL DEFAULT 0,
            copies INTEGER DEFAULT 1,
            status TEXT DEFAULT 'queued',
            nest_group TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            started_at TEXT,
            completed_at TEXT,
            FOREIGN KEY (machine_id) REFERENCES machines(id)
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_machine ON print_jobs(machine_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON print_jobs(status);
        CREATE INDEX IF NOT EXISTS idx_jobs_filename ON print_jobs(filename);
        CREATE INDEX IF NOT EXISTS idx_jobs_nest ON print_jobs(nest_group);

        CREATE TABLE IF NOT EXISTS warehouses (
            name TEXT PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS customers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            monthly_credit_inches REAL DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS credit_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id TEXT NOT NULL,
            amount REAL NOT NULL,
            balance_after REAL NOT NULL,
            reason TEXT NOT NULL,
            reference_id TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );
        CREATE INDEX IF NOT EXISTS idx_ledger_customer ON credit_ledger(customer_id);

        CREATE TABLE IF NOT EXISTS customer_files (
            id TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            file_size INTEGER DEFAULT 0,
            width_px INTEGER DEFAULT 0,
            height_px INTEGER DEFAULT 0,
            dpi_x REAL DEFAULT 0,
            dpi_y REAL DEFAULT 0,
            print_inches REAL DEFAULT 0,
            copies INTEGER DEFAULT 1,
            status TEXT DEFAULT 'uploaded',
            print_job_id INTEGER,
            uploaded_at TEXT DEFAULT (datetime('now')),
            completed_at TEXT,
            FOREIGN KEY (customer_id) REFERENCES customers(id),
            FOREIGN KEY (print_job_id) REFERENCES print_jobs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_cfiles_customer ON customer_files(customer_id);
        CREATE INDEX IF NOT EXISTS idx_cfiles_status ON customer_files(status);
    """)
    # Migration: add copies column if missing
    try:
        conn.execute("ALTER TABLE print_jobs ADD COLUMN copies INTEGER DEFAULT 1")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Migration: add operator column to machines if missing
    try:
        conn.execute("ALTER TABLE machines ADD COLUMN operator TEXT DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Migration: add warehouse column to machines if missing
    try:
        conn.execute("ALTER TABLE machines ADD COLUMN warehouse TEXT DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Migration: add customer_file_id to print_jobs
    try:
        conn.execute("ALTER TABLE print_jobs ADD COLUMN customer_file_id TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    conn.commit()
    conn.close()


# ── Machine operations ──

def upsert_machine(machine_id: str, name: str, watched_folder: str = "", operator: str = ""):
    conn = get_connection()
    # If a different machine_id registers with the same name, remove the old one
    old = conn.execute(
        "SELECT id FROM machines WHERE name = ? AND id != ?", (name, machine_id)
    ).fetchall()
    for row in old:
        conn.execute("DELETE FROM print_jobs WHERE machine_id = ?", (row["id"],))
        conn.execute("DELETE FROM machines WHERE id = ?", (row["id"],))

    conn.execute("""
        INSERT INTO machines (id, name, watched_folder, operator, last_seen, is_online)
        VALUES (?, ?, ?, ?, datetime('now'), 1)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            watched_folder = excluded.watched_folder,
            operator = excluded.operator,
            last_seen = datetime('now'),
            is_online = 1
    """, (machine_id, name, watched_folder, operator))
    conn.commit()
    conn.close()


def update_machine_heartbeat(machine_id: str):
    conn = get_connection()
    conn.execute("""
        UPDATE machines SET last_seen = datetime('now'), is_online = 1
        WHERE id = ?
    """, (machine_id,))
    conn.commit()
    conn.close()


def mark_offline_machines(timeout_seconds: int = 30):
    conn = get_connection()
    conn.execute("""
        UPDATE machines SET is_online = 0
        WHERE last_seen < datetime('now', ? || ' seconds')
    """, (f"-{timeout_seconds}",))
    conn.commit()
    conn.close()


def get_all_machines(warehouse: Optional[str] = None):
    conn = get_connection()
    if warehouse:
        rows = conn.execute(
            "SELECT * FROM machines WHERE warehouse = ? ORDER BY name", (warehouse,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM machines ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_machine(machine_id: str):
    """Delete a machine and all its print jobs from the database."""
    conn = get_connection()
    conn.execute("DELETE FROM print_jobs WHERE machine_id = ?", (machine_id,))
    conn.execute("DELETE FROM machines WHERE id = ?", (machine_id,))
    conn.commit()
    conn.close()


def update_machine_warehouse(machine_id: str, warehouse: str):
    """Assign a machine to a warehouse."""
    conn = get_connection()
    conn.execute("UPDATE machines SET warehouse = ? WHERE id = ?", (warehouse, machine_id))
    conn.commit()
    conn.close()


def get_warehouses():
    """Get all warehouse names (from warehouses table)."""
    conn = get_connection()
    rows = conn.execute("SELECT name FROM warehouses ORDER BY name").fetchall()
    conn.close()
    return [r["name"] for r in rows]


def create_warehouse(name: str):
    """Create a new warehouse."""
    conn = get_connection()
    conn.execute("INSERT OR IGNORE INTO warehouses (name) VALUES (?)", (name,))
    conn.commit()
    conn.close()


def delete_warehouse(name: str):
    """Delete a warehouse. Unassigns all machines from it."""
    conn = get_connection()
    conn.execute("UPDATE machines SET warehouse = '' WHERE warehouse = ?", (name,))
    conn.execute("DELETE FROM warehouses WHERE name = ?", (name,))
    conn.commit()
    conn.close()


# ── Print job operations ──

def sync_files_for_machine(machine_id: str, files: list[dict]):
    """Sync the file list from an agent. Add new files, remove deleted ones."""
    conn = get_connection()

    # Get ALL jobs for this machine (including completed) to avoid re-adding printed files
    existing_active = conn.execute("""
        SELECT id, filename, filepath, status FROM print_jobs
        WHERE machine_id = ? AND status IN ('queued', 'printing')
    """, (machine_id,)).fetchall()

    # Also get completed/removed jobs to know which files were already processed
    existing_done = conn.execute("""
        SELECT filepath FROM print_jobs
        WHERE machine_id = ? AND status IN ('completed', 'removed')
    """, (machine_id,)).fetchall()

    active_map = {r["filepath"]: dict(r) for r in existing_active}
    done_paths = {r["filepath"] for r in existing_done}
    incoming_paths = {f["filepath"] for f in files}

    # Remove active jobs for files that no longer exist on the PC
    for filepath, job in active_map.items():
        if filepath not in incoming_paths:
            conn.execute("""
                UPDATE print_jobs SET status = 'removed', completed_at = datetime('now')
                WHERE id = ?
            """, (job["id"],))

    # Add only truly new files (not already active AND not already completed)
    for f in files:
        if f["filepath"] not in active_map and f["filepath"] not in done_paths:
            nest_group = f.get("nest_group")
            copies = f.get("copies", 1)
            conn.execute("""
                INSERT INTO print_jobs (machine_id, filename, filepath, width_px, height_px, dpi_x, dpi_y, print_inches, copies, status, nest_group)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
            """, (
                machine_id, f["filename"], f["filepath"],
                f.get("width_px", 0), f.get("height_px", 0),
                f.get("dpi_x", 0), f.get("dpi_y", 0),
                f.get("print_inches", 0),
                copies, nest_group
            ))

    conn.commit()
    conn.close()


def start_printing(job_id: int, machine_id: str) -> Optional[int]:
    """Mark a job as printing. If it's part of a nest, start the whole nest.
    Returns the ID of the previously printing job (if auto-completed)."""
    conn = get_connection()

    # Check if this job is part of a nest
    job = conn.execute("SELECT nest_group FROM print_jobs WHERE id = ?", (job_id,)).fetchone()

    # Auto-complete any currently printing jobs on this machine
    prev_rows = conn.execute("""
        SELECT id, nest_group FROM print_jobs
        WHERE machine_id = ? AND status = 'printing'
    """, (machine_id,)).fetchall()

    prev_id = None
    completed_nests = set()
    for p in prev_rows:
        prev_id = p["id"]
        conn.execute("""
            UPDATE print_jobs SET status = 'completed', completed_at = datetime('now')
            WHERE id = ?
        """, (p["id"],))
        # If previous printing job was part of a nest, complete the whole nest
        if p["nest_group"] and p["nest_group"] not in completed_nests:
            completed_nests.add(p["nest_group"])
            conn.execute("""
                UPDATE print_jobs SET status = 'completed', completed_at = datetime('now')
                WHERE nest_group = ? AND status = 'printing'
            """, (p["nest_group"],))

    # If this job is part of a nest, start the whole nest
    if job and job["nest_group"]:
        conn.execute("""
            UPDATE print_jobs SET status = 'printing', started_at = datetime('now')
            WHERE nest_group = ? AND machine_id = ? AND status = 'queued'
        """, (job["nest_group"], machine_id))
    else:
        conn.execute("""
            UPDATE print_jobs SET status = 'printing', started_at = datetime('now')
            WHERE id = ? AND machine_id = ?
        """, (job_id, machine_id))

    conn.commit()
    conn.close()
    return prev_id


def complete_job(job_id: int):
    conn = get_connection()
    conn.execute("""
        UPDATE print_jobs SET status = 'completed', completed_at = datetime('now')
        WHERE id = ?
    """, (job_id,))
    conn.commit()
    conn.close()


def create_nest(job_ids: list[int], machine_id: str) -> Optional[str]:
    """Group multiple queued jobs into a nest. Returns nest_group ID."""
    if len(job_ids) < 2:
        return None
    conn = get_connection()
    nest_id = str(uuid.uuid4())[:8]
    placeholders = ",".join("?" * len(job_ids))
    conn.execute(f"""
        UPDATE print_jobs SET nest_group = ?
        WHERE id IN ({placeholders}) AND machine_id = ? AND status = 'queued'
    """, [nest_id] + job_ids + [machine_id])
    conn.commit()
    conn.close()
    return nest_id


def unnest(nest_group: str, machine_id: str):
    """Remove nest grouping, making files individual again."""
    conn = get_connection()
    conn.execute("""
        UPDATE print_jobs SET nest_group = NULL
        WHERE nest_group = ? AND machine_id = ? AND status = 'queued'
    """, (nest_group, machine_id))
    conn.commit()
    conn.close()


def start_nest_printing(nest_group: str, machine_id: str) -> Optional[int]:
    """Mark all jobs in a nest as printing. Auto-completes previous printing jobs."""
    conn = get_connection()

    # Auto-complete any currently printing jobs on this machine
    prev = conn.execute("""
        SELECT id FROM print_jobs
        WHERE machine_id = ? AND status = 'printing'
    """, (machine_id,)).fetchall()

    prev_id = None
    for p in prev:
        prev_id = p["id"]
        conn.execute("""
            UPDATE print_jobs SET status = 'completed', completed_at = datetime('now')
            WHERE id = ?
        """, (p["id"],))

    # Start all jobs in the nest
    conn.execute("""
        UPDATE print_jobs SET status = 'printing', started_at = datetime('now')
        WHERE nest_group = ? AND machine_id = ? AND status = 'queued'
    """, (nest_group, machine_id))

    conn.commit()
    conn.close()
    return prev_id


def complete_nest(nest_group: str):
    """Complete all jobs in a nest group."""
    conn = get_connection()
    conn.execute("""
        UPDATE print_jobs SET status = 'completed', completed_at = datetime('now')
        WHERE nest_group = ? AND status = 'printing'
    """, (nest_group,))
    conn.commit()
    conn.close()


def get_job_by_id(job_id: int):
    conn = get_connection()
    row = conn.execute("""
        SELECT pj.*, m.name as machine_name FROM print_jobs pj
        JOIN machines m ON pj.machine_id = m.id
        WHERE pj.id = ?
    """, (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_jobs_by_nest(nest_group: str):
    conn = get_connection()
    rows = conn.execute("""
        SELECT pj.*, m.name as machine_name FROM print_jobs pj
        JOIN machines m ON pj.machine_id = m.id
        WHERE pj.nest_group = ?
    """, (nest_group,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_jobs_for_machine(machine_id: str, include_completed: bool = False):
    conn = get_connection()
    if include_completed:
        rows = conn.execute("""
            SELECT * FROM print_jobs WHERE machine_id = ?
            ORDER BY CASE status WHEN 'printing' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, created_at DESC
        """, (machine_id,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM print_jobs WHERE machine_id = ? AND status IN ('queued', 'printing')
            ORDER BY CASE status WHEN 'printing' THEN 0 ELSE 1 END, created_at ASC
        """, (machine_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_active_jobs():
    conn = get_connection()
    rows = conn.execute("""
        SELECT pj.*, m.name as machine_name FROM print_jobs pj
        JOIN machines m ON pj.machine_id = m.id
        WHERE pj.status IN ('queued', 'printing')
        ORDER BY m.name, CASE pj.status WHEN 'printing' THEN 0 ELSE 1 END, pj.created_at ASC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def search_jobs(query: str, warehouse: Optional[str] = None):
    conn = get_connection()
    if warehouse:
        rows = conn.execute("""
            SELECT pj.*, m.name as machine_name FROM print_jobs pj
            JOIN machines m ON pj.machine_id = m.id
            WHERE pj.filename LIKE ? AND pj.status != 'removed'
                AND m.warehouse = ?
            ORDER BY pj.created_at DESC
        """, (f"%{query}%", warehouse)).fetchall()
    else:
        rows = conn.execute("""
            SELECT pj.*, m.name as machine_name FROM print_jobs pj
            JOIN machines m ON pj.machine_id = m.id
            WHERE pj.filename LIKE ? AND pj.status != 'removed'
            ORDER BY pj.created_at DESC
        """, (f"%{query}%",)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_completed_jobs(limit: int = 50, warehouse: Optional[str] = None):
    conn = get_connection()
    if warehouse:
        rows = conn.execute("""
            SELECT pj.*, m.name as machine_name FROM print_jobs pj
            JOIN machines m ON pj.machine_id = m.id
            WHERE pj.status = 'completed' AND m.warehouse = ?
            ORDER BY pj.completed_at DESC
            LIMIT ?
        """, (warehouse, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT pj.*, m.name as machine_name FROM print_jobs pj
            JOIN machines m ON pj.machine_id = m.id
            WHERE pj.status = 'completed'
            ORDER BY pj.completed_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_daily_stats(for_date: Optional[str] = None, warehouse: Optional[str] = None):
    if for_date is None:
        for_date = date.today().isoformat()
    conn = get_connection()
    if warehouse:
        rows = conn.execute("""
            SELECT m.id, m.name,
                COUNT(pj.id) as total_jobs,
                COALESCE(SUM(pj.print_inches * pj.copies), 0) as total_inches
            FROM machines m
            LEFT JOIN print_jobs pj ON pj.machine_id = m.id
                AND pj.status = 'completed'
                AND date(pj.completed_at) = ?
            WHERE m.warehouse = ?
            GROUP BY m.id, m.name
            ORDER BY m.name
        """, (for_date, warehouse)).fetchall()
    else:
        rows = conn.execute("""
            SELECT m.id, m.name,
                COUNT(pj.id) as total_jobs,
                COALESCE(SUM(pj.print_inches * pj.copies), 0) as total_inches
            FROM machines m
            LEFT JOIN print_jobs pj ON pj.machine_id = m.id
                AND pj.status = 'completed'
                AND date(pj.completed_at) = ?
            GROUP BY m.id, m.name
            ORDER BY m.name
        """, (for_date,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Customer operations ──

def _hash_password(password: str, salt: str = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode()).hexdigest()
    return hashed, salt


def create_customer(name: str, email: str, password: str, monthly_credit_inches: float = 0) -> dict:
    conn = get_connection()
    customer_id = str(uuid.uuid4())[:8]
    password_hash, salt = _hash_password(password)
    conn.execute("""
        INSERT INTO customers (id, name, email, password_hash, password_salt, monthly_credit_inches)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (customer_id, name, email, password_hash, salt, monthly_credit_inches))
    conn.commit()
    customer = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    conn.close()
    return dict(customer)


def verify_customer_password(email: str, password: str) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute("SELECT * FROM customers WHERE email = ? AND is_active = 1", (email,)).fetchone()
    conn.close()
    if not row:
        return None
    customer = dict(row)
    hashed, _ = _hash_password(password, customer["password_salt"])
    if hashed == customer["password_hash"]:
        return customer
    return None


def get_customer_by_id(customer_id: str) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_customers() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM customers WHERE is_active = 1 ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_customer(customer_id: str, name: str = None, email: str = None,
                    password: str = None, monthly_credit_inches: float = None) -> Optional[dict]:
    conn = get_connection()
    updates = []
    params = []
    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if email is not None:
        updates.append("email = ?")
        params.append(email)
    if password is not None:
        hashed, salt = _hash_password(password)
        updates.append("password_hash = ?")
        params.append(hashed)
        updates.append("password_salt = ?")
        params.append(salt)
    if monthly_credit_inches is not None:
        updates.append("monthly_credit_inches = ?")
        params.append(monthly_credit_inches)
    if not updates:
        conn.close()
        return get_customer_by_id(customer_id)
    params.append(customer_id)
    conn.execute(f"UPDATE customers SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def deactivate_customer(customer_id: str):
    conn = get_connection()
    conn.execute("UPDATE customers SET is_active = 0 WHERE id = ?", (customer_id,))
    conn.commit()
    conn.close()


# ── Credit operations ──

def get_customer_balance(customer_id: str) -> float:
    conn = get_connection()
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) as balance FROM credit_ledger WHERE customer_id = ?",
        (customer_id,)
    ).fetchone()
    conn.close()
    return row["balance"] if row else 0.0


def add_credit(customer_id: str, amount: float, reason: str, reference_id: str = None) -> dict:
    conn = get_connection()
    current = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) as balance FROM credit_ledger WHERE customer_id = ?",
        (customer_id,)
    ).fetchone()["balance"]
    balance_after = current + amount
    conn.execute("""
        INSERT INTO credit_ledger (customer_id, amount, balance_after, reason, reference_id)
        VALUES (?, ?, ?, ?, ?)
    """, (customer_id, amount, balance_after, reason, reference_id))
    conn.commit()
    conn.close()
    return {"amount": amount, "balance_after": balance_after, "reason": reason}


def deduct_credit(customer_id: str, inches: float, reference_id: str = None) -> dict:
    return add_credit(customer_id, -inches, "print_deduction", reference_id)


def get_credit_history(customer_id: str, limit: int = 50) -> list[dict]:
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM credit_ledger WHERE customer_id = ?
        ORDER BY created_at DESC LIMIT ?
    """, (customer_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Customer file operations ──

def create_customer_file(customer_id: str, original_filename: str, stored_filename: str,
                         file_size: int, width_px: int, height_px: int,
                         dpi_x: float, dpi_y: float, print_inches: float, copies: int = 1) -> dict:
    conn = get_connection()
    file_id = str(uuid.uuid4())[:8]
    conn.execute("""
        INSERT INTO customer_files (id, customer_id, original_filename, stored_filename,
            file_size, width_px, height_px, dpi_x, dpi_y, print_inches, copies)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (file_id, customer_id, original_filename, stored_filename,
          file_size, width_px, height_px, dpi_x, dpi_y, print_inches, copies))
    conn.commit()
    row = conn.execute("SELECT * FROM customer_files WHERE id = ?", (file_id,)).fetchone()
    conn.close()
    return dict(row)


def get_customer_files(customer_id: str = None, status: str = None) -> list[dict]:
    conn = get_connection()
    query = "SELECT cf.*, c.name as customer_name FROM customer_files cf JOIN customers c ON cf.customer_id = c.id WHERE 1=1"
    params = []
    if customer_id:
        query += " AND cf.customer_id = ?"
        params.append(customer_id)
    if status:
        query += " AND cf.status = ?"
        params.append(status)
    query += " ORDER BY cf.uploaded_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_customer_file_by_id(file_id: str) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute("""
        SELECT cf.*, c.name as customer_name FROM customer_files cf
        JOIN customers c ON cf.customer_id = c.id
        WHERE cf.id = ?
    """, (file_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_customer_file(file_id: str, customer_id: str) -> bool:
    conn = get_connection()
    result = conn.execute(
        "DELETE FROM customer_files WHERE id = ? AND customer_id = ? AND status = 'uploaded'",
        (file_id, customer_id)
    )
    conn.commit()
    deleted = result.rowcount > 0
    conn.close()
    return deleted


def assign_customer_file_to_machine(file_id: str, machine_id: str) -> Optional[dict]:
    conn = get_connection()
    cf = conn.execute("SELECT * FROM customer_files WHERE id = ?", (file_id,)).fetchone()
    if not cf:
        conn.close()
        return None
    cf = dict(cf)
    # Create a print_job linked to this customer file
    conn.execute("""
        INSERT INTO print_jobs (machine_id, filename, filepath, width_px, height_px,
            dpi_x, dpi_y, print_inches, copies, status, customer_file_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
    """, (machine_id, cf["original_filename"], "", cf["width_px"], cf["height_px"],
          cf["dpi_x"], cf["dpi_y"], cf["print_inches"], cf["copies"], file_id))
    job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "UPDATE customer_files SET status = 'queued', print_job_id = ? WHERE id = ?",
        (job_id, file_id)
    )
    conn.commit()
    conn.close()
    return {"job_id": job_id, "file_id": file_id}


def update_customer_file_status(file_id: str, status: str):
    conn = get_connection()
    if status == "completed":
        conn.execute(
            "UPDATE customer_files SET status = ?, completed_at = datetime('now') WHERE id = ?",
            (status, file_id)
        )
    else:
        conn.execute("UPDATE customer_files SET status = ? WHERE id = ?", (status, file_id))
    conn.commit()
    conn.close()


def update_customer_file_copies(file_id: str, copies: int):
    conn = get_connection()
    conn.execute("UPDATE customer_files SET copies = ? WHERE id = ?", (copies, file_id))
    conn.commit()
    conn.close()


def get_pending_inches(customer_id: str) -> float:
    """Get total inches of files that are uploaded/queued/printing (not yet completed)."""
    conn = get_connection()
    row = conn.execute("""
        SELECT COALESCE(SUM(print_inches * copies), 0) as total
        FROM customer_files
        WHERE customer_id = ? AND status IN ('uploaded', 'queued', 'printing')
    """, (customer_id,)).fetchone()
    conn.close()
    return row["total"] if row else 0.0


def get_customer_file_by_job_id(job_id: int) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM customer_files WHERE print_job_id = ?", (job_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_report(start_date: str, end_date: str, warehouse: Optional[str] = None):
    """Get detailed report for a date range. Returns per-machine daily breakdown."""
    conn = get_connection()

    if warehouse:
        # Per-machine totals for the range
        machine_totals = conn.execute("""
            SELECT m.id, m.name,
                COUNT(pj.id) as total_jobs,
                COALESCE(SUM(pj.print_inches * pj.copies), 0) as total_inches
            FROM machines m
            LEFT JOIN print_jobs pj ON pj.machine_id = m.id
                AND pj.status = 'completed'
                AND date(pj.completed_at) BETWEEN ? AND ?
            WHERE m.warehouse = ?
            GROUP BY m.id, m.name
            ORDER BY m.name
        """, (start_date, end_date, warehouse)).fetchall()

        # Daily breakdown (all machines combined)
        daily_totals = conn.execute("""
            SELECT date(pj.completed_at) as day,
                COUNT(pj.id) as total_jobs,
                COALESCE(SUM(pj.print_inches), 0) as total_inches
            FROM print_jobs pj
            JOIN machines m ON pj.machine_id = m.id
            WHERE pj.status = 'completed'
                AND date(pj.completed_at) BETWEEN ? AND ?
                AND m.warehouse = ?
            GROUP BY date(pj.completed_at)
            ORDER BY day
        """, (start_date, end_date, warehouse)).fetchall()

        # Per-machine per-day breakdown
        machine_daily = conn.execute("""
            SELECT m.name as machine_name, date(pj.completed_at) as day,
                COUNT(pj.id) as total_jobs,
                COALESCE(SUM(pj.print_inches * pj.copies), 0) as total_inches,
                MIN(pj.started_at) as first_start
            FROM print_jobs pj
            JOIN machines m ON pj.machine_id = m.id
            WHERE pj.status = 'completed'
                AND date(pj.completed_at) BETWEEN ? AND ?
                AND m.warehouse = ?
            GROUP BY m.name, date(pj.completed_at)
            ORDER BY day, m.name
        """, (start_date, end_date, warehouse)).fetchall()
    else:
        machine_totals = conn.execute("""
            SELECT m.id, m.name,
                COUNT(pj.id) as total_jobs,
                COALESCE(SUM(pj.print_inches * pj.copies), 0) as total_inches
            FROM machines m
            LEFT JOIN print_jobs pj ON pj.machine_id = m.id
                AND pj.status = 'completed'
                AND date(pj.completed_at) BETWEEN ? AND ?
            GROUP BY m.id, m.name
            ORDER BY m.name
        """, (start_date, end_date)).fetchall()

        daily_totals = conn.execute("""
            SELECT date(completed_at) as day,
                COUNT(id) as total_jobs,
                COALESCE(SUM(print_inches), 0) as total_inches
            FROM print_jobs
            WHERE status = 'completed'
                AND date(completed_at) BETWEEN ? AND ?
            GROUP BY date(completed_at)
            ORDER BY day
        """, (start_date, end_date)).fetchall()

        machine_daily = conn.execute("""
            SELECT m.name as machine_name, date(pj.completed_at) as day,
                COUNT(pj.id) as total_jobs,
                COALESCE(SUM(pj.print_inches * pj.copies), 0) as total_inches,
                MIN(pj.started_at) as first_start
            FROM print_jobs pj
            JOIN machines m ON pj.machine_id = m.id
            WHERE pj.status = 'completed'
                AND date(pj.completed_at) BETWEEN ? AND ?
            GROUP BY m.name, date(pj.completed_at)
            ORDER BY day, m.name
        """, (start_date, end_date)).fetchall()

    conn.close()

    return {
        "start_date": start_date,
        "end_date": end_date,
        "machine_totals": [dict(r) for r in machine_totals],
        "daily_totals": [dict(r) for r in daily_totals],
        "machine_daily": [dict(r) for r in machine_daily],
    }
