@echo off
setlocal EnableExtensions
title OwO-Dusk

rem Starts the bot (uwu.py). Double-click this file, or run it from a terminal with the same options as uwu.py.
rem It works from any drive letter or folder name. Run setup.bat first on a new PC.
cd /d "%~dp0"
if errorlevel 1 (
    echo [x] Cannot enter the project folder.
    pause
    exit /b 1
)

rem Find Python exactly like setup.bat does: the py launcher first, then python on PATH, then the per-user install.
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
    echo [x] Khong tim thay Python tren may nay.
    echo     Hay chay setup.bat truoc de cai Python 3.12 tro len.
    echo.
    pause
    exit /b 1
)

"%PY_CMD%" %PY_ARG% "%~dp0uwu.py" %*
set "START_RC=%errorlevel%"
echo.
echo [*] Bot da dung. Dong cua so nay neu khong can nua.
pause
endlocal & exit /b %START_RC%
