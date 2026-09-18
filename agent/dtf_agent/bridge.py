"""
pywebview JS↔Python bridge for the agent UI (ui/index.html).

Everything the UI can do goes through this `Api` class: browse/search Dropbox (via
the server), download files into the hot folder (= Downloaded in the server-side
queue), read this machine's queue, report RIP'd from the local RIPLOG, scan codes
(typed or from the camera), camera status/live view, settings, customer files.
"""
import os
import shutil
import urllib.parse
import webview

from . import core, camera
from .core import CFG, PROGRESS


class Api:
    # ── settings ──
    def config(self):
        return {k: v for k, v in CFG.items() if k != "agentKey"} | {"agentVersion": core.AGENT_VERSION}

    def save_config(self, patch):
        cam_changed = False
        rip_changed = False
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
            return {"status": "error", "message": str(e)}

    def search(self, q):
        try:
            return core.get_json("/api/dropbox/search", {"q": q or ""}, timeout=30)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def print_files(self, items):
        """items: [{path, copies}]. Claim each file (so no other machine grabs it),
        download it once, drop `copies` files into the hot folder (a (2x) order
        prints twice) and register it in this machine's server-side queue."""
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
                core.post_json("/api/dropbox/claim", {"path": p, **core.who()})
                link = core.post_json("/api/dropbox/temp-link", {"path": p}).get("link")
                if not link:
                    raise RuntimeError("no download link")
                name, ext = os.path.splitext(base)
                first = os.path.join(hot, base)
                core.download(link, first)
                ok.append(base)
                for c in range(2, copies + 1):
                    dst = os.path.join(hot, f"{name} (copy {c}){ext}")
                    shutil.copyfile(first, dst)
                    ok.append(os.path.basename(dst))
                core.post_json("/api/queue/assign", {"path": p, "hot_path": first, "copies": copies, **core.who()})
            except Exception as e:
                failed.append({"path": p, "error": str(e)})
        PROGRESS["active"] = False
        return {"ok": ok, "failed": failed, "hotFolder": hot}

    def download_progress(self):
        return dict(PROGRESS)

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
                if hb.ripped_after(it["name"], it.get("assigned_at") or ""):
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
                dest = os.path.join(CFG["hotFolder"], it["name"])
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
