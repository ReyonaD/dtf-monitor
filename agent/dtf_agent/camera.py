"""
Oven-camera supervisor: runs camera_worker in a CHILD PROCESS and forwards its QR
reads to the server (/api/queue/scan — the same call the "type a code" box makes).

Why a process: Windows' MSMF capture only behaves when the device is opened on a
process's main thread, and pywebview owns this process's main thread. The worker
prints one JSON line per event ({"event":"code"} once per appearance of a QR in
view; {"event":"status"} once a second) and writes a small preview JPEG while the
UI's Live panel is open. This supervisor keeps the last status for the UI and
restarts the worker if it dies or goes quiet.

Packaged (PyInstaller): the worker is THIS SAME EXE started with `--camera-worker`
(see agent_main.py) — no separate Python needed on the printer PC.
"""
import os
import sys
import json
import time
import threading
import subprocess

from . import core

SCAN_DEBOUNCE_S = 10  # a code must leave the camera's view this long before it counts again

_RUN_DIR = core.app_dir()
CAM_PREVIEW = os.path.join(_RUN_DIR, "camera_preview.jpg")
CAM_STOPFILE = os.path.join(_RUN_DIR, "camera_stop.flag")
CAM_LOG = os.path.join(_RUN_DIR, "camera_worker.log")

CAM = {"status": "off", "error": "", "index": None, "frames": 0, "last_code": "", "last_at": 0.0,
       "events": [], "lock": threading.Lock(), "proc": None, "gen": 0, "last_line": 0.0}
_sup_started = False


def _worker_cmd(idx):
    args = ["--index", str(idx), "--preview", CAM_PREVIEW, "--debounce", str(SCAN_DEBOUNCE_S),
            "--stopfile", CAM_STOPFILE, "--log", CAM_LOG]
    if core.FROZEN:
        return [sys.executable, "--camera-worker"] + args
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")  # no console behind the agent
    if exe.lower().endswith("python.exe") and os.path.isfile(cand):
        exe = cand
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camera_worker.py")
    return [exe, "-u", worker] + args


def _stop_proc(proc):
    """Ask the worker to release the camera and exit; force it only if it ignores us
    (an abruptly killed worker leaves the device wedged for a while)."""
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


def _scan(code):
    try:
        r = core.post_json("/api/queue/scan", {"code": code, **core.who()})
    except Exception as e:
        r = {"status": "error", "message": str(e)[:120]}
    with CAM["lock"]:
        CAM.update(last_code=code, last_at=time.time())
        CAM["events"].append({"code": code, "result": r, "at": time.time()})
        del CAM["events"][:-20]


def _reader(proc, gen):
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
            _scan(str(ev["code"]).strip())
        elif ev.get("event") == "status":
            CAM.update(status=ev.get("status", "?"), error=ev.get("error", "") or ev.get("note", ""),
                       frames=int(ev.get("frames") or 0), index=ev.get("index"))
    proc.wait()
    if CAM["gen"] == gen and CAM["proc"] is proc:
        rc = proc.returncode
        if rc == 3:      # could not open the device
            CAM.update(status="error", error="no camera found — plug in the webcam (retrying)")
            CAM["retry_at"] = time.time() + 10
        elif rc == 5:    # device lost mid-stream (USB unplugged)
            CAM.update(status="error", error="camera disconnected — reconnecting…")
            CAM["retry_at"] = time.time() + 4
        else:
            CAM.update(status="error", error=f"camera worker exited (code {rc}); restarting…")
            CAM["retry_at"] = time.time() + 5


def _supervisor():
    while True:
        time.sleep(5)
        proc = CAM["proc"]
        if proc is None:
            continue
        dead = proc.poll() is not None
        # A worker whose read() is wedged (device yanked) stops reporting: 20 s of silence
        # while live means gone; while still opening, allow ~45 s (MSMF open can be slow).
        silent = time.time() - CAM["last_line"] if CAM["last_line"] else 0
        quiet = silent > 30   # the worker reports every ≤2 s even while opening
        if dead and time.time() < CAM.get("retry_at", 0):
            continue
        if dead or quiet:
            if quiet and not dead:
                CAM.update(status="error", error="camera stopped responding — reconnecting…")
                _stop_proc(proc)
                time.sleep(4)   # let MSMF release the device before a fresh open
            start()


def start():
    """(Re)start the camera worker from CFG["camera"] ("0", "1", … or "off")."""
    global _sup_started
    old = CAM["proc"]
    CAM["gen"] += 1
    CAM["proc"] = None
    _stop_proc(old)
    sel = str(core.CFG.get("camera") or "off").strip().lower()
    if sel in ("", "off", "none", "no"):
        CAM.update(status="off", error="", index=None, frames=0)
        return
    try:
        idx = int(sel)
    except ValueError:
        CAM.update(status="error", error=f'camera setting "{sel}" is not a number (use 0, 1, … or off)')
        return
    CAM.update(status="starting", error="", index=idx, frames=0, last_line=time.time())
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(_worker_cmd(idx), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, creationflags=flags, cwd=_RUN_DIR)
    except Exception as e:
        CAM.update(status="error", error=f"could not start camera worker: {e}")
        return
    CAM["proc"] = proc
    threading.Thread(target=_reader, args=(proc, CAM["gen"]), daemon=True).start()
    if not _sup_started:
        _sup_started = True
        threading.Thread(target=_supervisor, daemon=True).start()


def stop():
    CAM["gen"] += 1
    proc, CAM["proc"] = CAM["proc"], None
    _stop_proc(proc)
    CAM.update(status="off")


def status():
    return {"status": CAM["status"], "error": CAM["error"], "index": CAM["index"], "frames": CAM["frames"],
            "lastCode": CAM["last_code"], "lastAt": CAM["last_at"], "available": True}


def events():
    with CAM["lock"]:
        ev, CAM["events"] = list(CAM["events"]), []
    return ev


def frame_b64():
    import base64
    if CAM["status"] != "live":
        return None
    try:
        with open(CAM_PREVIEW, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception:
        return None


def live(on):
    """UI opened/closed the Live panel: the worker only encodes preview frames while open."""
    flag = CAM_PREVIEW + ".want"
    try:
        if on:
            open(flag, "w").close()
        elif os.path.exists(flag):
            os.remove(flag)
    except Exception:
        pass
