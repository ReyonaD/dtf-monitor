"""
Non-UI core of the DTF Monitor agent: configuration (compatible with the legacy
agent's config.json), the server client (with the shared agent key), the RIPLOG
watcher, the heartbeat loop that feeds the floor dashboard, self-update, history
and customer files.

The UI (pywebview, bridge.py) only reads `state` and calls the small helpers here.
"""
import os
import sys
import json
import uuid
import time
import threading
import urllib.request
import urllib.parse

from .riplog import RIPLogParser, RIPLogWatcher

# Bump every time a new agent build is shipped (the server advertises the newest
# version in each heartbeat reply; older agents download it and relaunch).
AGENT_VERSION = "1.3.0"  # new agent line; the server still advertises 1.2.0 until rollout, so nothing auto-updates

# Edge/CDN bot filters 403 the default "Python-urllib" UA — send a real one.
UA = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) DTF-Monitor-Agent/{AGENT_VERSION}"

FROZEN = bool(getattr(sys, "frozen", False))


def app_dir():
    """Folder the exe (or agent_main.py) lives in — config.json sits next to it, as before."""
    base = sys.executable if FROZEN else sys.argv[0]
    return os.path.dirname(os.path.abspath(base))


CONFIG_FILE = os.environ.get("DTF_AGENT_CONFIG") or os.path.join(app_dir(), "config.json")

# ── Configuration ────────────────────────────────────────────────────────────
# New keys (what the UI edits) + a mirror of the legacy keys, so a PC updated
# in place keeps its machine name / RIPLOG / folder, and an old build could
# still read the file if we ever had to roll back.
DEFAULTS = {
    "server": os.environ.get("DTF_SERVER", "https://dtfproductionstatus.com").rstrip("/"),
    "browseRoot": os.environ.get("DTF_BROWSE_ROOT", "/PRODUCTION"),
    "hotFolder": os.environ.get("DTF_HOT_FOLDER", ""),
    "machine": os.environ.get("DTF_MACHINE", ""),
    "operator": os.environ.get("DTF_OPERATOR", ""),
    "riplog": os.environ.get("DTF_RIPLOG", ""),
    "camera": os.environ.get("DTF_CAMERA", "0"),   # webcam index ("0" = first) or "off"
    "agentKey": os.environ.get("DTF_AGENT_KEY", ""),  # shared key for the agent API (X-Agent-Key)
    "autostart": True,   # launch on Windows login (Startup-folder shortcut, no admin needed)
    "machineId": "",
}
LEGACY_MAP = {  # legacy config.json key → new key
    "server_url": "server", "machine_name": "machine", "watched_folder": "hotFolder",
    "riplog_path": "riplog", "operator": "operator", "machine_id": "machineId",
}
REQUIRED = {"server", "browseRoot", "machine"}  # a blank value falls back to the default


def save_cfg(cfg):
    out = {k: cfg.get(k) for k in DEFAULTS}
    out.update({legacy: cfg.get(new) for legacy, new in LEGACY_MAP.items()})
    for k in ("lock_enabled", "supervisor_pin"):
        if k in cfg:
            out[k] = cfg[k]
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)


def load_cfg():
    cfg = dict(DEFAULTS)
    saved = {}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            saved = json.load(f) or {}
    except Exception:
        saved = {}
    for legacy, new in LEGACY_MAP.items():  # legacy first, new keys win below
        if saved.get(legacy) not in (None, "") and not saved.get(new):
            cfg[new] = saved[legacy]
    # Old agents had a hand-typed server_url (default "http://"); the Dropbox/queue API
    # only lives on the Railway host over https. Keep a Railway/production URL (forcing
    # https), otherwise fall back to the default.
    srv = str(cfg.get("server") or "").strip().rstrip("/")
    if "dtfproductionstatus.com" in srv or "railway.app" in srv:
        cfg["server"] = "https://" + srv.split("://", 1)[-1]
    else:
        cfg["server"] = DEFAULTS["server"]
    for k in DEFAULTS:
        v = saved.get(k)
        if v is None:
            continue
        if k in REQUIRED and isinstance(v, str) and not v.strip():
            continue
        cfg[k] = v
    # Legacy-only settings the old build wrote; keep them so nothing is lost.
    for k in ("lock_enabled", "supervisor_pin"):
        if k in saved:
            cfg[k] = saved[k]
    if not cfg.get("machineId"):
        # First run on this PC: mint the id ONCE and persist it. The server keys
        # machines by this id (a new id with the same name replaces the old row
        # and its jobs), so it must never change between starts.
        cfg["machineId"] = str(uuid.uuid4())
        try:
            save_cfg(cfg)
        except Exception:
            pass
    return cfg


CFG = load_cfg()


