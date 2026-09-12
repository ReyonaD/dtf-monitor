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
"""
import os
import re
import json
import uuid
import shutil
import urllib.request
import urllib.parse
from datetime import datetime
import webview

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
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
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


# Printed sheets are moved into this sub-folder of the folder they were in
# (e.g. /PRODUCTION/1-DTF/PRO/BASILDI/). Keeps the store folder = "still to print".
PRINTED_FOLDER = "BASILDI"


def _move_to_printed(it):
    """Move a printed sheet to <its folder>/BASILDI on Dropbox (the server moves it
    and releases the claim). Outcome is recorded on the queue item."""
    src = it["path"]
    folder, base = src.rsplit("/", 1)
    dst = f"{folder}/{PRINTED_FOLDER}/{base}"  # name unchanged; who printed it is reported by the agent
    try:
        r = _post("/api/dropbox/move", {"from": src, "to": dst,
                                        "machine": it.get("printed_machine") or CFG["machine"],
                                        "operator": it.get("printed_operator") or CFG["operator"]})
        if r.get("status") == "ok":
            it["moved_to"] = dst
            it.pop("move_error", None)
            return
        it["move_error"] = r.get("message", "move failed")
    except Exception as e:
        it["move_error"] = str(e)[:200]
    _release(src)  # not moved, but at least drop the lock


# ── Queue: this machine's work list ─────────────────────────────────────────
# Stages: Downloaded (Print pressed → file in hot folder) → RIP'd (the file's
# name shows up in Flexi's RIPLOG) → Printed ✓ (oven camera read the sheet's QR).
# PREVIEW-ONLY store: queue.json next to this file. In the real agent the queue
# lives on the server (heartbeat already returns `jobs`) and the camera thread
# posts scans to /api/scan; here scans are simulated from the UI.
QUEUE_FILE = os.path.join(HERE, "queue.json")

_CODE_WITH_COPIES = re.compile(r"([A-Za-z]{1,4}\d+)\s*\(\d+\s*[xX]?\)")
_CODE_PLAIN = re.compile(r"\b([A-Za-z]{1,4}\d{3,})\b")
_COPIES = re.compile(r"\((\d+)\s*[xX]\)")
_PART = re.compile(r"\((\d+)\s*-\s*(\d+)\)|(?<![\w/])(\d+)\s*/\s*(\d+)(?![\w/])")
_INCH = re.compile(r"-(\d+)\s*INCH", re.I)


def _parse_name(name):
    """Same rules as parseFile() in index.html: order code, part/total, copies, inches."""
    m = _CODE_WITH_COPIES.search(name) or _CODE_PLAIN.search(name)
    code = m.group(1).upper() if m else None
    cm = _COPIES.search(name)
    copies = max(1, int(cm.group(1))) if cm else 1
    part, total = 1, 1
    pm = _PART.search(name)
    if pm:
        a = int(pm.group(1) or pm.group(3))
        b = int(pm.group(2) or pm.group(4))
        # "(a-b)": the smaller number is the part — never part > total
        part, total = (a, b) if a <= b else (b, a)
    im = _INCH.search(name)
    inch = (im.group(1) + '"') if im else ""
    return {"code": code, "part": part, "total": total, "copies": copies, "inch": inch}


def _cust_of(name):
    after = name
    cut = re.search(r"\(\d+\s*-\s*\d+\)", name)
    if cut:
        after = re.sub(r"^\(\d+\s*-\s*\d+\)\s*", "", name[cut.start():])
    else:
        c2 = re.search(r"\(\d+\s*[xX]?\)", name)
        if c2:
            after = re.sub(r"^\(\d+\s*[xX]?\)\s*[-–]?\s*", "", name[c2.start():])
    after = re.sub(r"^\(\d+\)\s*[-–]?\s*", "", after)  # "(2x) - (1) Name" → "Name"
    after = re.sub(r"-\d+\s*INCH.*$", "", after, flags=re.I)
    after = re.sub(r"\.[a-z]+$", "", after, flags=re.I)
    return re.sub(r"^[\s-]+", "", after).strip()


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _load_queue():
    try:
        with open(QUEUE_FILE, encoding="utf-8") as f:
            return json.load(f).get("items", [])
    except Exception:
        return []


def _save_queue(items):
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, indent=2)


def _queue_upsert(items, path, hot_path, copies):
    name = os.path.basename(path)
    for it in items:
        if it["path"] == path:
            # downloaded again → a fresh (re)print of this sheet
            it.update(assigned_at=_now(), ripped_at=None, printed_at=None, printed_count=0,
                      extra_scans=0, manual=False, hot_path=hot_path, copies=copies,
                      machine=CFG["machine"], operator=CFG["operator"])
            return it
    pf = _parse_name(name)
    it = {
        "id": uuid.uuid4().hex[:10], "path": path, "name": name, "hot_path": hot_path,
        "code": pf["code"] or name, "part": pf["part"], "total": pf["total"], "copies": copies,
        "inch": pf["inch"], "cust": _cust_of(name),
        "machine": CFG["machine"], "operator": CFG["operator"],
        "assigned_at": _now(), "ripped_at": None, "printed_at": None,
        "printed_count": 0, "extra_scans": 0, "manual": False,
    }
    items.append(it)
    return it


_RIP_CACHE = {"mtime": None, "text": None}


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


def _apply_riplog(items):
    """Mark Downloaded items RIP'd when their file name appears in the RIPLOG.
    (Preview shortcut: substring match. The real agent uses RIPLogParser and
    only counts entries newer than assigned_at.)"""
    txt = _riplog_text()
    if txt is None:
        return False
    changed = False
    for it in items:
        if it.get("ripped_at") or it.get("printed_at"):
            continue
        stem = os.path.splitext(it["name"])[0].lower()
        if stem and stem in txt:
            it["ripped_at"] = _now()
            changed = True
    return changed


def _release(path):
    try:
        _post("/api/dropbox/release", {"path": path})
    except Exception:
        pass


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
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(CFG, f, indent=2)
        except Exception as e:
            return {"status": "error", "message": str(e), "config": dict(CFG)}
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

    def list_folder(self, path, fresh=False):
        try:
            return _get(path or CFG["browseRoot"], fresh)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def search(self, q):
        try:
            url = f"{_server()}/api/dropbox/search?q=" + urllib.parse.quote(q or "")
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def print_files(self, items):
        """items: [{path, copies}]. Claim each file (so no other machine grabs
        it), download it once, and drop `copies` files into the hot folder (a
        (2x) order prints twice). Move-to-PRINTED happens on completion in the
        real agent."""
        hot = CFG["hotFolder"]
        os.makedirs(hot, exist_ok=True)
        ok, failed = [], []
        queue = _load_queue()
        PROGRESS.update(active=True, count=len(items), index=0, file="", done=0, total=0)
        for i, it in enumerate(items):
            p = it.get("path")
            copies = max(1, int(it.get("copies", 1) or 1))
            try:
                base = os.path.basename(p)
                PROGRESS.update(index=i + 1, file=base, done=0, total=0)
                _post("/api/dropbox/claim", {"path": p, "machine": CFG["machine"], "operator": CFG["operator"]})
                link = _post("/api/dropbox/temp-link", {"path": p}).get("link")
                if not link:
                    raise RuntimeError("no download link")
                name, ext = os.path.splitext(base)
                first = os.path.join(hot, base)
                _download(link, first)
                ok.append(base)
                for i in range(2, copies + 1):
                    dst = os.path.join(hot, f"{name} (copy {i}){ext}")
                    shutil.copyfile(first, dst)
                    ok.append(os.path.basename(dst))
                _queue_upsert(queue, p, first, copies)  # → Downloaded on this machine
            except Exception as e:
                failed.append({"path": p, "error": str(e)})
        PROGRESS["active"] = False
        _save_queue(queue)
        return {"ok": ok, "failed": failed, "hotFolder": hot}

    def download_progress(self):
        return dict(PROGRESS)

    # ── Queue API ──
    def queue(self):
        items = _load_queue()
        if _apply_riplog(items):
            _save_queue(items)
        rip = _riplog_path()
        return {"status": "ok", "items": items, "machine": CFG["machine"],
                "riplog": {"path": rip, "found": bool(rip and os.path.isfile(rip)),
                           "auto": not (CFG.get("riplog") or "").strip()}}

    def detect_riplog(self):
        """Settings → Detect: find Flexi's live RIPLOG.HTML on this PC."""
        _AUTO_RIPLOG["path"] = None
        p = _find_riplog()
        return {"status": "ok" if p else "notfound", "path": p}

    def queue_scan(self, code):
        """Simulated oven-camera read. Accepts 'PRO3956', 'PRO3956 (2-5)',
        'PRO3956-2/5' or a full file name."""
        s = (code or "").strip()
        pf = _parse_name(s)
        c, part = pf["code"], (pf["part"] if _PART.search(s) else None)
        if not c:
            return {"status": "unknown", "code": s}
        items = _load_queue()
        cands = [it for it in items if it["code"] == c and (part is None or it["part"] == part)]
        if not cands:
            return {"status": "unknown", "code": c, "part": part}
        open_ = [it for it in cands if not it.get("printed_at")]
        if not open_:
            it = cands[0]
            it["extra_scans"] = it.get("extra_scans", 0) + 1
            _save_queue(items)
            return {"status": "already", "item": it}
        it = open_[0]
        it["printed_count"] = it.get("printed_count", 0) + 1
        done = it["printed_count"] >= it.get("copies", 1)
        if done:
            it.update(printed_at=_now(), printed_machine=CFG["machine"], printed_operator=CFG["operator"])
            _move_to_printed(it)  # → <folder>/BASILDI, claim released by the server
        _save_queue(items)
        return {"status": "ok", "item": it, "done": done}

    def queue_action(self, item_id, action):
        items = _load_queue()
        it = next((x for x in items if x["id"] == item_id), None)
        if not it:
            return {"status": "error", "message": "not in queue"}
        try:
            if action == "release":
                _release(it["path"])
                items.remove(it)
            elif action == "remove":
                items.remove(it)
            elif action == "mark_printed":
                it.update(printed_count=it.get("copies", 1), printed_at=_now(), manual=True,
                          printed_machine=CFG["machine"], printed_operator=CFG["operator"])
                _move_to_printed(it)
            elif action == "move_printed":  # retry the BASILDI move
                _move_to_printed(it)
            elif action == "redownload":
                link = _post("/api/dropbox/temp-link", {"path": it["path"]}).get("link")
                if not link:
                    raise RuntimeError("no download link")
                os.makedirs(CFG["hotFolder"], exist_ok=True)
                dest = os.path.join(CFG["hotFolder"], it["name"])
                _download(link, dest)
                it.update(hot_path=dest, assigned_at=_now(), ripped_at=None, printed_at=None,
                          printed_count=0, extra_scans=0, manual=False)
                try:
                    _post("/api/dropbox/claim", {"path": it["path"], "machine": CFG["machine"], "operator": CFG["operator"]})
                except Exception:
                    pass
            else:
                return {"status": "error", "message": "unknown action"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
        _save_queue(items)
        return {"status": "ok", "items": items}

    def queue_clear_done(self):
        items = [it for it in _load_queue() if not it.get("printed_at")]
        _save_queue(items)
        return {"status": "ok", "items": items}


if __name__ == "__main__":
    webview.create_window(
        "DTF Monitor Agent — Preview",
        os.path.join(HERE, "index.html"),
        js_api=Api(),
        width=1180, height=780, min_size=(900, 600),
    )
    webview.start()
