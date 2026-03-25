# Progress

## What's Built
- [x] Project structure and dependencies
- [x] Server: FastAPI with REST API + WebSocket
- [x] Database: SQLite with machines, print_jobs tables
- [x] Dashboard: real-time machine cards, search, history, daily stats
- [x] Agent: setup wizard, folder watcher, heartbeat, tkinter GUI with Print buttons
- [x] Build scripts (start_server.bat, build.bat for PyInstaller)

## What's Left
- [ ] Resolve server address input in agent setup (hardcode vs auto-discover vs pre-fill)
- [ ] End-to-end testing
- [ ] Build agent .exe and test on actual printer PCs
- [ ] Auto-start on Windows boot (startup shortcut or Windows service)
- [ ] Optional: sound/visual alerts for new jobs
- [ ] Optional: Shopify store integration
- [ ] Optional: print width calculation (roll width awareness)

## Known Issues
- None yet (not tested)

## Evolution
1. **Initial plan** — Three-part system: agent, server, dashboard
2. **User feedback** — DPI must be dynamic (not fixed), folder selected on install, single Print button auto-completes previous, dashboard needs search bar
3. **Current** — All core components built, pending testing and deployment
