# Building the AutoCut desktop app (EXE + installer)

This turns AutoCut into normal Windows software: an `AutoCut.exe` you double-click
(no console window, ffmpeg included, browser opens by itself) and an
`AutoCut-Setup.exe` installer with a Start-menu entry, desktop icon, and
uninstaller.

## Steps

1. Install **Python 3.9+** from python.org (check "Add Python to PATH") if you
   haven't already.
2. Double-click **Build Installer.bat** and wait. The first run downloads build
   tools, ffmpeg, and the free Inno Setup (used to make the installer), so give
   it 10-20 minutes. Everything else is automatic.

   *(If the automatic Inno Setup install is ever blocked on your machine, grab
   it manually from https://jrsoftware.org/isinfo.php and run the file again --
   you still get `AutoCut.exe` either way, just not the Setup file.)*

Results:

- `dist\AutoCut\AutoCut.exe` -- the app itself (the whole `dist\AutoCut` folder
  is portable; copy it anywhere).
- `installer_out\AutoCut-Setup.exe` -- the installer to share or keep.

## What's different in the installed app

- **Output location:** finished videos, transcripts, clips, and projects go to
  `Videos\AutoCut` in your user folder (Program Files isn't writable). Running
  from source still uses the `output` folder next to the code.
- **No console window:** anything the app would have printed goes to
  `%LOCALAPPDATA%\TellinghouseAutoCut\autocut_log.txt`. Quit the app from the
  browser tab simply by closing it and, if you want the server stopped, end
  AutoCut from the system tray area of Task Manager (it uses no CPU when idle).
- **ffmpeg is bundled**, so nothing needs to be on PATH.
- The Whisper speech model still downloads on the first transcription
  (one-time, ~150 MB), to your user profile's cache folder.

## Notes

- **SmartScreen:** the EXE/installer are unsigned, so the first launch on a new
  PC shows "Windows protected your PC" -- click "More info" > "Run anyway".
  Removing that warning requires a paid code-signing certificate.
- **Size:** expect roughly 150-300 MB installed (Python runtime + Whisper
  engine + ffmpeg).
- Rebuilding after code changes: just run `Build Installer.bat` again (it reuses the
  build environment and ffmpeg, so later builds are much faster).
