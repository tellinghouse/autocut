# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the Tellinghouse AutoCut desktop app.

Don't run this by hand -- double-click "Build Installer.bat", which sets everything up
and then runs:  pyinstaller --noconfirm AutoCut.spec

Result: dist\AutoCut\AutoCut.exe (plus its _internal folder).
"""

import os

from PyInstaller.utils.hooks import collect_all

datas = [("web", "web")]          # the browser UI
binaries = []
hiddenimports = []

# faster-whisper and its native dependencies keep data files and DLLs in their
# packages; collect_all makes sure PyInstaller ships every piece. Anything not
# installed is simply skipped (the app then runs without transcription).
for pkg in ("faster_whisper", "ctranslate2", "tokenizers", "huggingface_hub",
            "onnxruntime", "av", "numpy"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        print(f"note: {pkg} not collected (not installed?)")

# ffmpeg/ffprobe are placed in ffmpeg_bundle\ by "Build Installer.bat" and ship inside
# the app, so installed users never have to set up ffmpeg themselves.
if os.path.isfile(os.path.join("ffmpeg_bundle", "ffmpeg.exe")):
    datas += [("ffmpeg_bundle", "ffmpeg")]
else:
    print("note: ffmpeg_bundle\\ffmpeg.exe not found -- the app will need ffmpeg on PATH")

a = Analysis(
    ["tellinghouse_autocut.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="AutoCut",
    console=False,  # no console window; output goes to %LOCALAPPDATA%\TellinghouseAutoCut\autocut_log.txt
    icon="autocut.ico" if os.path.isfile("autocut.ico") else None,
    version="version_info.txt" if os.path.isfile("version_info.txt") else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="AutoCut",
)
