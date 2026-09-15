"""
Local pywebview preview wired to REAL Dropbox data (via the DTF Monitor server).
LOCAL TEST ONLY — does not touch agent.py, the build, or other PCs.

The token never lives here: the server holds it. This preview asks the server to
list folders, hand out temporary download links, claim files, etc.

Setup (once):  pip install pywebview
Run:           python preview.py

Settings (hot folder, RIPLOG file, machine, operator, server, root) are edited
in-app from the ⚙ icon and saved to agent_config.json next to this file. Env
vars below are only the first-run defaults.
  DTF_SERVER, DTF_BROWSE_ROOT, DTF_HOT_FOLDER, DTF_MACHINE, DTF_OPERATOR, DTF_RIPLOG

The Queue (Downloaded → RIP'd → Printed ✓) lives on the SERVER (/api/queue/*), so
every machine, the wall board and Order Tracker see the same list. This bridge
only reports events: downloaded (after Print), RIP'd (it watches the local RIPLOG),
and — in the preview — a simulated oven-camera scan.
"""
import os
import re
import sys
import json
import shutil
import urllib.request
import urllib.parse
import base64
import threading
import time
import webview

try:
    import cv2  # oven camera (QR reader); optional — the UI works without it
except Exception:  # pragma: no cover
    cv2 = None

# Edge/CDN bot filters 403 the default "Python-urllib" UA — send a real one.
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DTF-Monitor-Agent/1.3"

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "agent_config.json")

DEFAULTS = {
    "server": os.environ.get("DTF_SERVER", "https://dtfproductionstatus.com").rstrip("/"),
    "browseRoot": os.environ.get("DTF_BROWSE_ROOT", "/PRODUCTION"),
    "hotFolder": os.environ.get("DTF_HOT_FOLDER", os.path.join(os.path.expanduser("~"), "Desktop", "DTF-HotFolder-TEST")),
    "machine": os.environ.get("DTF_MACHINE", "PICASSO_M_1"),
    "operator": os.environ.get("DTF_OPERATOR", "EMRE"),
    "riplog": os.environ.get("DTF_RIPLOG", ""),
    "camera": os.environ.get("DTF_CAMERA", "0"),  # webcam index ("0" = first), or "off"
}


# these must never be blank — a blank value falls back to the default
REQUIRED = {"server", "browseRoot", "hotFolder", "machine", "operator"}


def _load_cfg():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        for k in DEFAULTS:
            if k not in saved or saved[k] is None:
                continue
            v = saved[k]
            if k in REQUIRED and isinstance(v, str) and not v.strip():
                continue  # ignore a blank required value, keep the default
            cfg[k] = v
    except Exception:
        pass
    return cfg


CFG = _load_cfg()


def _server():
    return (CFG.get("server") or "").rstrip("/")


def _get(path, fresh=False):
    url = f"{_server()}/api/dropbox/list?path=" + urllib.parse.quote(path)
    if fresh:
        url += "&fresh=1"
    return _get_json_url(url)


def _get_json_url(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _post(ep, body):
    req = urllib.request.Request(f"{_server()}{ep}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": UA}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


# Live download progress, polled by the UI (pywebview runs each JS→Python call
# on its own thread, so download_progress() answers while print_files() runs).
PROGRESS = {"active": False, "file": "", "done": 0, "total": 0, "index": 0, "count": 0}


def _download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=180) as resp, open(dest, "wb") as fout:
        PROGRESS.update(done=0, total=int(resp.headers.get("Content-Length") or 0))
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            fout.write(chunk)
            PROGRESS["done"] += len(chunk)


def _who():
    return {"machine": CFG["machine"], "operator": CFG["operator"]}


