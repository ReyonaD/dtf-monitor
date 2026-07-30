@echo off
:: ---------------------------------------------------------------------------
:: DTF Monitor Agent - install auto-start (runs on Windows login)
:: Put this .bat in the SAME folder as DTF-Monitor-Agent.exe (and its
:: config.json), then double-click it once. It creates a shortcut in the
:: Startup folder so the agent launches automatically every time you log in.
:: ---------------------------------------------------------------------------
setlocal
set "EXE=%~dp0DTF-Monitor-Agent.exe"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"

if not exist "%EXE%" (
  echo ERROR: DTF-Monitor-Agent.exe not found next to this file.
  echo Put install_autostart.bat in the same folder as the .exe and try again.
  pause
  exit /b 1
)

powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%STARTUP%\DTF-Monitor-Agent.lnk'); $s.TargetPath='%EXE%'; $s.WorkingDirectory='%~dp0'; $s.Save()"

echo.
echo Done. The agent will now start automatically on login.
echo Shortcut created at:
echo   %STARTUP%\DTF-Monitor-Agent.lnk
echo.
echo To remove auto-start later, delete that shortcut (Win+R -> shell:startup).
pause
