@echo off
rem Let's Farm Bot in a console window, with the log scrolling live.
rem Close the tray version first: both use port 48721.
cd /d "%~dp0"

echo ========================================
echo   Let's Farm Bot (console)
echo ========================================

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment...
    python -m venv .venv || goto :failed
)

rem Reinstall only when requirements.txt changed since the last install.
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1
if errorlevel 1 (
    echo [2/3] Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :failed
    copy /y requirements.txt ".venv\requirements.installed" >nul
)

echo [3/3] Starting web...
".venv\Scripts\python.exe" -m app.main web
pause
exit /b 0

:failed
echo.
echo Setup failed - see the error above.
pause
exit /b 1