# ── RIPLOG (Flexi's RIP log on this PC) ─────────────────────────────────────
def _find_riplog():
    """Auto-detect Flexi's live RIPLOG.HTML. Newer SAi Production Suite installs keep it
    under ProgramData\\SAi\\SAi Production Suite\\LicenseData\\<id>\\<hash>\\Jobs and Settings\\,
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
    if not found:
        return ""
    return max(found, key=lambda f: os.stat(f).st_mtime)


_AUTO_RIPLOG = {"path": None}


def _riplog_path():
    """Configured RIPLOG path, or the auto-detected one when the setting is blank."""
    p = (CFG.get("riplog") or "").strip()
    if p:
        return p
    if _AUTO_RIPLOG["path"] is None:
        _AUTO_RIPLOG["path"] = _find_riplog()
    return _AUTO_RIPLOG["path"]


_RIP_CACHE = {"mtime": None, "text": None}


def _riplog_text():
    """Lower-cased text of RIPLOG.HTML (tags stripped), re-read only when it changes."""
    p = _riplog_path()
    if not p or not os.path.isfile(p):
        return None
    try:
        mt = os.stat(p).st_mtime
        if _RIP_CACHE["mtime"] != mt:
            with open(p, encoding="utf-8", errors="ignore") as f:
                raw = f.read()
            txt = re.sub(r"<[^>]+>", " ", raw).replace("&nbsp;", " ").replace("&amp;", "&")
            _RIP_CACHE.update(mtime=mt, text=txt.lower())
        return _RIP_CACHE["text"]
    except Exception:
        return None


def _in_riplog(name, txt):
    """Preview shortcut: substring match on the file's stem. The real agent uses
    RIPLogParser and only counts entries newer than assigned_at."""
    stem = os.path.splitext(name)[0].lower()
    return bool(stem) and stem in txt


# ── Oven camera: reads the QR on each sheet as it leaves the oven ────────────
# The webcam is owned by a CHILD PROCESS (camera_worker.py): Windows' MSMF capture
# only behaves when the device is opened on a process's main thread, and pywebview
# owns this process's main thread. The worker prints one JSON line per event
# ({"event":"code"} for each new QR read — once per appearance in view; {"event":"status"} once a
# second) and writes a small preview JPEG; this supervisor posts each read to the
# server (/api/queue/scan — the same call the "type a code" box makes), keeps the
# last status for the UI, and restarts the worker if it dies or goes quiet.
import subprocess
SCAN_DEBOUNCE_S = 3  # a code must leave the camera's view for this long before it counts again
CAM = {"status": "off", "error": "", "index": None, "frames": 0, "last_code": "", "last_at": 0.0,
       "events": [], "lock": threading.Lock(), "proc": None, "gen": 0, "last_line": 0.0}
CAM_PREVIEW = os.path.join(HERE, "camera_preview.jpg")
CAM_STOPFILE = os.path.join(HERE, "camera_stop.flag")
CAM_LOG = os.path.join(HERE, "camera_worker.log")  # worker events, for support
WORKER = os.path.join(HERE, "camera_worker.py")


def _cam_stop_proc(proc):
    """Ask the worker to release the camera and exit; force it only if it ignores us."""
    if proc is None or proc.poll() is not None:
        return
    try:
        open(CAM_STOPFILE, "w").close()
        for _ in range(30):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if proc.poll() is None:
            proc.terminate()
    except Exception:
        pass
    finally:
        try:
            os.remove(CAM_STOPFILE)
        except Exception:
            pass


def _cam_scan(code):
    try:
        r = _post("/api/queue/scan", {"code": code, **_who()})
    except Exception as e:
        r = {"status": "error", "message": str(e)[:120]}
    with CAM["lock"]:
        CAM.update(last_code=code, last_at=time.time())
        CAM["events"].append({"code": code, "result": r, "at": time.time()})
        del CAM["events"][:-20]


def _cam_reader(proc, gen):
    """Read the worker's stdout until it exits; ignore output from a superseded worker."""
    for raw in proc.stdout:
        if CAM["gen"] != gen:
            break
        line = raw.decode("utf-8", "ignore").strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        CAM["last_line"] = time.time()
        if ev.get("event") == "code" and ev.get("code"):
            _cam_scan(str(ev["code"]).strip())
        elif ev.get("event") == "status":
            CAM.update(status=ev.get("status", "?"), error=ev.get("error", ""),
                       frames=int(ev.get("frames") or 0), index=ev.get("index"))
    proc.wait()
    if CAM["gen"] == gen and CAM["proc"] is proc:
        CAM.update(status="error", error=f"camera worker exited (code {proc.returncode}); restarting…")


def _cam_supervisor():
    """Restart the worker if it exits or stops reporting for 15 s."""
    while True:
        time.sleep(5)
        proc = CAM["proc"]
        if proc is None:
            continue
        dead = proc.poll() is not None
        # opening the device can take ~10-20 s on MSMF, so be patient before restarting
        quiet = CAM["last_line"] and time.time() - CAM["last_line"] > 45
        if dead or quiet:
            start_camera()


_cam_sup_started = False


