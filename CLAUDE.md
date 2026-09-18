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
- **Queue tab** (added 2026-09) = this machine's work list, the operator's main screen. Stages
  advance on their own: **Downloaded** (Print pressed → queue) → **RIP'd** (file name seen in
  RIPLOG) → **Printed ✓** (oven camera read the sheet's QR). Stuck rows get an amber/red stripe
  (Downloaded→RIP'd 30/60 min, RIP'd→Printed 45/90 min). Row menu: re-download / mark printed
  (camera missed) / release / remove. Preview stores the queue in `queue.json` and simulates
  the camera with a "Scan" box; the real agent keeps it server-side (heartbeat `jobs`).
- **Selection/claim/queue are per SHEET** (file path), not per order: a 5-sheet order can be
  split across machines (click one sheet = take just that one; click the order = all). Each
  sheet is its own queue row and its own Printed ✓, so "who/which machine" is recorded per
  sheet; OT rolls sheets up to the order (see Production-tracking design below).
- **Printed sheets move to `<their folder>/BASILDI/`** on Dropbox (server `/api/dropbox/move`,
  which creates the folder if missing, releases the claim and **records who printed it** —
  `machine`/`operator` from the request — in the `dropbox_printed` SQLite table keyed by the
  file's new path). `/api/dropbox/list` and `/search` return `printedBy {machine, operator, at}`
  next to `claimedBy`, so **every PC's agent** shows "Printed · MACHINE · OPERATOR" on files
  in BASILDI. The file name is NOT changed. A store folder = "still to print"; a partly
  printed order shows only its remaining sheets plus a "✓ n/N printed" badge.
  **Decision (2026-09-12): ONE fixed `BASILDI/` per folder** — no dated / per-operator
  sub-folders. Operators used to hand-make folders like `09-12-26-Aslan basildi`; that habit is
  replaced by the server record (who/when lives in `dropbox_printed`, not in folder names). The Download button is called **Download**
  (it downloads to the hot folder; printing happens in Flexi) and shows a live progress bar.
- **Oven camera (built 2026-09-15)**: `agent/ui_preview/camera_worker.py` runs as a CHILD PROCESS
  supervised by `preview.py` (`start_camera` / `_cam_reader` / `_cam_supervisor`). It owns the webcam
  (OpenCV, MSMF backend), runs `QRCodeDetector.detectAndDecodeMulti` on every 3rd frame, debounces
  the same code for 60 s, prints one JSON line per event (`code` / `status`) and writes a preview
  JPEG; the parent posts each code to `/api/queue/scan` (identical to the typed-code box) and shows
  the same green/amber/red flash. Settings key `camera` = webcam index ("0") or "off"; ⚙ has the field.
  **Why a process, and gotchas learned the hard way:** (1) MSMF capture opened from a non-main thread
  in the pywebview process never delivered frames; (2) MSMF rejects `CAP_PROP_*_TIMEOUT_MSEC`
  (prop 53) — opening with it fails; (3) opening a C920 via MSMF can take **~18 s** (device
  negotiation; each `cap.set()` after open re-negotiates ~6 s), so the supervisor waits 45 s before
  calling a worker "quiet"; (4) a worker killed abruptly leaves the device wedged for a while —
  the parent stops it via a stop-file (`camera_stop.flag`) so it releases the camera cleanly;
  (5) the Windows Camera app (or any other app) holding the device gives MSMF error
  `-1072875772`. Test QR: `cv2.QRCodeEncoder` output of "PRO7807 (1-3)" decodes fine.
  **CPU budget (measured 2026-09-15, 16-core PC):** worker idle **0.09 core**, UI 0.03 — after
  capturing 720p @ 10 fps (the FPS constructor param is ignored, so it is re-set after open),
  `cv2.setNumThreads(1)`, BELOW_NORMAL priority and a motion gate (160x90 gray diff; detect at
  most 4x/s while moving + one sweep every 3 s). Before tuning it was ~1 core (1080p@30fps,
  detection every 3rd frame). Detection cost: ~15 ms empty / ~40 ms with a QR at 720p; a QR
  smaller than ~110 px did NOT decode in tests — keep 720p full-frame detection.
  Kill stray previews/workers with PowerShell `Stop-Process -Name pythonw,python` (a Git-Bash
  `taskkill` loop silently missed them once, leaving 4 workers fighting over the camera).
- **`++` at the very start of a file name = urgent/priority order.** Parsed in `sheet_names.py` and
  the preview's `parseFile`; shown as a red URGENT badge in Print Files (sorted first), the Queue,
  and `/queue`; stored in `sheet_queue.urgent`; forwarded to OT (`urgent: true`), which sets the
  order's `urgent` flag (never un-sets it).
- **Downloads are numbered** `N-- <file>` (operators' habit: `31---------++1PX - …`): next N = highest
  numeric prefix in the hot folder + 1. Each `(2x)` copy gets its own number. RIPLOG matching
  compares the numbered local name AND the bare name (`core.ripped_after`).
- Part rule for "(a-b)": the smaller number is the part (so "(2-5)" = part 2 of 5, "(3-1)" =
  part 1 of 3). Same in `_parse_name` (py) and `parseFile` (js) — keep them identical.

## NEW agent (`agent/agent_main.py` + `agent/dtf_agent/`, built 2026-09-18 — NOT RELEASED)
Replaces the tkinter `agent.py` (kept until rollout). One exe, three roles:
- `core.py` — config (reads/writes the OLD `config.json` keys too: machine_name/watched_folder/
  riplog_path/machine_id — an in-place update keeps identity; `machineId` is minted once and
  persisted), server client (`X-Agent-Key`), heartbeat every 8 s (RIPLOG file list → floor
  dashboard, jobs/customer files/latest_version back), self-update (same batch swap as before),
  history, `ripped_after(name, since)` = real RIPLOG check for the queue's RIP'd stage,
  `find_riplog` incl. the SAi Production Suite path.
- `riplog.py` — RIPLogParser/RIPLogWatcher moved verbatim from agent.py.
- `camera.py` + `camera_worker.py` — oven camera; packaged worker = **same exe `--camera-worker`**.
- `bridge.py` + `ui/index.html` — the preview UI (Print Files / Queue / History + customer files,
  first-run opens Settings). Lock screen and Start/Complete buttons are GONE on purpose.
- Build: `pip install -r requirements-agent.txt pyinstaller && python -m PyInstaller build_agent.spec`
  (`pyinstaller` may not be on PATH in Git Bash). 70 MB onefile, UPX off, numpy collected
  explicitly (else "OpenCV bindings requires numpy" at runtime). Verified 2026-09-18 on the
  office PC: window, online, RIPLOG watched, Dropbox rail; worker mode clean without a camera.
- Dev run: `DTF_AGENT_CONFIG=<path> pythonw agent_main.py` (env var overrides config location).
- **Release checklist (do when the cameras arrive):** set `AGENT_API_KEY` on the server
  (Railway var) and the same key in the exe (`DTF_AGENT_KEY` at build or `agentKey` in
  config.json) → deploy server (legacy endpoints stay open for old agents) → bump
  `AGENT_VERSION` in core.py → build → upload exe to the volume `/data/agent/` + version file
  → old agents self-update on the next heartbeat → afterwards remove the RIPLOG
  "auto-complete → OT Printed" path in server.py (the oven scan is the truth now).
- Testing gotcha: a heartbeat with a NEW machine_id but an EXISTING machine_name makes the
  server delete the old row + its print_jobs (`upsert_machine`). Names are case-sensitive
  (`PICASSO_M_1` test ≠ real `Picasso_M_1`). Never test with a real machine's exact name and a
  fresh id.

## Server-side sheet queue (`/api/queue/*`, built 2026-09-12)
The per-machine work list lives in SQLite table `sheet_queue` (one row per machine+file), so
every agent, the wall board **`/queue`** (`static/queue.html`, session-protected, auto-refresh
5 s, linked from the dashboard header) and Order Tracker see the same thing. Agents only report
events: `assign` (after download → Downloaded, also claims, and tells OT stage `downloaded` so its
chase list shows "Downloaded on M1"), `ripped` (agent's RIPLOG watcher →
server tells OT), `scan` (oven camera / preview box → server stamps who, moves the file to
BASILDI, tells OT; a scan at machine X's oven can complete a sheet another machine downloaded
— the oven is the truth), `action` (release/remove/mark_printed/move_printed), `clear`.
`GET /api/queue?machine=` returns the list; `GET /api/queue/all` feeds the board. Timestamps are
UTC ISO **with offset** (the UI parses them, naive strings would be off by the TZ). File-name
parsing is `server/sheet_names.py` (JS twin `parseFile` in the preview — keep in sync). The
preview no longer keeps `queue.json`.

## Sheet progress → Order Tracker (`POST /api/sheet-status`, built 2026-09-12)
Agent queue events go **through this server** to OT (the OT API key never leaves the server):
`{code, part, total, copies, stage: ripped|printed, machine, operator, fileName, printedCount}`
→ `order_tracker.update_sheet()` → OT `/integrations/print` per-sheet mode (OT keeps a `Sheet`
row per part and rolls the order up: `RIP'd 1/5` → `Printed 3/5` → `Printed`). The preview
agent calls it when RIPLOG flips a queue item to RIP'd and when a scan/Mark-printed completes;
the Queue row shows `OT ✓` / `⚠ OT` with the reason.
**Legacy path still live**: the old tkinter agents' RIPLOG "auto-complete" + Complete button
(`server.py` → `update_orders_for_jobs`) still writes order-level `Printed` to OT — kept on
purpose until the new agent is rolled out to the printer PCs (the old agents have no other
way to report). Retire it when `agent.py` gets the Queue + camera.

## Production-tracking design (agreed 2026-09, being built)
Problem: RIPLOG ≠ printed. A downloaded file can never reach Flexi, and a RIP'd file can never
be sent to the printer — both invisible to the agent. Design: **expected vs. actual**.
- **GSB** stamps a Sheet ID (order + part/total, same string as the filename) as a **QR in the
  non-transfer top margin** of every gang sheet. Per-shop toggle; owner's shops on.
- **Order Tracker** = ledger of expected orders/sheets + **chase list** (due today, not through
  the oven) + thresholds/alerts; DTF Monitor's wall dashboard shows the chase list.
- **Agent** (printer PC): Queue as above. "Printing" is NOT a stage (RIP'd covers it).
- **Oven checkpoint = a webcam fixed at the oven exit, USB into the printer PC, read by an
  agent camera thread** (passive — the sheet passes under it, nobody has to remember to scan).
  Tablet/hand scanner rejected. Fallbacks: hold sheet to camera / type the code. If reads are
  flaky, swap for a fixed-mount 2D scanner in **USB-serial** mode (HID mode would type into
  Flexi). Scan → `/api/scan` → server marks Printed, moves the Dropbox file to PRINTED, releases
  the claim, forwards to OT.
- Zero-code first step: point Flexi's **hot folder (auto-RIP)** at the agent's hot folder so the
  "forgot to import into Flexi" step disappears (verify the Flexi edition supports it).

## Conventions & gotchas
- **Deploy is `railway up`** (+ commit/push to GitHub). Committing only when the user asks.
- Setting Railway env vars with leading-slash values from **Git Bash** gets MSYS-mangled
  (`/PRODUCTION` → `C:/Program Files/Git/PRODUCTION`); use `MSYS_NO_PATHCONV=1` or the Railway
  MCP. `dropbox_service._clean_root()` self-heals a mangled `DROPBOX_ROOT_PATH`.
- Agent auto-update earlier had a first-run `_MEI…python313.dll` transient (Defender scanning
  the extracted DLL) — works on 2nd launch; not a Windows-version issue.
- Keep user-facing strings in English.
