@echo off
REM ---------------------------------------------------------------------------
REM  VFS Slot Watcher - Windows launcher
REM
REM  Double-click this file, or run it from a command prompt in this folder.
REM  It sets up the virtual environment on first run, then starts the dashboard
REM  on http://127.0.0.1:8787 and opens it in your browser.
REM
REM  Needs: Python 3.11+ (tick "Add python.exe to PATH" when installing) and a
REM  real Brave or Chrome. There is no AI and no API key anywhere in this - it is
REM  plain Python that drives a browser.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Python was not found.
    echo   Install it from https://www.python.org/downloads/ and tick
    echo   "Add python.exe to PATH" on the first screen, then run this again.
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating the virtual environment (first run only^)...
    python -m venv .venv || goto :failed
)

echo Installing / updating dependencies...
call ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
call ".venv\Scripts\python.exe" -m pip install --quiet -e . || goto :failed

REM Playwright's own Chromium is blocked by VFS, but the driver still wants a browser present.
call ".venv\Scripts\python.exe" -m playwright install chromium >nul 2>&1

REM --- is a real browser installed? the bot cannot work without one ------------
call ".venv\Scripts\python.exe" -c "from vfsbot.watcher import find_browser; import sys; b=find_browser(); print('Using browser:', b) if b else sys.exit(3)"
if errorlevel 3 (
    echo.
    echo   No Brave or Chrome found.
    echo   Install Brave from https://brave.com/download/  ^(or Chrome^), or put the
    echo   full path in config.yaml:
    echo.
    echo       browser:
    echo         executable: C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe
    echo.
    pause
    exit /b 1
)

if not exist "data" mkdir data
if not exist "state" mkdir state

echo.
echo   Dashboard: http://127.0.0.1:8787
echo   Close this window to stop it.
echo.
start "" http://127.0.0.1:8787
call ".venv\Scripts\python.exe" -m vfsbot.cli ui
exit /b 0

:failed
echo.
echo   Setup failed - see the messages above.
pause
exit /b 1
