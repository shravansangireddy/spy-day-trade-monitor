@echo off
title SPY Day-Trade Monitor (port 8765)
cd /d "%~dp0"

if exist "%~dp0.venv\Scripts\python.exe" (
  set "PY=%~dp0.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

echo Starting SPY Day-Trade Monitor on port 8765...
echo   Repo root: %~dp0
echo   Python:    %PY%
echo.
"%PY%" main.py
if errorlevel 1 (
  echo.
  echo Failed — install deps first:
  echo   "%PY%" -m pip install -r requirements.txt
  pause
)