def is_configured():
    return bool((CFG.get("machine") or "").strip() and (CFG.get("hotFolder") or "").strip())


# ── Server client ────────────────────────────────────────────────────────────
def server():
    return (CFG.get("server") or "").rstrip("/")


def _headers(json_body=False):
    h = {"User-Agent": UA}
    if CFG.get("agentKey"):
        h["X-Agent-Key"] = CFG["agentKey"]
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def get_json(path, params=None, timeout=25):
    url = f"{server()}{path}"
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def post_json(path, body, timeout=30):
    req = urllib.request.Request(f"{server()}{path}", data=json.dumps(body).encode(),
                                 headers=_headers(json_body=True), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# Live download progress, polled by the UI (pywebview runs each JS→Python call on
# its own thread, so download_progress() answers while a download runs).
PROGRESS = {"active": False, "file": "", "done": 0, "total": 0, "index": 0, "count": 0}


def download(url, dest, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as fout:
        PROGRESS.update(done=0, total=int(resp.headers.get("Content-Length") or 0))
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            fout.write(chunk)
            PROGRESS["done"] += len(chunk)


def who():
    return {"machine": CFG["machine"], "operator": CFG["operator"]}


# ── RIPLOG auto-detect ───────────────────────────────────────────────────────
def find_riplog():
    """Flexi's live RIPLOG.HTML. Newer SAi Production Suite installs keep it under
    ProgramData\\SAi\\SAi Production Suite\\LicenseData\\<id>\\<hash>\\Jobs and Settings\\,
    older FlexiPRINT under Program Files\\SAi\\Flexi*\\Jobs and Settings\\. If several
    match, the most recently modified one is the live log."""
    import glob
    pd = os.environ.get("ProgramData", r"C:\ProgramData")
    roots = [
        os.path.join(pd, "SAi", "**", "Jobs and Settings", "RIPLOG.HTML"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "SAi", "Flexi*", "Jobs and Settings", "RIPLOG.HTML"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "SAi", "Flexi*", "Jobs and Settings", "RIPLOG.HTML"),
        os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "SAi", "**", "Jobs and Settings", "RIPLOG.HTML"),
    ]
    found = []
    for pat in roots:
        for m in glob.glob(pat, recursive=True):
            if os.path.isfile(m) and os.sep + "Backups" + os.sep not in m and os.sep + "Install" + os.sep not in m:
                found.append(m)
    return max(found, key=lambda f: os.stat(f).st_mtime) if found else ""


_AUTO_RIPLOG = {"path": None}


def riplog_path():
    """Configured RIPLOG path, or the auto-detected one when the setting is blank."""
    p = (CFG.get("riplog") or "").strip()
    if p:
        return p
    if _AUTO_RIPLOG["path"] is None:
        _AUTO_RIPLOG["path"] = find_riplog()
    return _AUTO_RIPLOG["path"]


def reset_riplog_cache():
    _AUTO_RIPLOG["path"] = None


# ── Heartbeat: RIPLOG → server (floor dashboard), jobs/customer files back ──
class Heartbeat(threading.Thread):
    """Every 8 s: POST the RIPLOG-derived file list (what the wall dashboard shows)
    and take back this machine's jobs, customer files and the latest agent version.
    Also the queue's RIP'd source: `ripped_after(name, since)`."""

    def __init__(self):
        super().__init__(daemon=True)
        self.state = {"connected": False, "last_ok": 0.0, "jobs": [], "customer_files": [],
                      "history": [], "latest_version": "", "riplog_active": False, "error": ""}
        self.riplog_files = []
        self.riplog_jobs = []        # raw parsed RIPLOG entries (for the queue's RIP'd check)
        self._watcher = None
        self._updating = False
        self._stop = threading.Event()
        self.restart_riplog()

    # RIPLOG
    def restart_riplog(self):
        if self._watcher:
            try:
                self._watcher.stop()
            except Exception:
                pass
            self._watcher = None
        p = riplog_path()
        if p and os.path.isfile(p):
            self._watcher = RIPLogWatcher(p, self._on_riplog)
            self.state["riplog_active"] = True
            try:  # prime immediately so the queue sees RIP'd items without waiting for a change
                self._on_riplog(RIPLogParser.parse_file(p))
            except Exception:
                pass
        else:
            self.state["riplog_active"] = False

    def _on_riplog(self, parsed_jobs):
        self.riplog_jobs = parsed_jobs or []
        try:
            self.riplog_files = RIPLogParser.build_file_list(parsed_jobs)
        except Exception:
            self.riplog_files = []
        self.send()

    def ripped_after(self, name, since_iso):
        """True if the RIPLOG holds a RIP entry for this file name that finished after
        `since_iso` (the moment the file was downloaded), minus a minute of clock slack."""
        from datetime import datetime, timedelta
        try:
            since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
            if since.tzinfo is not None:
                since = since.astimezone().replace(tzinfo=None)  # RIPLOG stamps are local naive
        except Exception:
            since = None
        target = (name or "").lower()
        for j in self.riplog_jobs:
            f = os.path.basename(j.get("file", "") or "").lower()
            if f != target:
                continue
            ts = (RIPLogParser._parse_timestamp(j.get("rip_end")) or RIPLogParser._parse_timestamp(j.get("rip_start"))
                  or RIPLogParser._parse_timestamp(j.get("output_start")))
            if since is None or ts is None or ts >= since - timedelta(minutes=1):
                return True
        return False

    # heartbeat
    def send(self):
        payload = {
            "machine_id": CFG["machineId"], "machine_name": CFG["machine"],
            "watched_folder": CFG["hotFolder"], "operator": CFG["operator"],
            "agent_version": AGENT_VERSION, "files": list(self.riplog_files),
        }
        try:
            data = post_json("/api/heartbeat", payload, timeout=8)
            self.state.update(jobs=data.get("jobs", []), customer_files=data.get("customer_files", []),
                              latest_version=data.get("latest_version") or "", connected=True,
                              last_ok=time.time(), error="")
            self._maybe_update(self.state["latest_version"])
            return True
        except Exception as e:
            self.state.update(connected=False, error=str(e)[:160])
            return False

    def fetch_history(self):
        try:
            allh = get_json("/api/history", {"limit": 50}, timeout=8)
            self.state["history"] = [j for j in allh if j.get("machine_id") == CFG["machineId"]]
        except Exception:
            pass

    def run(self):
        cycle = 0
        while not self._stop.is_set():
            self.send()
            if cycle % 5 == 0:
                self.fetch_history()
            cycle += 1
            self._stop.wait(8)

    def stop(self):
        self._stop.set()
        if self._watcher:
            try:
                self._watcher.stop()
            except Exception:
                pass

    # self-update (only from the packaged exe; fail-soft)
    def _maybe_update(self, latest):
        try:
            if self._updating or not latest or not FROZEN or not _version_newer(latest, AGENT_VERSION):
                return
            self._updating = True
            threading.Thread(target=self._do_update, daemon=True).start()
        except Exception:
            pass

    def _do_update(self):
        """Download the new exe next to us, write an updater batch that waits for this
        process to release the exe, swaps it, relaunches — then exit."""
        import subprocess
        try:
            exe_path = os.path.abspath(sys.executable)
            exe_dir = os.path.dirname(exe_path)
            new_path = os.path.join(exe_dir, "DTF-Monitor-Agent.new.exe")
            download(f"{server()}/api/agent/download", new_path)
            if os.path.getsize(new_path) < 3_000_000:
                os.remove(new_path); self._updating = False; return
            with open(new_path, "rb") as f:
                if f.read(2) != b"MZ":
                    os.remove(new_path); self._updating = False; return
            bat_path = os.path.join(exe_dir, "_update.bat")
            bat = (
                "@echo off\r\nsetlocal\r\n"
                f'set "EXE={exe_path}"\r\nset "NEW={new_path}"\r\n'
                "set /a tries=0\r\n:waitloop\r\n"
                'del "%EXE%" 2>nul\r\nif not exist "%EXE%" goto swap\r\n'
                "set /a tries+=1\r\nif %tries% geq 60 goto fail\r\n"
                "timeout /t 1 /nobreak >nul\r\ngoto waitloop\r\n"
                ':swap\r\nmove /y "%NEW%" "%EXE%" >nul\r\nstart "" "%EXE%"\r\ngoto done\r\n'
                ':fail\r\ndel "%NEW%" 2>nul\r\nstart "" "%EXE%"\r\n'
                ':done\r\ndel "%~f0"\r\n'
            )
            with open(bat_path, "w") as f:
                f.write(bat)
            DETACHED = 0x00000008 | 0x00000200
            subprocess.Popen(["cmd", "/c", bat_path], creationflags=DETACHED, close_fds=True)
            os._exit(0)
        except Exception:
            self._updating = False


def _version_newer(a, b):
    def parts(v):
        out = []
        for p in str(v or "").split("."):
            try:
                out.append(int(p))
            except ValueError:
                out.append(0)
        return out
    pa, pb = parts(a), parts(b)
    n = max(len(pa), len(pb))
    return pa + [0] * (n - len(pa)) > pb + [0] * (n - len(pb))


HEARTBEAT = None


def start_heartbeat():
    global HEARTBEAT
    if HEARTBEAT is None:
        HEARTBEAT = Heartbeat()
        HEARTBEAT.start()
    return HEARTBEAT
