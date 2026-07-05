@echo off
setlocal
title Tellinghouse Media - AutoCut
cd /d "%~dp0"

echo ============================================
echo   Tellinghouse Media - AutoCut
echo ============================================
echo.

rem ---- Find a real Python (the Microsoft Store stub fails this check) ----
set "PY="
py -3 --version >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    python --version >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY goto :no_python

rem ---- Make sure ffmpeg is available ----
where ffmpeg >nul 2>nul
if not errorlevel 1 goto :have_ffmpeg
set "PATH=%PATH%;%LOCALAPPDATA%\Microsoft\WinGet\Links"
where ffmpeg >nul 2>nul
if not errorlevel 1 goto :have_ffmpeg
echo ffmpeg was not found. Trying to install it automatically via winget...
winget install --id=Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements
set "PATH=%PATH%;%LOCALAPPDATA%\Microsoft\WinGet\Links"
where ffmpeg >nul 2>nul
if not errorlevel 1 goto :have_ffmpeg
echo.
echo ffmpeg was installed but isn't visible in this window yet.
echo Close this window and double-click RUN_ME.bat again.
echo If it still fails, see README.md for the manual installation steps.
pause
exit /b 1

:have_ffmpeg
rem ---- Local Python environment (kept out of OneDrive-synced folders) ----
set "VENVDIR=%LOCALAPPDATA%\TellinghouseAutoCut\venv"
if exist ".venv\Scripts\python.exe" set "VENVDIR=.venv"
if exist "%VENVDIR%\Scripts\python.exe" goto :have_venv
echo Setting up (first run only, takes a few minutes)...
%PY% -m venv "%VENVDIR%"
if errorlevel 1 goto :venv_failed

:have_venv
call "%VENVDIR%\Scripts\activate.bat"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

echo.
echo Starting Tellinghouse Media AutoCut... your browser will open in a moment.
echo Keep this window open while you use it. Close it when you're done.
echo.
python tellinghouse_autocut.py
pause
exit /b 0

:no_python
echo Python was not found on your computer.
echo.
echo Install it from https://www.python.org/downloads/
echo IMPORTANT: during setup, check the box "Add Python to PATH".
echo Then run this file again.
echo.
echo (Note: if typing "python" pops up the Microsoft Store, that's a
echo Windows shortcut, not real Python -- use the python.org installer.)
pause
exit /b 1

:venv_failed
echo Could not set up the local Python environment.
echo Try reinstalling Python from https://www.python.org/downloads/
echo with "Add Python to PATH" checked, then run this file again.
pause
exit /b 1
