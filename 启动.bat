@echo off
rem Launch DailyWallpaper with no console window.
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" -m dailywallpaper %*
