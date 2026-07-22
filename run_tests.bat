@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Python virtual environment was not found.
  echo Create it with: py -3.11 -m venv .venv
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m ruff check app tests migrations
if errorlevel 1 (
  echo [ERROR] Ruff check failed or Ruff is not installed.
  echo Install development dependencies with:
  echo   .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
  exit /b 1
)

".venv\Scripts\python.exe" -m compileall -q app tests migrations
if errorlevel 1 exit /b 1

".venv\Scripts\python.exe" -m pytest -q
if errorlevel 1 exit /b 1

endlocal
