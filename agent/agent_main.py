"""
DTF Monitor Agent — entry point (replaces the tkinter agent.py).

  python agent_main.py                 run the agent (pywebview UI)
  DTF-Monitor-Agent.exe                same, packaged (PyInstaller, see build_agent.spec)
  <exe|python agent_main.py> --camera-worker …   internal: the oven-camera child process

Startup: load config (compatible with the old config.json) → first-run setup if the
machine name / hot folder are missing → heartbeat (RIPLOG → floor dashboard) →
camera worker → window.
"""
import os
import sys


def _run_camera_worker(argv):
    from dtf_agent import camera_worker
    sys.argv = ["camera_worker"] + argv
    camera_worker.main()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--camera-worker":
        _run_camera_worker(sys.argv[2:])
        return

    import webview
    from dtf_agent import core, camera
    from dtf_agent.bridge import Api

    ui_dir = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "dtf_agent", "ui")
    index = os.path.join(ui_dir, "index.html")

    api = Api()
    from dtf_agent import autostart
    if core.is_configured():
        autostart.apply(bool(core.CFG.get("autostart", True)))  # keep the Startup shortcut in sync
    core.start_heartbeat()
    if core.is_configured():
        camera.start()
    import atexit
    atexit.register(camera.stop)

    title = f"DTF Monitor Agent {core.AGENT_VERSION}"
    win = webview.create_window(title, index, js_api=api, width=1180, height=780, min_size=(900, 600))

    # ── Close (X) hides to the system tray instead of quitting: the agent must keep
    # heartbeating / watching the camera all day. Quit only from the tray menu.
    from dtf_agent import tray
    tray.install(win, title, on_quit=lambda: _shutdown(win))

    def on_loaded():
        # First run: open Settings with a "welcome" hint so the operator fills in
        # machine name / operator / hot folder; RIPLOG is auto-detected.
        if not core.is_configured():
            try:
                win.evaluate_js("window.__firstRun && window.__firstRun()")
            except Exception:
                pass
    win.events.loaded += on_loaded

    def on_closing():
        if tray.quitting():
            return True          # Quit from the tray menu: really close
        tray.hide_to_tray(win)   # X: hide, keep running
        return False
    win.events.closing += on_closing

    webview.start()
    _shutdown(None)


def _shutdown(win):
    from dtf_agent import core, camera, tray
    try:
        if core.HEARTBEAT:
            core.HEARTBEAT.stop()
    except Exception:
        pass
    camera.stop()
    tray.remove()
    if win is not None:
        try:
            win.destroy()
        except Exception:
            pass


if __name__ == "__main__":
    main()
