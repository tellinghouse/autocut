@echo off
setlocal
title Tellinghouse AutoCut - build the desktop app
cd /d "%~dp0"

echo ============================================
echo   Building the AutoCut desktop app
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
if not defined PY (
    echo Python was not found. Install it from https://www.python.org/downloads/
    echo with "Add Python to PATH" checked, then run this file again.
    pause
    exit /b 1
)

rem ---- Build environment (separate from the app's own venv) ----
set "BVENV=%LOCALAPPDATA%\TellinghouseAutoCut\build_venv"
if not exist "%BVENV%\Scripts\python.exe" (
    echo Creating the build environment - first run only...
    %PY% -m venv "%BVENV%"
    if errorlevel 1 goto :fail
)
call "%BVENV%\Scripts\activate.bat"
echo Installing build tools - takes a few minutes the first time...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt pyinstaller
if errorlevel 1 goto :fail

rem ---- ffmpeg binaries to bundle into the app ----
if exist "ffmpeg_bundle\ffmpeg.exe" if exist "ffmpeg_bundle\ffprobe.exe" goto :have_ff
mkdir ffmpeg_bundle 2>nul
echo Looking for ffmpeg on this computer...
for /f "delims=" %%i in ('where ffmpeg 2^>nul') do if not exist "ffmpeg_bundle\ffmpeg.exe" copy /y "%%i" "ffmpeg_bundle\ffmpeg.exe" >nul
for /f "delims=" %%i in ('where ffprobe 2^>nul') do if not exist "ffmpeg_bundle\ffprobe.exe" copy /y "%%i" "ffmpeg_bundle\ffprobe.exe" >nul
if exist "ffmpeg_bundle\ffmpeg.exe" if exist "ffmpeg_bundle\ffprobe.exe" goto :have_ff

echo Downloading ffmpeg - one time, about 90 MB...
powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile 'ffmpeg_dl.zip'"
if errorlevel 1 goto :ff_fail
powershell -NoProfile -Command "Expand-Archive -Force 'ffmpeg_dl.zip' 'ffmpeg_dl'"
if errorlevel 1 goto :ff_fail
for /r "ffmpeg_dl" %%i in (ffmpeg.exe) do if exist "%%i" copy /y "%%i" "ffmpeg_bundle\ffmpeg.exe" >nul
for /r "ffmpeg_dl" %%i in (ffprobe.exe) do if exist "%%i" copy /y "%%i" "ffmpeg_bundle\ffprobe.exe" >nul
rmdir /s /q ffmpeg_dl 2>nul
del ffmpeg_dl.zip 2>nul
if not exist "ffmpeg_bundle\ffmpeg.exe" goto :ff_fail
if not exist "ffmpeg_bundle\ffprobe.exe" goto :ff_fail

:have_ff
echo ffmpeg ready.
echo.

rem ---- Build AutoCut.exe ----
echo Building AutoCut.exe - this takes several minutes...
pyinstaller --noconfirm AutoCut.spec
if errorlevel 1 goto :fail
echo.
echo App built: dist\AutoCut\AutoCut.exe

rem ---- Build the installer (needs the free Inno Setup 6) ----
call :find_iscc
if not exist "%ISCC%" (
    echo Inno Setup 6 not found - installing it automatically via winget...
    winget install --id JRSoftware.InnoSetup -e --accept-package-agreements --accept-source-agreements
    call :find_iscc
)
if exist "%ISCC%" (
    echo Building the installer...
    "%ISCC%" /Qp installer.iss
    if errorlevel 1 goto :fail
    echo.
    echo ============================================
    echo   Done!
    echo   Installer: installer_out\AutoCut-Setup.exe
    echo.
    echo   Share AutoCut-Setup.exe with anyone - they double-click it
    echo   to install AutoCut like any normal program.
    echo ============================================
) else (
    echo.
    echo ============================================
    echo   App is built: dist\AutoCut\AutoCut.exe
    echo.
    echo   Couldn't set up Inno Setup automatically, so the
    echo   AutoCut-Setup.exe installer wasn't made this time.
    echo   Install the free Inno Setup 6 from
    echo   https://jrsoftware.org/isinfo.php and run this file
    echo   again to produce the installer.
    echo ============================================
)
pause
exit /b 0

rem ---- Locate the Inno Setup compiler (checks both Program Files paths) ----
:find_iscc
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
goto :eof

:ff_fail
echo.
echo Could not get ffmpeg automatically. Download the "release essentials"
echo build from https://www.gyan.dev/ffmpeg/builds/ and copy ffmpeg.exe and
echo ffprobe.exe from its bin folder into this folder's ffmpeg_bundle folder,
echo then run this file again.
pause
exit /b 1

:fail
echo.
echo Build failed -- see the messages above.
pause
exit /b 1
