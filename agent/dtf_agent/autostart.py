"""
Start-with-Windows: a shortcut in the user's Startup folder that points at this
exe (same mechanism the old install_autostart.bat used, so a PC that already has
the shortcut keeps it). No admin rights needed. Only applies to the packaged exe.
"""
import os
import sys
import subprocess

from . import core

SHORTCUT_NAME = "DTF-Monitor-Agent.lnk"


def shortcut_path():
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup", SHORTCUT_NAME)


def is_installed():
    return os.path.isfile(shortcut_path())


def apply(enabled: bool) -> str:
    """Create/refresh (enabled) or remove (disabled) the Startup shortcut. Returns a status text."""
    if not core.FROZEN:
        return "dev mode - autostart not touched"
    lnk = shortcut_path()
    if not enabled:
        try:
            if os.path.isfile(lnk):
                os.remove(lnk)
            return "removed"
        except Exception as e:
            return f"could not remove: {e}"
    exe = os.path.abspath(sys.executable)
    q = lambda s: s.replace("'", "''")
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
          "$s.TargetPath='%s';$s.WorkingDirectory='%s';$s.Description='DTF Monitor Agent';$s.Save()"
          % (q(lnk), q(exe), q(os.path.dirname(exe))))
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       creationflags=flags, timeout=20, capture_output=True)
        return "installed" if os.path.isfile(lnk) else "failed"
    except Exception as e:
        return f"failed: {e}"
