"""
pywebview JS↔Python bridge for the agent UI (ui/index.html).

Everything the UI can do goes through this `Api` class: browse/search Dropbox (via
the server), download files into the hot folder (= Downloaded in the server-side
queue), read this machine's queue, report RIP'd from the local RIPLOG, scan codes
(typed or from the camera), camera status/live view, settings, customer files.
"""
import os
import json
import time
import threading
import urllib.parse
import urllib.error
import webview

from . import core, camera, autostart
from .core import CFG, PROGRESS


class Api:
    # ── settings ──
    def config(self):
        return {k: v for k, v in CFG.items() if k != "agentKey"} | {"agentVersion": core.AGENT_VERSION, "autostartInstalled": autostart.is_installed()}

    def save_config(self, patch):
        cam_changed = False
        rip_changed = False
        auto_changed = False
        for k, v in (patch or {}).items():
            if k not in core.DEFAULTS or k in ("agentKey", "machineId"):
                continue
            if isinstance(v, str):
                v = v.strip()
            if k in core.REQUIRED and (v is None or v == ""):
                continue  # never overwrite a good value with a blank one
            if k == "camera" and str(v) != str(CFG.get(k)):
                cam_changed = True
            if k == "riplog" and str(v) != str(CFG.get(k)):
                rip_changed = True
            if k == "autostart":
                v = bool(v)
                auto_changed = v != bool(CFG.get(k, True))
            CFG[k] = v
        try:
            core.save_cfg(CFG)
        except Exception as e:
            return {"status": "error", "message": str(e), "config": self.config()}
        if rip_changed:
            core.reset_riplog_cache()
            if core.HEARTBEAT:
                core.HEARTBEAT.restart_riplog()
        if cam_changed:
            camera.start()
        if auto_changed or (CFG.get("autostart") and not autostart.is_installed()):
            autostart.apply(bool(CFG.get("autostart")))
        return {"status": "ok", "config": self.config()}

    def pick_folder(self):
        try:
            res = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
            if res:
                return {"status": "ok", "path": res[0]}
        except Exception as e:
            return {"status": "error", "message": str(e)}
        return {"status": "cancel"}

    def pick_file(self):
        try:
            res = webview.windows[0].create_file_dialog(webview.OPEN_DIALOG)
            if res:
                return {"status": "ok", "path": res[0]}
        except Exception as e:
            return {"status": "error", "message": str(e)}
        return {"status": "cancel"}

    def test_connection(self):
        return core.diagnose()

    def detect_riplog(self):
        core.reset_riplog_cache()
        p = core.find_riplog()
        return {"status": "ok" if p else "notfound", "path": p}

    # ── Dropbox (through the server; the token never lives on the PC) ──
    def list_folder(self, path, fresh=False):
        try:
            params = {"path": path or CFG["browseRoot"]}
            if fresh:
                params["fresh"] = 1
            return core.get_json("/api/dropbox/list", params)
        except Exception as e:
            return {"status": "error", "message": core.err_text(e)}

    def search(self, q):
        try:
            return core.get_json("/api/dropbox/search", {"q": q or ""}, timeout=30)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def print_files(self, items):
        """items: [{path, copies, force}]. Starts the job in a background thread and
        returns at once; the UI polls download_progress() until active=False and reads
        `result`. (A long JS→Python call used to die on big batches, and with it the
        rest of the batch.) Claims are taken one by one (first machine wins), then the
        files download 3 at a time, resuming on network drops, and each one is
        registered in this machine's server-side queue as soon as it is on disk."""
        if PROGRESS.get("active"):
            return {"status": "busy"}
        PROGRESS.update(active=True, count=len(items), finished=0, index=0, file="", done=0, total=0,
                        files={}, result=None, started=time.time())
        threading.Thread(target=self._print_files_job, args=(list(items),), daemon=True).start()
        return {"status": "started", "count": len(items)}

    def _print_files_job(self, items):
        from concurrent.futures import ThreadPoolExecutor
        hot = CFG["hotFolder"]
        ok, failed = [], []
        lock = threading.Lock()
        try:
            os.makedirs(hot, exist_ok=True)
            # 1) claim (sequential, fast) — decides which files are ours
            todo = []
            for it in items:
                p = it.get("path")
                try:
                    core.post_json("/api/dropbox/claim", {"path": p, "force": bool(it.get("force")), **core.who()})
                    todo.append(it)
                except urllib.error.HTTPError as he:
                    if he.code == 409:   # another machine took it first — don't download it twice
                        try:
                            by = json.loads(he.read().decode()).get("claimedBy") or {}
                        except Exception:
                            by = {}
                        who = by.get("machine") or "another machine"
                        if by.get("operator"):
                            who += f" ({by['operator']})"
                        failed.append({"path": p, "taken": True, "error": f"taken by {who} {by.get('secondsAgo', 0)} s ago"})
                    else:
                        failed.append({"path": p, "error": core.err_text(he)})
                except Exception as e:
                    failed.append({"path": p, "error": core.err_text(e)})
            # 2) numbers up front so "36-- a", "37-- b" follow the selection order
            n0 = core.next_download_number(hot)
            for i, it in enumerate(todo):
                it["_n"] = n0 + i
            PROGRESS["files"] = {os.path.basename(it["path"]): {"done": 0, "total": 0} for it in todo}

            def one(it):
                p = it["path"]
                base = os.path.basename(p)
                copies = max(1, int(it.get("copies", 1) or 1))
                try:
                    link = core.post_json("/api/dropbox/temp-link", {"path": p}).get("link")
                    if not link:
                        raise RuntimeError("no download link")
                    dest = os.path.join(hot, core.numbered(it["_n"], base))

                    def prog(done, total):
                        with lock:
                            PROGRESS["files"][base] = {"done": done, "total": total}
                            PROGRESS["file"] = base
                    core.download(link, dest, on_progress=prog)
                    # (Nx) copies: ONE file is downloaded; the operator sets copies in Flexi. The
                    # queue still knows `copies`, so the oven expects that many scans.
                    core.post_json("/api/queue/assign", {"path": p, "hot_path": dest, "copies": copies, **core.who()})
                    with lock:
                        ok.append(os.path.basename(dest))
                except Exception as e:
                    with lock:
                        failed.append({"path": p, "error": core.err_text(e)})
                finally:
                    with lock:
                        PROGRESS["finished"] = PROGRESS.get("finished", 0) + 1
            with ThreadPoolExecutor(max_workers=3) as ex:
                list(ex.map(one, todo))
        except Exception as e:
            failed.append({"error": core.err_text(e)})
        finally:
            PROGRESS["result"] = {"ok": ok, "failed": failed, "hotFolder": hot}
            PROGRESS["active"] = False

    def download_progress(self):
        p = dict(PROGRESS)
        files = dict(p.get("files") or {})
        p["done"] = sum(f["done"] for f in files.values())
        p["total"] = sum(f["total"] for f in files.values())
        p["known"] = sum(1 for f in files.values() if f["total"])
        p["files"] = files
        return p

    # ── queue (server-side) ──
    def _my_queue(self):
        return core.get_json("/api/queue", {"machine": CFG["machine"]}).get("items", [])

    def queue(self):
        """This machine's queue; flips Downloaded → RIP'd for files the local RIPLOG
        shows RIP'd after they were downloaded (the server tells Order Tracker)."""
        try:
            items = self._my_queue()
        except Exception as e:
            return {"status": "error", "message": str(e)}
        hb = core.HEARTBEAT
        if hb is not None and hb.state.get("riplog_active"):
            for idx, it in enumerate(items):
                if it.get("ripped_at") or it.get("printed_at"):
                    continue
                local = os.path.basename(it.get("hot_path") or "") or it["name"]   # the numbered local file
                if hb.ripped_after(local, it.get("assigned_at") or ""):
                    try:
                        r = core.post_json("/api/queue/ripped", {"id": it["id"]})
                        if r.get("item"):
                            items[idx] = r["item"]
                    except Exception:
                        pass
        rip = core.riplog_path()
        return {"status": "ok", "items": items, "machine": CFG["machine"],
                "riplog": {"path": rip, "found": bool(rip and os.path.isfile(rip)),
                           "auto": not (CFG.get("riplog") or "").strip()}}

    def queue_history(self):
        try:
            return core.get_json("/api/queue/history", {"machine": CFG["machine"], "limit": 300})
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def queue_scan(self, code):
        try:
            return core.post_json("/api/queue/scan", {"code": (code or "").strip(), **core.who()})
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def queue_action(self, item_id, action):
        try:
            if action == "redownload":
                it = next((x for x in self._my_queue() if str(x["id"]) == str(item_id)), None)
                if not it:
                    return {"status": "error", "message": "not in queue"}
                link = core.post_json("/api/dropbox/temp-link", {"path": it["path"]}).get("link")
                if not link:
                    raise RuntimeError("no download link")
                os.makedirs(CFG["hotFolder"], exist_ok=True)
                dest = os.path.join(CFG["hotFolder"], core.numbered(core.next_download_number(CFG["hotFolder"]), it["name"]))
                core.download(link, dest)
                core.post_json("/api/queue/assign", {"path": it["path"], "hot_path": dest,
                                                     "copies": it.get("copies", 1), **core.who()})
                return {"status": "ok", "items": self._my_queue()}
            return core.post_json("/api/queue/action", {"id": item_id, "action": action, **core.who()})
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def queue_clear_done(self):
        try:
            return core.post_json("/api/queue/clear", {"machine": CFG["machine"]})
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ── oven camera ──
    def camera_status(self):
        return camera.status()

    def camera_events(self):
        return camera.events()

    def camera_frame(self):
        return camera.frame_b64()

    def camera_live(self, on):
        camera.live(bool(on))
        return {"status": "ok"}

    def camera_restart(self):
        camera.start()
        return camera.status()

    # ── legacy: connection state, history, customer files ──
    def agent_state(self):
        hb = core.HEARTBEAT
        st = hb.state if hb else {}
        return {"connected": bool(st.get("connected")), "error": st.get("error", ""),
                "riplogActive": bool(st.get("riplog_active")), "latestVersion": st.get("latest_version", ""),
                "version": core.AGENT_VERSION, "history": st.get("history", []),
                "customerFiles": st.get("customer_files", [])}

    def download_customer_file(self, file_id, original_filename):
        """Customer-portal file assigned to this machine → Save As… (legacy tab)."""
        try:
            name, ext = os.path.splitext(original_filename or "file")
            res = webview.windows[0].create_file_dialog(webview.SAVE_DIALOG, save_filename=original_filename or "file")
            path = res[0] if isinstance(res, (list, tuple)) else res
            if not path:
                return {"status": "cancel"}
            url = f"{core.server()}/api/agent/customer-files/{urllib.parse.quote(str(file_id))}/download"
            core.download(url, path, timeout=120)
            return {"status": "ok", "path": path}
        except Exception as e:
            return {"status": "error", "message": str(e)}
