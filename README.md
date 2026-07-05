# Tellinghouse Media -- AutoCut

A video auto-editor built for Tellinghouse Media. Two ways to use it:

**Podcast (multi-camera).** Drop in your camera angles, and AutoCut:

- **Syncs everything automatically.** If each camera/mic started recording at a
  slightly different moment, AutoCut lines them all up to the same real timeline
  by matching the audio (like a clap, or just the room sound every mic picks up).
- **Cuts to whoever's talking.** It follows the conversation and switches the
  video to whichever camera's mic is currently active, with a "minimum shot
  length" so it doesn't flicker between cameras.
- **Handles mismatched lengths.** Trim to the shortest overlap, or keep every
  frame and let the edit use whichever cameras are rolling at each moment.
- **Levels the audio.** The mixed audio track is volume-normalized to standard
  podcast loudness.

**Talking head (one camera).** Drop in a single video (plus a separate mic
recording if you have one), and AutoCut tightens the pauses into jump cuts,
levels the audio, and makes captions and clips.

**Extras (both modes):**

- **Transcript, captions & YouTube chapters** -- speech-to-text runs on this
  computer (nothing is uploaded). You get a timestamped `.txt` transcript, an
  `.srt` caption file, and a paste-ready YouTube chapter list.
- **Social clips** -- finds the highest-energy moments of the episode and cuts
  them as vertical (9:16) shorts for TikTok/Reels/Shorts, plus a widescreen
  copy of each.
- **Tighten long pauses** -- cuts dead air automatically (great for solo
  videos; use with care on relaxed two-person chats).
- **Color correction** -- per-camera exposure, contrast, saturation, and warmth
  sliders with live before/after thumbnails, plus support for `.cube` LUT files
  (from your camera maker or a purchased look pack). Corrections are baked into
  the rendered video; the FCPXML project stays untouched so you can still grade
  properly in your editor.
- **Edit by transcript** -- after a render with a transcript, click "Edit by
  transcript" on the Done screen. Every line shows its time and **who said it**
  (worked out from whose mic was loudest). Search for a word ("roach"), click a
  line's time or speaker to strike it out (shift-click for a whole range), and
  apply -- the video is re-cut without those sections and the transcript,
  captions, and chapters are regenerated to match. You can also fix typos in
  the text and rename speakers ("Host", "Guest") before applying. Repeatable --
  every earlier cut stays in the output folder.
- **Framing** -- per-camera zoom and reposition with live previews. Punch in on
  a 4K camera with no quality loss in a 1080p video.
- **YouTube export quality** -- a render setting tuned to YouTube's recommended
  upload specs (better compression, 2-second keyframes, correct color tags,
  higher audio bitrate). Standard quality renders much faster for checking cuts.
- **Intro, outro & animated titles** -- on the Done screen, "Add intro & titles"
  attaches your premade intro/outro (auto-resized) and burns in animated text:
  lower-third name cards (with one click per speaker, placed when each person
  first talks), or centered banners like "Subscribe" and sponsor thank-yous.
  Transcript, captions, and chapters shift automatically to match.
- **Projects** -- every finished cut auto-saves as a project. "Resume a previous
  session" on the first screen lists them all; open one to keep editing by
  transcript, add branding, or re-download files. As many projects as you have
  episodes.
- **Batch mode** -- point AutoCut at a folder of recording sessions and let it
  churn through every episode overnight. Each subfolder = one episode.

It runs entirely on your computer -- nothing is uploaded anywhere.

## One-time setup

1. Install **Python** (3.9 or newer) if you don't have it: https://www.python.org/downloads/
   When installing, check the box that says **"Add Python to PATH."**
   (If typing `python` in a terminal pops up the Microsoft Store, that's a
   Windows shortcut, not real Python -- use the python.org installer.)
2. Double-click **RUN_ME.bat**. The first run will:
   - Try to install **ffmpeg** automatically (via `winget`, built into Windows 10/11).
   - Set up a small local environment and install the required packages
     (`numpy` and the `faster-whisper` speech engine). This lives in your local
     app-data folder, not in this folder.
   - Open AutoCut in your browser.

If the automatic ffmpeg install doesn't work, install it manually:
   - Go to https://www.gyan.dev/ffmpeg/builds/ and download the "release essentials" build.
   - Unzip it, then add its `bin` folder to your Windows PATH (search "Edit the
     system environment variables" in the Start menu -> Environment Variables ->
     edit `Path` -> add the folder).
   - Close and reopen RUN_ME.bat.

After the first run, starting AutoCut again is just: double-click **RUN_ME.bat**.

