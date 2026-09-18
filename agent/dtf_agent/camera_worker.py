"""
Oven-camera worker: owns the webcam in its own process and reports QR reads.

Runs as a child of the agent (preview.py) because Windows' MSMF capture is
unreliable when the device is opened from a non-main thread — here it is opened
on this process's main thread, so the agent's UI thread is never involved.

Protocol (stdout, one JSON object per line):
  {"event":"status","status":"starting|live|error","frames":N,"error":"...","index":I}
  {"event":"code","code":"PRO3956 (2-5)"}              # a new QR read (once per appearance in view)
A small preview JPEG is written atomically to --preview every ~0.5 s.

Usage: python camera_worker.py --index 0 --preview C:\\path\\preview.jpg [--debounce 60]
"""
import argparse
import json
import os
import sys
import time

try:
    import cv2
except Exception as e:  # pragma: no cover
    print(json.dumps({"event": "status", "status": "error", "frames": 0,
                      "error": f"opencv-python not installed: {e}"}), flush=True)
    sys.exit(2)


LOG = {"path": ""}


def emit(obj):
    line = json.dumps(obj)
    try:
        # stdout is a pipe to the agent; in a windowed exe started by hand it may be
        # closed/invalid (Errno 22) — never let logging crash the worker
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:
        pass
    if LOG["path"]:
        try:
            with open(LOG["path"], "a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S ") + line + "\n")
        except Exception:
            pass


def open_any(index):
    """Open the configured index; if that fails, try 0..3 — a replugged USB camera can
    come back under a different index. Returns (cap, index_used) or (None, index)."""
    cap = open_cam(index)
    if cap is not None:
        return cap, index
    for i in range(0, 4):
        if i == index:
            continue
        cap = open_cam(i)
        if cap is not None:
            emit({"event": "info", "msg": f"camera {index} not found, using camera {i}"})
            return cap, i
    return None, index


