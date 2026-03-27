# DTF Monitor

Real-time production monitoring system for DTF (Direct to Film) printing operations. Tracks machines, print queues, and customer orders across multiple Windows PCs from a single web dashboard.

## Architecture

```
PC Agent (x5 machines)  ──HTTP POST──▶  Central Server  ◀──WebSocket──  Web Dashboard
   (tkinter GUI)                       (FastAPI + SQLite)                (vanilla JS)
```

**3 components:**
- **Agent** — Runs on each printer PC. Watches the FlexiPrint RIPLOG for files, reads image DPI/dimensions, sends heartbeat every 8 seconds.
- **Server** — Collects heartbeats, syncs file state to SQLite, broadcasts live updates via WebSocket.
- **Dashboard** — Browser-based real-time view of all machines, queues, and stats.

## Features

### Admin Dashboard (`/admin`)
- Live machine status cards (printing / idle / offline)
- Print queue per machine with inch calculations
- Search files across all machines
- Reports: daily/weekly/monthly output per machine
- Machine management (assign warehouses, delete)
- Customer management (create, credit, delete with double confirmation)
- Customer file assignment to machines
- Dark / Light theme toggle

### Customer Portal (`/customer`)
- Login with email/password
- Upload PNG/TIFF files
- Track file status (uploaded → queued → printing → completed)
- View credit balance and history
- Dark / Light theme toggle

### Agent (Windows .exe)
- First-run setup wizard (select folder, name machine, enter server URL)
- FlexiPrint RIPLOG parsing for automatic file detection
- One-click Print/Complete workflow
- Nest grouping (multi-file print jobs)
- History tab with completed jobs
- System tray icon

## Project Structure

```
dtf-monitor/
├── agent/
│   ├── agent.py              # Agent GUI + heartbeat logic
│   ├── DTF-Monitor-Agent.spec # PyInstaller spec
│   └── build.bat             # Build standalone .exe
│
├── server/
│   ├── server.py             # FastAPI endpoints + WebSocket
│   ├── database.py           # SQLite schema + queries
│   ├── auth.py               # Session management
│   ├── google_sheets.py      # Google Sheets integration
│   └── static/
│       ├── index.html        # Admin dashboard
│       ├── dashboard.js      # Dashboard logic
│       ├── style.css         # Shared styles (dark/light themes)
│       ├── customer.html     # Customer portal
│       ├── customer.js       # Customer portal logic
│       └── landing.html      # Landing page
│
├── requirements.txt
└── start_server.bat
```

## Setup

### Server
```bash
pip install -r requirements.txt
cd server
python server.py
# Runs on http://localhost:8090
```

### Agent
```bash
cd agent
python agent.py
# Or build .exe:
build.bat
```

Agent first-run wizard will ask for:
1. FlexiPrint watched folder path
2. Machine name
3. Server URL
4. Operator name

## Database

Single-file SQLite (`server/dtf_monitor.db`), auto-created on first run. Tables:
- `machines` — Registered printer PCs
- `print_jobs` — All print jobs (queued, printing, completed)
- `warehouses` — Warehouse grouping
- `customers` — Customer accounts
- `customer_files` — Files uploaded by customers
- `credit_ledger` — Credit transaction history

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **File sync via heartbeat** | Agent sends full file list every 8s; server diffs against DB. Resilient to missed events, no file watcher on server side. |
| **Auto-complete on Print** | Clicking "Print" on file B auto-completes file A. Single-button workflow for operators. |
| **Dynamic DPI** | Each image's DPI read from metadata via Pillow. Fallback to 360 DPI if missing. `print_inches = height_px / dpi_y` |
| **Offline detection** | Background task checks `last_seen` every 15s; machines marked offline after 30s. |
| **SQLite** | Zero infrastructure. Single file, no database server needed. WAL mode for concurrent reads. |
| **Credits are manual** | No automatic monthly renewal. Admin adds credits manually via dashboard. Avoids confusion about billing cycles. |

## Recent Changes & Fixes

### Customer File Assignment Bug (Fixed)
**Problem:** When a customer file was assigned to a machine via the admin dashboard, a `print_job` was created with an empty `filepath` (since the file lives on the server, not on the printer PC). On the next agent heartbeat (every 8 seconds), `sync_files_for_machine()` would see this empty-filepath job missing from the agent's local file list and mark it as `removed`. The job would disappear from the agent's queue before the operator could see it.

**Fix:** `sync_files_for_machine()` in `database.py` now skips jobs with empty filepath when removing stale entries. Customer-assigned jobs (which have `filepath=""`) are preserved in the queue regardless of agent heartbeat sync.

**Affected file:** `server/database.py` — `sync_files_for_machine()` function

### Theme Toggle (Dark/Light Mode)
Added dark/light theme toggle to all pages (landing, admin dashboard, customer portal). Theme preference saved to localStorage per page. CSS variables drive all colors — no hardcoded dark values.

### Customer Credit System Simplified
Removed "monthly allocation" concept. Credits are now purely manual — admin adds/removes credits from the Customers tab. Eliminates confusion about automatic renewal that didn't exist.

### Customers Management Tab
New "Customers" tab in admin dashboard header alongside Dashboard, Reports, Machines. Full customer table with inline credit management, details modal, and delete with double confirmation dialog.

## Status

**Working:**
- Server + Dashboard + Customer Portal
- Agent GUI with RIPLOG parsing
- Customer file upload and assignment flow
- Credit system (manual)
- Dark/Light theme on all pages

**Needs Testing:**
- Agent .exe build on actual printer PCs
- End-to-end customer file flow (upload → assign → print → complete → credit deduction)
- Multi-machine concurrent operation

**Not Started:**
- Auto-start on Windows boot
- Shopify integration (optional)
- Sound/visual alerts for new jobs (optional)
