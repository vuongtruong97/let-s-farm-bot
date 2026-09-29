@echo off
cd /d "%~dp0"

echo ========================================
echo   Let's Farm Bot
echo ========================================

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment...
    python -m venv .venv
)

echo [2/3] Installing dependencies...
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt

echo [3/3] Starting web...
python -m app.main web

pause