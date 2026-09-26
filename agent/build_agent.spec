# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the NEW agent (pywebview UI + RIPLOG heartbeat + oven camera).
#   pyinstaller build_agent.spec
# Output: dist/DTF-Monitor-Agent.exe (one file, windowed). The oven-camera worker is
# the same exe started with --camera-worker, so OpenCV ships inside it.
# UPX is OFF: it corrupts OpenCV / pythoncom DLLs.

import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hidden = (
    collect_submodules("webview")            # pywebview backends (edgechromium, winforms)
    + collect_submodules("numpy")            # OpenCV's bindings import numpy lazily — PyInstaller misses it
    + ["cv2", "certifi"]
    + ["clr", "pythoncom", "pywintypes", "win32api", "win32con", "win32gui"]
    + ["dtf_agent", "dtf_agent.core", "dtf_agent.bridge", "dtf_agent.camera",
       "dtf_agent.camera_worker", "dtf_agent.riplog", "dtf_agent.tray", "dtf_agent.autostart"]
    + collect_submodules("pystray") + ["PIL.Image", "PIL.ImageDraw"]
)
datas = [(os.path.join("dtf_agent", "ui"), os.path.join("dtf_agent", "ui"))]
datas += collect_data_files("webview")       # WebView2 loader dll etc.
datas += collect_data_files("numpy", include_py_files=False)
datas += collect_data_files("certifi")     # cacert.pem — TLS roots independent of the PC's store

a = Analysis(
    ["agent_main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "scipy", "PIL.ImageTk"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DTF-Monitor-Agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="agent_icon.ico",
)