def start_camera():
    """(Re)start the camera worker from CFG["camera"] ("0", "1", … or "off")."""
    global _cam_sup_started
    old = CAM["proc"]
    CAM["gen"] += 1
    CAM["proc"] = None
    _cam_stop_proc(old)
    sel = str(CFG.get("camera") or "off").strip().lower()
    if sel in ("", "off", "none", "no"):
        CAM.update(status="off", error="", index=None, frames=0)
        return
    try:
        idx = int(sel)
    except ValueError:
        CAM.update(status="error", error=f'camera setting "{sel}" is not a number (use 0, 1, … or off)')
        return
    if cv2 is None:
        CAM.update(status="error", error="opencv-python is not installed on this PC", index=idx)
        return
    CAM.update(status="starting", error="", index=idx, frames=0, last_line=time.time())
    # windowless interpreter for the worker (no console pops up behind the agent)
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if exe.lower().endswith("python.exe") and os.path.isfile(cand):
        exe = cand
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            [exe, "-u", WORKER, "--index", str(idx), "--preview", CAM_PREVIEW,
             "--debounce", str(SCAN_DEBOUNCE_S), "--stopfile", CAM_STOPFILE, "--log", CAM_LOG],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            creationflags=flags, cwd=HERE)
    except Exception as e:
        CAM.update(status="error", error=f"could not start camera worker: {e}")
        return
    CAM["proc"] = proc
    threading.Thread(target=_cam_reader, args=(proc, CAM["gen"]), daemon=True).start()
    if not _cam_sup_started:
        _cam_sup_started = True
        threading.Thread(target=_cam_supervisor, daemon=True).start()


def stop_camera():
    CAM["gen"] += 1
    proc, CAM["proc"] = CAM["proc"], None
    _cam_stop_proc(proc)
    CAM.update(status="off")


