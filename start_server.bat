@echo off
echo Starting DTF Floor Monitor Server...
echo Dashboard will be available at http://localhost:8090
echo.
cd /d "%~dp0server"
python server.py
pause
