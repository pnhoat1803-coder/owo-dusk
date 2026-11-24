@echo off
setlocal EnableExtensions
title OwO-Dusk Dashboard

rem Runs the web dashboard on its own, without starting the bot. The dashboard can then start, stop and restart the
rem bot from the page. The address, the port and the refresh interval come from config\global_settings.json
rem (website.host, website.port, website.refreshInterval).
rem Usage:  dashboard.bat          settings from the config
rem         dashboard.bat 1201     another port, for example when the bot's own dashboard already uses 1200
rem Closing this window stops the dashboard. The bot keeps running unless website.stopBotWithDashboard is true.
set "PORT_ARG=%~1"

cd /d "%~dp0"
if errorlevel 1 (
    echo [x] Cannot enter the project folder.
    pause
    exit /b 1
)

rem Find Python the same way setup.bat does: the py launcher first, then python on PATH, then a Python 3 install folder.
set "PY_CMD="
set "PY_ARG="
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=py"
    set "PY_ARG=-3"
)
if not defined PY_CMD (
    for /f "delims=" %%W in ('where python 2^>nul') do (
        if not defined PY_CMD (
            echo %%W | findstr /i "WindowsApps" >nul 2>&1
            if errorlevel 1 (
                "%%W" -c "import sys" >nul 2>&1
                if not errorlevel 1 set "PY_CMD=%%W"
            )
        )
    )
)
if not defined PY_CMD (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
        if exist "%%D\python.exe" set "PY_CMD=%%D\python.exe"
    )
)
if not defined PY_CMD (
    echo [x] Python was not found on this PC. Install Python 3.12 or newer from https://www.python.org/downloads/
    echo     and tick "Add Python to PATH", then run this again.
    pause
    exit /b 1
)

if not exist "config\global_settings.json" (
    echo [x] config\global_settings.json was not found. Keep dashboard.bat in the project folder.
    pause
    exit /b 1
)

set "PORT="
set "REFRESH=15"
for /f "usebackq tokens=1,2 delims= " %%A in (`"%PY_CMD%" %PY_ARG% -c "import json; w=json.load(open('config/global_settings.json', encoding='utf-8'))['website']; print(w['port'], w.get('refreshInterval', 15))"`) do (
    set "PORT=%%A"
    set "REFRESH=%%B"
)
if defined PORT_ARG set "PORT=%PORT_ARG%"
if not defined PORT set "PORT=1200"
echo %PORT%| findstr /r "^[0-9][0-9]*$" >nul
if errorlevel 1 (
    echo [x] The port must be a number. Usage: dashboard.bat 1201
    pause
    exit /b 1
)

rem Windows lets a second server bind a port that is already in use, so look for a listener first
"%PY_CMD%" %PY_ARG% -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1', %PORT%))==0 else 1)"
if not errorlevel 1 (
    echo [x] Port %PORT% is already in use. The bot's dashboard may be running.
    echo     Open http://127.0.0.1:%PORT%/ in your browser, or start this on another port: dashboard.bat 1201
    pause
    exit /b 1
)

echo.
echo [*] Dashboard on its own: http://127.0.0.1:%PORT%/
echo [*] Other devices on the network: the LAN address is shown in the dashboard. Sign in with website.password.
echo [*] Stop it with Ctrl+C and then Y, or close this window. The bot is not stopped by this window.
echo.
"%PY_CMD%" %PY_ARG% -m website.server --port %PORT% --refresh-interval %REFRESH%

echo.
echo [*] The dashboard has stopped.
echo     If it could not start, port %PORT% may be in use. Try another port: dashboard.bat 1201
pause
endlocal
