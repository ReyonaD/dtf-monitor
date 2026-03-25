# Active Context

## Current State
Initial build of all three components is complete. Not yet tested end-to-end.

## Recent Changes
- Built server (FastAPI + SQLite + WebSocket)
- Built dashboard (dark theme, live updates, search, history)
- Built agent (setup wizard, folder watcher, tkinter GUI with Print buttons)
- Created memory-bank documentation

## Open Questions
- **Server address in agent setup**: Currently the setup wizard asks operators to type the server URL. Owner flagged this — may want to hardcode it, auto-discover, or pre-fill.

## Next Steps
- Resolve server address question for agent setup
- Test the system end-to-end (run server, run agent, verify dashboard updates)
- Consider: auto-start on Windows boot, sound alerts, Shopify integration
- Build agent .exe with PyInstaller and test on a printer PC

## Active Decisions
- DPI is dynamic (read from each file's metadata), fallback to 300 DPI
- Single "Print" button auto-completes previous job (no separate "Done" button)
- Agent sends full file list on heartbeat (server diffs to sync)
- Dashboard uses WebSocket for real-time updates