class Api:
    def config(self):
        return dict(CFG)

    def save_config(self, patch):
        for k, v in (patch or {}).items():
            if k not in DEFAULTS:
                continue
            if isinstance(v, str):
                v = v.strip()
            if k in REQUIRED and (v is None or v == ""):
                continue  # don't overwrite a good value with a blank one
            CFG[k] = v
        _AUTO_RIPLOG["path"] = None
        cam_before = str(patch.get("camera", CFG.get("camera")))
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(CFG, f, indent=2)
        except Exception as e:
            return {"status": "error", "message": str(e), "config": dict(CFG)}
        if "camera" in (patch or {}) and cam_before is not None:
            start_camera()
        return {"status": "ok", "config": dict(CFG)}

    def pick_folder(self):
        """Open a native folder picker (for the hot-folder field)."""
        try:
            win = webview.windows[0]
            res = win.create_file_dialog(webview.FOLDER_DIALOG)
            if res:
                return {"status": "ok", "path": res[0]}
        except Exception as e:
            return {"status": "error", "message": str(e)}
        return {"status": "cancel"}

    def pick_file(self):
        """Open a native file picker (for the RIPLOG file field)."""
        try:
            win = webview.windows[0]
            res = win.create_file_dialog(webview.OPEN_DIALOG)
            if res:
                return {"status": "ok", "path": res[0]}
        except Exception as e:
            return {"status": "error", "message": str(e)}
        return {"status": "cancel"}

    def detect_riplog(self):
        """Settings → Detect: find Flexi's live RIPLOG.HTML on this PC."""
        _AUTO_RIPLOG["path"] = None
        p = _find_riplog()
        return {"status": "ok" if p else "notfound", "path": p}

    def list_folder(self, path, fresh=False):
        try:
            return _get(path or CFG["browseRoot"], fresh)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def search(self, q):
        try:
            return _get_json_url(f"{_server()}/api/dropbox/search?q=" + urllib.parse.quote(q or ""), timeout=30)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def print_files(self, items):
        """items: [{path, copies}]. Claim each file (so no other machine grabs
        it), download it once, drop `copies` files into the hot folder (a (2x)
        order prints twice) and register it in this machine's server-side queue
        as Downloaded."""
        hot = CFG["hotFolder"]
        os.makedirs(hot, exist_ok=True)
        ok, failed = [], []
        PROGRESS.update(active=True, count=len(items), index=0, file="", done=0, total=0)
        for i, it in enumerate(items):
            p = it.get("path")
            copies = max(1, int(it.get("copies", 1) or 1))
            try:
                base = os.path.basename(p)
                PROGRESS.update(index=i + 1, file=base, done=0, total=0)
                _post("/api/dropbox/claim", {"path": p, **_who()})
                link = _post("/api/dropbox/temp-link", {"path": p}).get("link")
                if not link:
                    raise RuntimeError("no download link")
                name, ext = os.path.splitext(base)
                first = os.path.join(hot, base)
                _download(link, first)
                ok.append(base)
                for c in range(2, copies + 1):
                    dst = os.path.join(hot, f"{name} (copy {c}){ext}")
                    shutil.copyfile(first, dst)
                    ok.append(os.path.basename(dst))
                _post("/api/queue/assign", {"path": p, "hot_path": first, "copies": copies, **_who()})
            except Exception as e:
                failed.append({"path": p, "error": str(e)})
        PROGRESS["active"] = False
        return {"ok": ok, "failed": failed, "hotFolder": hot}

    def download_progress(self):
        return dict(PROGRESS)

    # ── Queue (server-side) ──
    def queue(self):
        """This machine's queue from the server; flips Downloaded → RIP'd for files
        that now appear in the local RIPLOG (the server tells Order Tracker)."""
        try:
            items = _get_json_url(f"{_server()}/api/queue?machine=" + urllib.parse.quote(CFG["machine"])).get("items", [])
        except Exception as e:
            return {"status": "error", "message": str(e)}
        txt = _riplog_text()
        if txt is not None:
            for idx, it in enumerate(items):
                if it.get("ripped_at") or it.get("printed_at"):
                    continue
                if _in_riplog(it["name"], txt):
                    try:
                        r = _post("/api/queue/ripped", {"id": it["id"]})
                        if r.get("item"):
                            items[idx] = r["item"]
                    except Exception:
                        pass
        rip = _riplog_path()
        return {"status": "ok", "items": items, "machine": CFG["machine"],
                "riplog": {"path": rip, "found": bool(rip and os.path.isfile(rip)),
                           "auto": not (CFG.get("riplog") or "").strip()}}

    def queue_scan(self, code):
        """Simulated oven-camera read (the real agent's camera thread posts the same)."""
        try:
            return _post("/api/queue/scan", {"code": (code or "").strip(), **_who()})
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def queue_action(self, item_id, action):
        try:
            if action == "redownload":
                items = _get_json_url(f"{_server()}/api/queue?machine=" + urllib.parse.quote(CFG["machine"])).get("items", [])
                it = next((x for x in items if str(x["id"]) == str(item_id)), None)
                if not it:
                    return {"status": "error", "message": "not in queue"}
                link = _post("/api/dropbox/temp-link", {"path": it["path"]}).get("link")
                if not link:
                    raise RuntimeError("no download link")
                os.makedirs(CFG["hotFolder"], exist_ok=True)
                dest = os.path.join(CFG["hotFolder"], it["name"])
                _download(link, dest)
                _post("/api/queue/assign", {"path": it["path"], "hot_path": dest, "copies": it.get("copies", 1), **_who()})
                return self.queue_action(None, "__list__")
            if action == "__list__":
                return {"status": "ok", "items": _get_json_url(f"{_server()}/api/queue?machine=" + urllib.parse.quote(CFG["machine"])).get("items", [])}
            return _post("/api/queue/action", {"id": item_id, "action": action, **_who()})
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ── Oven camera ──
    def camera_status(self):
        return {"status": CAM["status"], "error": CAM["error"], "index": CAM["index"], "frames": CAM["frames"],
                "lastCode": CAM["last_code"], "lastAt": CAM["last_at"], "available": cv2 is not None}

    def camera_events(self):
        """Scan results since the last call (the UI shows the same flash as a typed code)."""
        with CAM["lock"]:
            ev, CAM["events"] = list(CAM["events"]), []
        return ev

    def camera_frame(self):
        """Latest preview frame as a base64 JPEG (None when the camera is off)."""
        if CAM["status"] != "live":
            return None
        try:
            with open(CAM_PREVIEW, "rb") as f:
                return base64.b64encode(f.read()).decode()
        except Exception:
            return None

    def camera_live(self, on):
        """UI opened/closed the Live panel: the worker only encodes preview frames while it is open."""
        flag = CAM_PREVIEW + ".want"
        try:
            if on:
                open(flag, "w").close()
            elif os.path.exists(flag):
                os.remove(flag)
        except Exception:
            pass
        return {"status": "ok"}

    def camera_restart(self):
        start_camera()
        return self.camera_status()

    def queue_clear_done(self):
        try:
            return _post("/api/queue/clear", {"machine": CFG["machine"]})
        except Exception as e:
            return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    start_camera()
    import atexit
    atexit.register(stop_camera)
    webview.create_window(
        "DTF Monitor Agent — Preview",
        os.path.join(HERE, "index.html"),
        js_api=Api(),
        width=1180, height=780, min_size=(900, 600),
    )
    webview.start()
