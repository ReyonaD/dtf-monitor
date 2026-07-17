@echo off
:: DTF Monitor Server - Auto Start (runs hidden, restarts on crash)
cd /d "%~dp0server"

:loop
echo [%date% %time%] Starting DTF Monitor Server...
python server.py
echo [%date% %time%] Server stopped. Restarting in 5 seconds...
timeout /t 5 /noq
goto loop
