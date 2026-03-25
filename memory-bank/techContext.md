# Tech Context

## Technologies
| Component | Tech | Why |
|-----------|------|-----|
| Server | Python + FastAPI | Async, fast, simple, good WebSocket support |
| Database | SQLite (WAL mode) | Zero setup, single file, fast enough for 5 machines |
| Live updates | WebSocket | Instant push to dashboard, no polling |
| Agent | Python + watchdog + Pillow + tkinter | Folder monitoring, image reading, native Windows GUI |
| Dashboard | Vanilla HTML/CSS/JS | No build step, no framework overhead, easy to modify |
| Packaging | PyInstaller | Creates standalone .exe so operators don't need Python |

## Dependencies (requirements.txt)
- fastapi, uvicorn — web server
- websockets — WebSocket protocol
- pillow — read image dimensions and DPI
- watchdog — filesystem change detection
- pystray — system tray (available, not yet used)
- requests — HTTP client for agent→server calls
- pyinstaller — build .exe

## Development Setup
- Platform: Windows 11
- Project path: `c:\Users\Alp_office\Documents\MyDocuments\Order_Folders\dtf-monitor\`
- Server runs on port 8080
- Install: `pip install -r requirements.txt`
- Run server: `start_server.bat` or `cd server && python server.py`
- Run agent: `cd agent && python agent.py`
- Build agent exe: `cd agent && build.bat`

## Technical Constraints
- All PCs on same local network
- Agent must work on Windows (tkinter for GUI, PyInstaller for .exe)
- Image files can be large (print-resolution TIFF) — Pillow reads metadata without loading full image
- SQLite is single-writer but that's fine for 5 agents sending heartbeats every 8 seconds

## File Structure
```
dtf-monitor/
├── server/
│   ├── server.py          # FastAPI app, API endpoints, WebSocket
│   ├── database.py        # SQLite schema, queries, all DB operations
│   └── static/
│       ├── index.html     # Dashboard page
│       ├── style.css      # Dark warehouse-monitor theme
│       └── dashboard.js   # WebSocket client, rendering, search
├── agent/
│   ├── agent.py           # Folder watcher, heartbeat, tkinter GUI
│   ├── config.json        # Generated on first run (machine ID, name, folder, server URL)
│   └── build.bat          # PyInstaller build script
├── memory-bank/           # Project documentation
├── requirements.txt
└── start_server.bat       # Server launcher
```