def open_cam(index):
    # Note: MSMF rejects CAP_PROP_*_TIMEOUT_MSEC (property 53), so no timeouts here;
    # the parent restarts this process if it stops reporting.
    # Resolution goes in the constructor: every cap.set() afterwards makes MSMF
    # renegotiate the stream (~6 s each on a C920), so setting it here saves ~12 s.
    t0 = time.time()
    try:
        # 720p @ 10 fps: decoding the camera stream is the main CPU cost, and a 2.5 cm QR at
        # 20-30 cm is still ~150 px wide at 720p - plenty for the detector.
        cap = cv2.VideoCapture(index, cv2.CAP_MSMF, [cv2.CAP_PROP_FRAME_WIDTH, 1280, cv2.CAP_PROP_FRAME_HEIGHT, 720, cv2.CAP_PROP_FPS, 10])
    except Exception:
        cap = cv2.VideoCapture(index, cv2.CAP_MSMF)
    if not cap.isOpened():
        cap.release()
        return None
    w, h = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    if (w, h) != (1280.0, 720.0):  # constructor params ignored → fall back to set()
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        cap.set(cv2.CAP_PROP_FPS, 10)
    fps = cap.get(cv2.CAP_PROP_FPS) or 0
    if fps > 15:  # constructor FPS ignored → ask again (one ~6 s renegotiation, once per start)
        cap.set(cv2.CAP_PROP_FPS, 10)
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
    emit({"event": "info", "msg": f"opened in {time.time()-t0:.1f}s at {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))} @ {fps:.0f} fps"})
    return cap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--preview", default="")
    ap.add_argument("--debounce", type=float, default=3.0)  # seconds a code must be OUT of view before it counts again
    ap.add_argument("--stopfile", default="")   # parent creates this file to ask for a clean exit
    ap.add_argument("--seconds", type=float, default=0)  # tests: exit cleanly after N seconds
    ap.add_argument("--debug", action="store_true")     # per-read timing on stderr
    ap.add_argument("--log", default="")                # also append events to this file
    a = ap.parse_args()
    t_start = time.time()
    LOG["path"] = a.log
    if a.log:
        try:
            if os.path.exists(a.log) and os.path.getsize(a.log) > 200_000:
                os.remove(a.log)
        except Exception:
            pass
    def dbg(msg):
        if a.debug:
            print(f"[{time.time()-t_start:6.2f}s] {msg}", file=sys.stderr, flush=True)

    try:  # run below normal priority: printing/RIP always comes first on this PC
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL
    except Exception:
        pass
    emit({"event": "status", "status": "starting", "frames": 0, "error": "", "index": a.index})
    dbg("opening camera")
    cap, a.index = open_any(a.index)
    dbg(f"open done -> {cap is not None}")
    if cap is None:
        emit({"event": "status", "status": "error", "frames": 0, "index": a.index,
              "error": f"camera {a.index} could not be opened (in use by another app?)"})
        sys.exit(3)

    cv2.setNumThreads(1)  # one quiet core-slice, not a burst of threads on the printer PC
    det = cv2.QRCodeDetector()
    prev_small = None      # motion gate: skip detection while the scene is static
    last_detect = 0.0
    seen = {}
    frames = 0
    misses = 0
    last_status = 0.0
    last_preview = 0.0
    want_preview = False   # only while the UI has the Live panel open (flag file next to --preview)
    last_flag_check = 0.0
    status = "starting"
    while True:
        if a.stopfile and os.path.exists(a.stopfile):
            break
        if a.seconds and time.time() - t_start > a.seconds:
            break
        _t = time.time()
        ret, frame = cap.read()
        now = time.time()
        if a.debug and (frames < 5 or not ret):
            dbg(f"read ret={ret} took {now-_t:.3f}s frames={frames} misses={misses}")
        if not ret:
            misses += 1
            if misses > 100:  # ~3-5 s without frames: the device is gone (USB unplugged?)
                # Exit instead of reopening in-process: MSMF only comes back cleanly in a
                # fresh process. The agent restarts the worker in a few seconds.
                try:
                    cap.release()
                except Exception:
                    pass
                emit({"event": "status", "status": "error", "frames": frames, "index": a.index,
                      "error": "camera stopped delivering frames (unplugged?) — reconnecting"})
                sys.exit(5)
            time.sleep(0.03)
        else:
            misses = 0
            frames += 1
            if status != "live":
                status = "live"
                emit({"event": "status", "status": status, "frames": frames, "error": "", "index": a.index})
            # Motion gate: a 160x90 grayscale diff (~0.1 ms) decides whether anything moved.
            # Detection (~15-40 ms) runs at most 4x/s while the film is moving, plus one
            # sweep every 3 s regardless — so an idle oven costs practically nothing.
            small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90), interpolation=cv2.INTER_AREA)
            moving = prev_small is not None and float(cv2.absdiff(small, prev_small).mean()) > 2.0
            prev_small = small
            if (moving and now - last_detect > 0.25) or now - last_detect > 3.0:
                last_detect = now
                try:
                    ok, codes, _pts, _ = det.detectAndDecodeMulti(frame)
                except Exception:
                    ok, codes = False, []
                if ok:
                    for code in codes:
                        code = (code or "").strip()
                        if not code:
                            continue
                        # re-arm logic: a code counts once while it stays in view; after it has
                        # been out of view for `debounce` seconds, seeing it again is a new sheet
                        if now - seen.get(code, 0) > a.debounce:
                            emit({"event": "code", "code": code})
                        seen[code] = now
            if a.preview and now - last_flag_check > 1.0:
                last_flag_check = now
                want_preview = os.path.exists(a.preview + ".want")
            if a.preview and want_preview and now - last_preview > 0.1:  # ~10 fps = capture rate
                last_preview = now
                try:
                    small = cv2.resize(frame, (640, 360), interpolation=cv2.INTER_AREA)
                    tmp = a.preview[:-4] + ".tmp.jpg" if a.preview.lower().endswith(".jpg") else a.preview + ".tmp.jpg"  # imwrite picks the codec from the extension
                    if cv2.imwrite(tmp, small, [cv2.IMWRITE_JPEG_QUALITY, 60]):
                        os.replace(tmp, a.preview)
                except Exception:
                    pass
        if now - last_status > 1.0:
            last_status = now
            emit({"event": "status", "status": status, "frames": frames, "error": "", "index": a.index})
    # clean release: an abruptly killed process leaves the device wedged for a while
    cap.release()
    emit({"event": "status", "status": "off", "frames": frames, "error": "", "index": a.index})


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