**Prefer a normal installed app?** You can build a regular `AutoCut.exe` plus an
`AutoCut-Setup.exe` installer (Start-menu entry, desktop icon, ffmpeg included,
no console window) -- see **BUILDING.md**. Once installed, finished videos land
in `Videos\AutoCut` instead of the `output` folder.

**Note on transcription:** the first time you make a transcript, AutoCut
downloads the speech model (one-time, needs internet, ~150 MB). After that,
transcription is fully offline.

## Using it

1. **Pick what you're making** -- multi-camera podcast, or single-camera
   talking head.
2. **Add your footage.** Drag in your video file(s). If you recorded separate
   mic/recorder audio (a Zoom recorder, lav mics, etc.), drop those in too --
   AutoCut will offer to pair each one to the right camera. Two files with the
   same name (common when cameras are the same model) are kept separate
   automatically.
3. **Click "Analyze footage"** and watch the progress bar.
4. **Review**: check the sync table (every track can be nudged by hand), name
   your episode, and pick your outputs and extras.
5. **Click "Make my video."** Progress is shown step by step -- cutting,
   tightening, transcribing, clipping.
6. **Download everything** from the last screen. It's all also saved in the
   `output` folder next to AutoCut, named like `Episode 12_2026-07-04_143210.mp4`.

Refreshing the browser is always safe -- AutoCut picks up right where it was,
even mid-render.

### Batch mode (overnight)

On the first screen, open **"Batch a whole folder"**. Organize your recordings
like this:

```
D:\Recordings\July sessions\
    Ep 14 - Jane Doe\      <- one episode
        camA.mp4  camB.mp4  zoom.wav
    Ep 15 - John Smith\    <- another episode
        camA.mp4  camB.mp4
```

Type the path (e.g. `D:\Recordings\July sessions`), click **Scan** to see what
AutoCut found, pick your extras, and click **Run batch**. Files are read in
place (no uploading). Episodes are named after their folders. If one episode
fails, the rest keep going -- you get a per-episode report at the end.

### If the sync looks off

Every track's row shows a confidence badge (High / Medium / Low) for its
detected sync offset. If a track shows Low confidence, or the preview looks off,
you can type a corrected number of seconds directly into that track's "Sync" box
before clicking "Make my video" (increase it to delay that track, decrease to
move it earlier). This works for separate audio recorders too, not just cameras.

Tips for reliable auto-sync:

- Start all cameras/recorders within about **4 minutes** of each other. (Beyond
  that, auto-detection may fail -- you can still type the offset by hand.)
- A single loud clap near the start, picked up by every mic, makes sync
  detection near-perfect.
- A camera with **no audio track at all** can't be auto-synced. It still works --
  AutoCut marks it "No audio" and you type its sync offset yourself.

### About the editable project file (FCPXML)

The project-file export uses the standard FCPXML format that Premiere Pro,
DaVinci Resolve, and Final Cut can all import. The auto-cut sequence is on the
main track exactly as in the rendered video; every other camera's full synced
footage is placed on tracks above it, and each separate mic/recorder file on an
audio track below it -- so nothing is thrown away and you can swap angles or
grab clean audio by hand. This file format hasn't been test-imported into real
editing software from where AutoCut was built, so double-check it opens cleanly
and nudge anything that looks off.

## Advanced settings (podcast mode)

- **Minimum shot length** -- how long a camera must stay on screen before the
  edit is allowed to cut again. Lower = snappier editing, higher = calmer.
- **Switch sensitivity** -- how much louder someone needs to be before the edit
  cuts to them. Higher = fewer, more confident cuts.

## Troubleshooting

- **"ffmpeg/ffprobe was not found"** -- see the setup section above.
- **"Python was not found"** -- install from python.org with "Add Python to
  PATH" checked. The Microsoft Store `python` shortcut is not enough.
- **Transcript option is grayed out** -- close AutoCut and run RUN_ME.bat again
  so it can install the speech engine.
- **Nothing opens in the browser** -- go to the address printed in the RUN_ME
  window (usually `http://127.0.0.1:8765/`) manually.
- **"Could not find a free port"** -- close other copies of AutoCut you have
  running, then try again.
- **Want a clean slate?** Click "Start over" in the app. Old renders can be
  deleted from the `output` folder next to AutoCut.

## What's inside this folder

- `RUN_ME.bat` -- double-click this to start AutoCut (hands off to `Start_AutoCut.bat`).
- `tellinghouse_autocut.py` -- the local web server.
- `autocut/` -- the syncing, editing, transcription, and rendering engine.
- `web/` -- the browser interface.
- `output/` -- your finished videos, transcripts, and clips land here.
- `Build Installer.bat`, `AutoCut.spec`, `installer.iss`, `version_info.txt` --
  build the installable desktop app (see BUILDING.md).

---
*Tellinghouse Media*
