@echo off
setlocal EnableExtensions
title OwO-Dusk Setup

rem One-click setup. Double-click this file. It works from any drive letter or folder name.
cd /d "%~dp0"
if errorlevel 1 (
    echo [x] Cannot enter the project folder.
    pause
    exit /b 1
)

rem Find Python: the py launcher first, then python on PATH, then the per-user install folder. start.bat uses the same steps.
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
    echo.
    echo [x] Python was not found on this PC.
    echo     Install Python 3.12 or newer from https://www.python.org/downloads/
    echo     and tick "Add Python to PATH", then run setup.bat again.
    echo.
    pause
    exit /b 1
)

"%PY_CMD%" %PY_ARG% "%~dp0setup.py" %*
set "SETUP_RC=%errorlevel%"
endlocal & exit /b %SETUP_RC%
