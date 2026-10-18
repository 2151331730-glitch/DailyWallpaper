@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
  echo ==================================================
  echo  [ERROR] .venv not found in this folder.
  echo.
  echo  Run these once in this folder:
  echo      py -3.13 -m venv .venv
  echo      .venv\Scripts\python -m pip install -e ".[dev]"
  echo ==================================================
  pause
  exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" -m dailywallpaper %*
