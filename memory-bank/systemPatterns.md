# System Patterns

## Architecture
```
PC Agent (x5)  ──HTTP POST──▶  Central Server  ◀──WebSocket──  Dashboard (browser)
  (watchdog)                    (FastAPI+SQLite)                 (vanilla JS)
```

## Data Flow
1. Agent scans watched folder → finds PNG/TIFF files → reads dimensions + DPI from metadata
2. Agent sends heartbeat every 8 seconds with full file list to `POST /api/heartbeat`
3. Server syncs file list (adds new, removes deleted), updates machine status
4. Server broadcasts updated state to all connected dashboard clients via WebSocket
5. Dashboard renders machine cards with live data

## Key Design Patterns

### Auto-Complete on Print
When operator clicks "Print" on file B, the server automatically marks file A (currently printing) as completed. Single-button workflow — no separate "Done" button needed.

### File Sync (not CRUD)
Agent doesn't send add/remove events. It sends the full file list every heartbeat. Server diffs against DB to add new files and remove deleted ones. This is more resilient to missed events.

### Dynamic DPI
Each image file contains its own DPI in metadata. Agent reads it with Pillow (`img.info['dpi']`). Falls back to 300 DPI if metadata is missing. Print inches = height_px / dpi_y.

### Offline Detection
Server background task checks `last_seen` timestamp every 15 seconds. Machines not heard from in 30 seconds are marked offline.

### WebSocket Broadcast
Every API call that changes state (heartbeat, start print, complete job) triggers a broadcast of the full dashboard state to all connected WebSocket clients. Simple but effective for 5 machines.

## Component Relationships
- Agent depends on Server (HTTP API)
- Dashboard depends on Server (WebSocket + REST)
- Server is standalone (SQLite embedded, no external DB)
- Agent and Dashboard have no direct connection
