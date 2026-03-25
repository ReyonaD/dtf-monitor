@echo off
echo Building DTF Monitor Agent...
pyinstaller --onefile --windowed --name "DTF-Monitor-Agent" --icon=NONE agent.py
echo.
echo Build complete! Find the .exe in the dist\ folder.
pause
