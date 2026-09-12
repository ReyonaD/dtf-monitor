# CLAUDE.md — DTF Monitor (project brain)

Read automatically by every Claude Code session here. Shared, portable project memory
(committed to git, unlike local `~/.claude` memory). Keep it current.

Sister app: **Order Tracker** (separate repo, its own `CLAUDE.md`) — the Shopify order
tracker on Railway. This app's printer agent reports prints back to Order Tracker's
`/integrations/print`; OT pulls warehouse split from here via `/sheets/pull-split`.

## What this is
Production-floor monitor for the DTF print shop.
- **Server**: Python FastAPI + SQLite (on a Railway volume at `/data`), at
  https://dtfproductionstatus.com. Auth via `server/auth.py` (`AuthMiddleware` /
  `is_public_path` / `PUBLIC_PATHS`); dashboard password in env `DASHBOARD_PASSWORD`.
- **Agent**: a tkinter app (`agent/agent.py`) running on each printer PC; PyInstaller
  `--onefile --windowed`. Self-updates via heartbeat `latest_version` + `/api/agent/download`
  (served from the volume `/data/agent/`). `AGENT_VERSION` in agent.py.

## Deploy
- `railway up --service dtf-monitor --detach` from the repo root (uploads working dir).
  GitHub source of truth: github.com/ReyonaD/dtf-monitor (also commit+push).
  Railway project id d69e3e36-07f3-478b-9dea-b60914bc9b04, service e4c68a13….
- Deploying restarts the live floor monitor briefly — avoid mid-day unless needed.
- Verify: `railway logs --service dtf-monitor`; health via `/health` (may 302 to login).

## Dropbox (Print-Files feature)
Operators browse/print DTF design files that live in a Dropbox **team folder** `/PRODUCTION`.
`server/dropbox_service.py` holds the token (never on agents).
- **Auth is a permanent refresh token** (set 2026-09): env `DROPBOX_REFRESH_TOKEN` +
  `DROPBOX_APP_KEY` (0v1p2yg1cwvdu1b) + `DROPBOX_APP_SECRET`; `_bearer()` mints ~4h access
  tokens (falls back to legacy `DROPBOX_TOKEN` if the three aren't set). No more expiry/502s.
  Re-issue: authorize `https://www.dropbox.com/oauth2/authorize?client_id=<KEY>&token_access_type=offline&response_type=code`
  (single-use code), then POST `https://api.dropboxapi.com/oauth2/token`
  (`grant_type=authorization_code&code=…`, HTTP Basic `KEY:SECRET`).
- Team folder: every call sends header `Dropbox-API-Path-Root: {".tag":"root","root":<DROPBOX_ROOT_NS>}`
  (root ns 3220133891; the member home ns differs). `DROPBOX_ROOT_PATH=/PRODUCTION`.
  Edge/CDN 403s the default `Python-urllib` UA — always send a browser-like `User-Agent`.
- **search_v2 quirk**: it rejects `options.path` in the team-root namespace (400 invalid_argument),
  so `/api/dropbox/search` searches the whole root namespace with NO path and filters results
  to those under `/PRODUCTION` (compare lowercased — Dropbox is case-insensitive).
- Endpoints (public in auth.py for the agent): `/api/dropbox/list` (20s cache; `?fresh=1`
  bypasses), `/api/dropbox/search`, `/api/dropbox/temp-link`, `/api/dropbox/move`,
  `/api/dropbox/claim` + `/release` (claim = lock a file the instant Print is pressed so two
  machines can't grab it; TTL 45min; cleared on move-to-PRINTED).

## Print-Files agent UI (local test)
`agent/ui_preview/` is a **pywebview** preview wired to the real server (LOCAL TEST ONLY —
does not touch the built agent or other PCs). `preview.py` = Python↔JS bridge; `index.html` =
UI. Settings (hot folder, machine, operator, server, browse root) saved to `agent_config.json`;
edited from the ⚙ icon. Browsing: rail = current folder's children with a back arrow; grid/list
views; parses order code / (N-M) part / inch / (Nx) copies / customer from filenames; claim-lock
(🔒); Print downloads via temp-link to the hot folder (copies dropped N times). Do NOT roll this
into the real `agent.py` / bump AGENT_VERSION / push to the 10 PCs until the user says so.

## Conventions & gotchas
- **Deploy is `railway up`** (+ commit/push to GitHub). Committing only when the user asks.
- Setting Railway env vars with leading-slash values from **Git Bash** gets MSYS-mangled
  (`/PRODUCTION` → `C:/Program Files/Git/PRODUCTION`); use `MSYS_NO_PATHCONV=1` or the Railway
  MCP. `dropbox_service._clean_root()` self-heals a mangled `DROPBOX_ROOT_PATH`.
- Agent auto-update earlier had a first-run `_MEI…python313.dll` transient (Defender scanning
  the extracted DLL) — works on 2nd launch; not a Windows-version issue.
- Keep user-facing strings in English.
