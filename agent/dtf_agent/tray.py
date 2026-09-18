"""
System-tray icon for the agent: X hides the window here; the tray menu re-opens it
or quits. Uses pystray (Win32 notification area) with a small generated icon.
"""
import threading

_icon = None
_win = None
_quit_cb = None


def _make_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(124, 58, 237, 255))  # agent purple
    d.text((22, 14), "D", fill=(255, 255, 255, 255), font_size=36)
    return img


def show_window(*_):
    if _win is None:
        return
    try:
        _win.show()
        _win.restore()
    except Exception:
        pass


def hide_to_tray(win):
    try:
        win.hide()
    except Exception:
        pass
    if _icon is not None:
        try:
            _icon.notify("Still running in the background — the queue and the oven camera stay on.",
                         "DTF Monitor Agent")
        except Exception:
            pass


_quitting = False


def quitting():
    return _quitting


def _quit(*_):
    global _quitting
    _quitting = True
    cb = _quit_cb
    if cb:
        threading.Thread(target=cb, daemon=True).start()


def install(win, title, on_quit):
    """Create the tray icon (its own thread). Menu: Open · Quit."""
    global _icon, _win, _quit_cb
    _win, _quit_cb = win, on_quit
    try:
        import pystray
        _icon = pystray.Icon("dtf-monitor-agent", _make_image(), title,
                             menu=pystray.Menu(
                                 pystray.MenuItem("Open DTF Monitor Agent", show_window, default=True),
                                 pystray.MenuItem("Quit agent", _quit),
                             ))
        threading.Thread(target=_icon.run, daemon=True).start()
    except Exception:
        _icon = None  # no tray available: X will still hide; relaunching the exe re-shows


def remove():
    global _icon
    if _icon is not None:
        try:
            _icon.stop()
        except Exception:
            pass
        _icon = None
