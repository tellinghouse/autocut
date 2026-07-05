#!/usr/bin/env python3
"""Tellinghouse Media -- AutoCut local server: a tiny web app for auto-syncing
and auto-cutting multicam podcast footage (or tightening up single-camera
talking-head videos). Run this file and it opens in your browser.
"""

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, unquote

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from autocut import media_utils, sync, editor, fcpxml_export, postproc, transcribe  # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(__file__))
FROZEN = bool(getattr(sys, "frozen", False))  # True when running as the packaged .exe

if FROZEN:
    # Packaged desktop app (PyInstaller): bundled resources live in _MEIPASS,
    # finished videos go to the user's Videos folder (Program Files isn't
    # writable), and console output goes to a log file (there is no console).
    RESOURCE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    APPDATA_DIR = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "TellinghouseAutoCut")
    OUTPUT_DIR = os.path.join(os.path.expanduser("~"), "Videos", "AutoCut")
    os.makedirs(APPDATA_DIR, exist_ok=True)
    _log_file = open(os.path.join(APPDATA_DIR, "autocut_log.txt"), "a",
                     buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = sys.stderr = _log_file
    # Use the ffmpeg/ffprobe that ship inside the app, if present.
    _ffmpeg = os.path.join(RESOURCE_DIR, "ffmpeg", "ffmpeg.exe")
    _ffprobe = os.path.join(RESOURCE_DIR, "ffmpeg", "ffprobe.exe")
    if os.path.isfile(_ffmpeg):
        os.environ.setdefault("AUTOCUT_FFMPEG", _ffmpeg)
    if os.path.isfile(_ffprobe):
        os.environ.setdefault("AUTOCUT_FFPROBE", _ffprobe)
else:
    RESOURCE_DIR = APP_DIR
    OUTPUT_DIR = os.path.join(APP_DIR, "output")

WEB_DIR = os.path.join(RESOURCE_DIR, "web")
RUN_DIR = os.path.join(tempfile.gettempdir(), "autocut_run")
UPLOAD_DIR = os.path.join(RUN_DIR, "uploads")
WORK_DIR = os.path.join(RUN_DIR, "work")
LUT_DIR = os.path.join(RUN_DIR, "luts")
BUMPER_DIR = os.path.join(RUN_DIR, "bumpers")

for d in (UPLOAD_DIR, WORK_DIR, LUT_DIR, BUMPER_DIR, OUTPUT_DIR):
    os.makedirs(d, exist_ok=True)

LOCK = threading.Lock()
UPLOAD_NAME_LOCK = threading.Lock()  # serializes picking + claiming upload filenames
TRANSCRIBE_OK = transcribe.is_available()

MEDIA_VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".mts", ".mxf"}
MEDIA_AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
MEDIA_EXT = MEDIA_VIDEO_EXT | MEDIA_AUDIO_EXT


def fresh_job():
    return {
        "stage": "idle",       # idle -> analyzing -> ready -> processing -> done / error
        "progress": 0.0,
        "message": "",
        "error": None,
        "files": {},           # filename -> probe info
        "offsets": {},         # filename -> offset seconds
        "confidence": {},      # filename -> 0..1
        "reference": None,
        "video_files": [],
        "audio_only_files": [],
        "trim_duration": None,     # None means the tracks don't all overlap
        "keep_full_duration": None,
        "src_paths": {},           # filename -> absolute path
        "results": [],             # [{id, label, path, kind}]
        "batch": None,             # {"index", "total", "episode"} while a batch runs
        "batch_summary": None,     # [{"episode", "ok", "error"}] when a batch finishes
        "transcript_segments": None,   # [{start, end, text, speaker}] of the current video
        "transcript_speakers": None,   # {track_name: display_label}
        "edit_count": 0,               # how many transcript edits have been applied
        "export_quality": "standard",  # encode profile for re-cuts and branding
        "content_start": 0.0,          # where the episode content begins (after an intro)
        "content_end": None,           # where it ends (before an outro); None = video end
    }


JOB = fresh_job()


def set_job(**kwargs):
    with LOCK:
        JOB.update(kwargs)


def safe_episode_name(raw):
    """Turn a user-typed episode name into a safe filename fragment."""
    raw = (raw or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9 _\-]", "", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:60] or "AutoCut"


def unique_upload_name(name):
    """If `name` already exists in the upload folder, pick 'name (2).ext' etc.
    (Cameras of the same model all produce identical filenames.)"""
    base, ext = os.path.splitext(name)
    candidate = name
    i = 2
    while os.path.exists(os.path.join(UPLOAD_DIR, candidate)):
        candidate = f"{base} ({i}){ext}"
        i += 1
    return candidate


def reset_work():
    if os.path.isdir(WORK_DIR):
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    os.makedirs(WORK_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Color correction
# ---------------------------------------------------------------------------

def build_color_vf(cfg):
    """Turn one camera's color settings into an ffmpeg filter string (or None).

    cfg: {"exposure": EV -1.5..1.5, "contrast": 0.7..1.3, "saturation": 0..2,
          "warmth": -1..1, "lut": filename-in-LUT_DIR or None}
    """
    if not cfg:
        return None
    parts = []
    try:
        ev = float(cfg.get("exposure", 0) or 0)
        con = float(cfg.get("contrast", 1) or 1)
        sat = float(cfg.get("saturation", 1) or 1)
        warm = float(cfg.get("warmth", 0) or 0)
    except (TypeError, ValueError):
        ev, con, sat, warm = 0.0, 1.0, 1.0, 0.0
    ev = max(-3.0, min(3.0, ev))
    con = max(0.5, min(1.6, con))
    sat = max(0.0, min(2.5, sat))
    warm = max(-1.0, min(1.0, warm))

    lut = cfg.get("lut")
    if lut:
        lut_name = os.path.basename(str(lut))
        lut_path = os.path.join(LUT_DIR, lut_name)
        if os.path.isfile(lut_path):
            # ffmpeg filter args need : and \ escaped; use forward slashes.
            esc = lut_path.replace("\\", "/").replace(":", "\\:").replace("'", "")
            parts.append(f"lut3d='{esc}'")
    if abs(ev) > 0.01:
        parts.append(f"exposure=exposure={ev:.2f}")
    if abs(con - 1.0) > 0.005 or abs(sat - 1.0) > 0.005:
        parts.append(f"eq=contrast={con:.3f}:saturation={sat:.3f}")
    if abs(warm) > 0.01:
        # 6500K is neutral; lower = warmer (orange), higher = cooler (blue).
        temp = int(round(6500 - warm * 2300))
        parts.append(f"colortemperature=temperature={temp}")
    return ",".join(parts) if parts else None


def color_vf_by_camera(color_settings, video_files):
    out = {}
    for cam in video_files:
        vf = build_color_vf((color_settings or {}).get(cam))
        if vf:
            out[cam] = vf
    return out


# ---------------------------------------------------------------------------
# Speaker attribution: which mic is loudest while each line is spoken
# ---------------------------------------------------------------------------

def build_framing_vf(cfg):
    """Per-camera zoom/pan -> a crop filter applied at full source resolution.
    cfg: {"zoom": 1..4, "panx": -1..1, "pany": -1..1} (pan only matters when zoomed)."""
    if not cfg:
        return None
    try:
        z = float(cfg.get("zoom", 1) or 1)
        px = float(cfg.get("panx", 0) or 0)
        py = float(cfg.get("pany", 0) or 0)
    except (TypeError, ValueError):
        return None
    z = max(1.0, min(4.0, z))
    if z < 1.005:
        return None
    px = max(-1.0, min(1.0, px))
    py = max(-1.0, min(1.0, py))
    fx = (1.0 + px) / 2.0   # 0 = far left, 1 = far right
    fy = (1.0 + py) / 2.0
    return (f"crop=iw/{z:.3f}:ih/{z:.3f}:"
            f"(iw-iw/{z:.3f})*{fx:.3f}:(ih-ih/{z:.3f})*{fy:.3f}")


def framing_vf_by_camera(framing_settings, video_files):
    out = {}
    for cam in video_files:
        vf = build_framing_vf((framing_settings or {}).get(cam))
        if vf:
            out[cam] = vf
    return out


# ---------------------------------------------------------------------------
# Branding: intro/outro bumpers and animated text overlays (drawtext)
# ---------------------------------------------------------------------------

def _font_path(bold=False):
    candidates = [
        r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def _dt_escape_text(s):
    """Prepare user text for a single-quoted drawtext 'text=' argument.
    Inside quotes, commas and colons are safe; apostrophes use the '\\'' idiom."""
    s = (s or "").replace("\\", "").replace("%", "")
    return s.replace("'", "'\\''")


def _dt_escape_path(p):
    return p.replace("\\", "/").replace(":", "\\:").replace("'", "")


def _fade_alpha(st, et, fade=0.4):
    # plain commas: the whole expression is wrapped in single quotes downstream
    return (f"if(lt(t-{st:.2f},{fade}),(t-{st:.2f})/{fade},"
            f"if(gt(t,{et:.2f}-{fade}),max(0,({et:.2f}-t)/{fade}),1))")


def build_overlay_filters(overlays, h):
    """Turn overlay specs into a list of drawtext filters.

    overlays: [{"style": "lower_third"|"banner", "text", "subtext",
                "start": sec, "dur": sec}]
    """
    font = _font_path(bold=True)
    font_sub = _font_path(bold=False)
    if not font:
        raise ValueError("No usable font found for text overlays.")
    filters = []
    for ov in overlays:
        text = str(ov.get("text", "")).strip()
        if not text:
            continue
        subtext = str(ov.get("subtext", "") or "").strip()
        try:
            st = max(0.0, float(ov.get("start", 0)))
            dur = max(0.8, float(ov.get("dur", 4)))
        except (TypeError, ValueError):
            continue
        et = st + dur
        style = ov.get("style", "lower_third")
        alpha = _fade_alpha(st, et)
        enable = f"between(t,{st:.2f},{et:.2f})"
        fs_main = max(18, int(h * 0.055))
        fs_sub = max(14, int(h * 0.034))
        box = f"box=1:boxcolor=black@0.55:boxborderw={max(8, int(h * 0.016))}"
        common = (f"fontfile='{_dt_escape_path(font)}':fontcolor=white:"
                  f"{box}:alpha='{alpha}':enable='{enable}'")
        if style == "banner":
            filters.append(
                f"drawtext=text='{_dt_escape_text(text)}':fontsize={fs_main}:"
                f"x=(w-text_w)/2:y=h*0.84:{common}")
        else:  # lower third
            filters.append(
                f"drawtext=text='{_dt_escape_text(text)}':fontsize={fs_main}:"
                f"x=w*0.055:y=h*0.76:{common}")
            if subtext:
                common_sub = (f"fontfile='{_dt_escape_path(font_sub or font)}':fontcolor=white@0.92:"
                              f"{box}:alpha='{alpha}':enable='{enable}'")
                filters.append(
                    f"drawtext=text='{_dt_escape_text(subtext)}':fontsize={fs_sub}:"
                    f"x=w*0.055:y=h*0.76+{int(fs_main * 1.45)}:{common_sub}")
    return filters


def _prep_bumper(name, w, h, fps, out_path):
    """Re-encode an intro/outro to the episode's exact spec (adding silent audio
    if it has none) so it can be losslessly concatenated. Returns its duration."""
    src = os.path.join(BUMPER_DIR, os.path.basename(name))
    if not os.path.isfile(src):
        raise ValueError(f"Intro/outro file not found: {name}")
    info = media_utils.probe(src)
    if not info["has_video"]:
        raise ValueError(f"{name} doesn't contain video.")
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps}")
    cmd = [media_utils.ffmpeg_path(), "-y", "-nostdin", "-i", src]
    if not info["has_audio"]:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest"]
    cmd += ["-vf", vf] + editor.video_enc_args(fps) + editor.audio_enc_args() + [out_path]
    media_utils.run(cmd)
    return media_utils.probe(out_path)["duration"]


def list_speaker_mics(analysis, audio_pairing):
    """The distinct audio tracks that each represent a person: every camera's
    chosen mic (paired recorder or its own audio) plus unpaired audio files."""
    files = analysis["files"]
    mics = []
    paired_targets = []
    for n in analysis["video_files"]:
        m = audio_pairing.get(n) or n
        if not files.get(m, {}).get("has_audio"):
            m = n
        paired_targets.append(m)
        if files.get(m, {}).get("has_audio") and m not in mics:
            mics.append(m)
    for n in analysis["audio_only_files"]:
        if n not in paired_targets and files.get(n, {}).get("has_audio") and n not in mics:
            mics.append(n)
    return mics


def assign_speakers(segments, mics, offsets, program_start, tighten_spans=None):
    """Give every transcript segment a 'speaker' (a mic track name).

    Segment times are in the FINAL video's clock; mic energy curves live on the
    program clock. tighten_spans (if pause-tightening ran) maps between them.
    """
    if not segments:
        return segments
    if len(mics) <= 1:
        only = mics[0] if mics else None
        for seg in segments:
            seg["speaker"] = only
        return segments

    from autocut.active_speaker import frame_rms, smooth as smooth_curve
    curves = {}
    for m in mics:
        wav_path = os.path.join(WORK_DIR, f"{m}.analysis.wav")
        if not os.path.isfile(wav_path):
            continue
        audio, sr = media_utils.read_wav_mono(wav_path)
        times, rms = frame_rms(audio, sr, frame_sec=0.2, hop_sec=0.05)
        # place on the final (pre-tighten) clock: file time + sync offset - program start
        curves[m] = (times + offsets.get(m, 0.0) - program_start, smooth_curve(rms, taps=5))
    if not curves:
        for seg in segments:
            seg["speaker"] = mics[0]
        return segments

    prev = None
    for seg in segments:
        pts_final = np.linspace(seg["start"], seg["end"], 9)
        if tighten_spans:
            pts = np.array([postproc.time_before_cuts(tighten_spans, float(t)) for t in pts_final])
        else:
            pts = pts_final
        scores = {}
        for m, (t_curve, r_curve) in curves.items():
            vals = np.interp(pts, t_curve, r_curve, left=0.0, right=0.0)
            scores[m] = float(np.median(vals))
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best, best_v = ranked[0]
        second_v = ranked[1][1] if len(ranked) > 1 else 0.0
        if prev is not None and best != prev and best_v < second_v * 1.15 + 1e-9:
            best = prev  # too close to call: keep the previous speaker (continuity)
        seg["speaker"] = best
        prev = best
    return segments


def default_speaker_labels(mics):
    labels = {}
    for m in mics:
        labels[m] = os.path.splitext(os.path.basename(m))[0]
    return labels


def apply_speaker_labels(segments, labels):
    """Bake display labels onto segments (used by the txt/srt writers)."""
    multi = len({s.get("speaker") for s in segments if s.get("speaker")}) > 1
    for seg in segments:
        sp = seg.get("speaker")
        seg["speaker_label"] = (labels or {}).get(sp, "") if (sp and multi) else \
            ((labels or {}).get(sp) if sp and labels and labels.get(sp) else None)
        if not multi:
            seg["speaker_label"] = None
    return segments


def cut_ranges_from_deleted(segments, deleted_idx, duration, pad_pre=0.05):
    """Deleted transcript lines -> time ranges to remove. Each deleted line
    claims the time from the end of the PREVIOUS line (so the pause/gasp before
    it goes too) until just before the next line starts (so the dead time after
    it goes as well)."""
    n = len(segments)
    ranges = []
    for i in sorted(set(deleted_idx)):
        if i < 0 or i >= n:
            continue
        seg = segments[i]
        if i == 0:
            start = 0.0
        else:
            after_prev = float(segments[i - 1]["end"]) + 0.15
            just_before = max(0.0, float(seg["start"]) - pad_pre)
            start = min(after_prev, just_before) if after_prev < just_before else just_before
        if i + 1 < n:
            end = max(start, float(segments[i + 1]["start"]) - pad_pre)
        else:
            end = duration
        ranges.append((start, min(end, duration)))
    merged = []
    for s, e in sorted(ranges):
        if merged and s <= merged[-1][1] + 0.05:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return [(s, e) for s, e in merged if e - s > 0.01]


# ---------------------------------------------------------------------------
# Analysis (pure core, used by both single-episode and batch flows)
# ---------------------------------------------------------------------------

def analyze_core(named_paths, progress):
    """named_paths: {display_name: absolute_path}. Returns the analysis dict."""
    files = {}
    audio_arrays = {}
    sr_used = 16000
    names = list(named_paths.keys())
    total = max(1, len(names))

    for idx, name in enumerate(names):
        progress(idx / total * 0.8, f"Reading {name} ({idx + 1} of {total})")
        path = named_paths[name]
        if not os.path.isfile(path):
            raise ValueError(f"Missing file: {name}")
        info = media_utils.probe(path)
        if not info["has_video"] and not info["has_audio"]:
            raise ValueError(f"{name} doesn't seem to contain any video or audio.")
        kind = "video" if info["has_video"] else "audio"
        files[name] = {**info, "kind": kind, "name": name}

        if info["has_audio"]:
            wav_path = os.path.join(WORK_DIR, f"{name}.analysis.wav")
            media_utils.extract_audio_wav(path, wav_path, sr=sr_used)
            audio, sr = media_utils.read_wav_mono(wav_path)
            audio_arrays[name] = audio
        else:
            # A camera with no audio track: it can't be auto-synced or drive the
            # cut, but it can still be used (nudge its Sync value by hand).
            audio_arrays[name] = np.zeros(1, dtype=np.float32)

    progress(0.85, "Lining up the sync")
    ref_id, offset_info = sync.compute_offsets(audio_arrays, sr_used, max_offset_sec=600, analysis_sec=300)
    offsets = {k: v[0] for k, v in offset_info.items()}
    confidence = {k: v[1] for k, v in offset_info.items()}

    video_files = [n for n, f in files.items() if f["kind"] == "video"]
    audio_only_files = [n for n, f in files.items() if f["kind"] == "audio"]
    if not video_files:
        raise ValueError("Add at least one video file (a camera angle).")

    durations = {n: files[n]["duration"] for n in video_files}
    full_start, full_end, _, _ = editor.compute_program_range(video_files, offsets, durations, "keep_full")
    try:
        trim_start, trim_end, _, _ = editor.compute_program_range(video_files, offsets, durations, "trim")
        trim_duration = trim_end - trim_start
    except ValueError:
        trim_duration = None  # cameras never all overlap; UI disables "trim"

    return {
        "files": files,
        "offsets": offsets,
        "confidence": confidence,
        "reference": ref_id,
        "video_files": video_files,
        "audio_only_files": audio_only_files,
        "trim_duration": trim_duration,
        "keep_full_duration": full_end - full_start,
        "src_paths": dict(named_paths),
    }


def run_analyze(filenames):
    """Single-episode flow: analyze uploaded files in a background thread."""
    try:
        reset_work()
        named_paths = {n: os.path.join(UPLOAD_DIR, n) for n in filenames}

        def progress(p, msg):
            set_job(progress=p, message=msg)

        analysis = analyze_core(named_paths, progress)
        set_job(stage="ready", progress=0.0, message="", error=None, results=[], **analysis)
    except Exception as e:
        traceback.print_exc()
        set_job(stage="error", error=str(e))


# ---------------------------------------------------------------------------
# Processing (pure core, used by both single-episode and batch flows)
# ---------------------------------------------------------------------------

def process_core(analysis, settings, progress):
    """Render + extras for one episode. Returns a results list:
    [{"id", "label", "path", "kind"}] -- entries with path=None are info notes."""
    files = analysis["files"]
    offsets = dict(analysis["offsets"])
    video_files = list(analysis["video_files"])
    audio_only_files = list(analysis["audio_only_files"])
    src_paths = analysis["src_paths"]

    mode = settings.get("mode", "keep_full")
    audio_mode = settings.get("audio_mode", "mixed")
    min_shot_sec = float(settings.get("min_shot_sec", 2.0))
    switch_margin = float(settings.get("switch_margin", 1.15))
    want_video = bool(settings.get("want_video", True))
    want_fcpxml = bool(settings.get("want_fcpxml", False))
    audio_pairing = settings.get("audio_pairing", {}) or {}
    episode = safe_episode_name(settings.get("episode_name", ""))

    want_transcript = bool(settings.get("want_transcript", False))
    whisper_model = transcribe.MODEL_CHOICES.get(settings.get("whisper_model", "fast"), "base")
    clips_count = int(settings.get("clips_count", 0) or 0)
    clips_len = float(settings.get("clips_len", 45) or 45)
    want_tighten = bool(settings.get("want_tighten", False))
    pause_min_sec = float(settings.get("pause_min_sec", 2.0))

    editor.set_encode_profile(settings.get("export_quality", "standard"))

    offset_overrides = settings.get("offset_overrides", {}) or {}
    for name, val in offset_overrides.items():
        if val is None or val == "":
            continue
        try:
            offsets[name] = float(val)
        except (TypeError, ValueError):
            pass

    camera_paths = {n: src_paths[n] for n in video_files}
    durations = {n: files[n]["duration"] for n in video_files}
    camera_specs = {n: files[n] for n in video_files}

    def has_audio(name):
        return bool(files.get(name, {}).get("has_audio"))

    energy_audio_by_camera = {}
    for n in video_files:
        paired = audio_pairing.get(n) or n
        wav_path = os.path.join(WORK_DIR, f"{paired}.analysis.wav")
        if not os.path.isfile(wav_path):
            wav_path = os.path.join(WORK_DIR, f"{n}.analysis.wav")
            paired = n
        if os.path.isfile(wav_path):
            audio, sr = media_utils.read_wav_mono(wav_path)
        else:
            audio, sr = np.zeros(1, dtype=np.float32), 16000  # camera with no audio
        energy_audio_by_camera[n] = (audio, sr, paired)

    progress(0.02, "Building the edit")
    edl = editor.build_edl(
        video_files, offsets, durations, energy_audio_by_camera,
        mode=mode, min_shot_sec=min_shot_sec, switch_margin=switch_margin,
    )

    target_w, target_h, target_fps = editor.pick_target_spec(list(camera_specs.values()))

    results = []
    render_dir = os.path.join(WORK_DIR, "render")
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    base = os.path.join(OUTPUT_DIR, f"{episode}_{stamp}")

    have_extras = want_transcript or clips_count > 0 or want_tighten
    render_end = 0.5 if have_extras else 0.9

    out_path = None
    if want_video:
        mixed_sources = None
        if audio_mode == "mixed":
            mixed_sources = []
            seen_paths = set()
            paired_targets = []
            for n in video_files:
                paired = audio_pairing.get(n) or n
                paired_targets.append(paired)
                if not has_audio(paired):
                    continue
                p = src_paths.get(paired)
                if not p or p in seen_paths:
                    continue  # same recorder paired to two cameras: mix it once
                seen_paths.add(p)
                off = offsets.get(paired, offsets.get(n, 0.0))
                mixed_sources.append((p, off))
            for n in audio_only_files:
                p = src_paths.get(n)
                if n not in paired_targets and p and p not in seen_paths and has_audio(n):
                    seen_paths.add(p)
                    mixed_sources.append((p, offsets.get(n, 0.0)))
            if not mixed_sources:
                raise ValueError(
                    "None of these files has usable audio, so a mixed audio track "
                    "can't be built. Try the 'Switch audio with the camera' option."
                )

        out_path = base + ".mp4"
        editor.render_program(
            camera_paths, edl, audio_mode, render_dir, out_path,
            mixed_audio_sources=mixed_sources,
            target_w=target_w, target_h=target_h, target_fps=target_fps,
            progress_cb=lambda p, msg: progress(0.05 + p * (render_end - 0.05), msg),
            vf_extra_by_camera=color_vf_by_camera(settings.get("color"), video_files),
            vf_pre_by_camera=framing_vf_by_camera(settings.get("framing"), video_files),
        )
        results.append({"id": "video", "label": "Episode video", "path": out_path, "kind": "video"})

    # ----- extras (all work on the finished file, so timestamps line up) -----
    transcript_state = None
    tighten_spans = None
    if want_video and out_path:
        if want_tighten:
            progress(render_end + 0.01, "Tightening long pauses")
            tight_dir = os.path.join(WORK_DIR, "tighten")
            tight_out = os.path.join(tight_dir, "tightened.mp4")
            os.makedirs(tight_dir, exist_ok=True)
            tightened = postproc.tighten_pauses(
                out_path, tight_out, tight_dir,
                min_pause=pause_min_sec, keep_pause=0.5,
                progress_cb=lambda p: progress(render_end + 0.01 + p * 0.09, "Tightening long pauses"),
            )
            if tightened is not None and os.path.isfile(tight_out):
                removed, tighten_spans = tightened
                shutil.move(tight_out, out_path)
                results.append({"id": "tighten_note", "kind": "info", "path": None,
                                "label": f"Removed {removed:.0f} seconds of dead air"})

        times = rms = final_wav = None
        final_dur = 0.0
        if want_transcript or clips_count > 0:
            post_dir = os.path.join(WORK_DIR, "post")
            times, rms, final_dur, final_wav = postproc.analyze_media_rms(out_path, post_dir, tag="final")

        if want_transcript:
            if not TRANSCRIBE_OK:
                results.append({"id": "transcript_missing", "kind": "info", "path": None,
                                "label": "Transcript skipped: speech engine not installed. "
                                         "Run RUN_ME.bat again to install it."})
            else:
                progress(0.62, "Transcribing (first run downloads the speech model)")
                segments = transcribe.transcribe_wav(
                    final_wav, model_size=whisper_model, duration=final_dur,
                    progress_cb=lambda p: progress(0.62 + p * 0.24, f"Transcribing {int(p * 100)}%"),
                )
                if not segments:
                    results.append({"id": "transcript_empty", "kind": "info", "path": None,
                                    "label": "No speech was detected, so no transcript was made."})
                else:
                    mics = list_speaker_mics(analysis, audio_pairing)
                    segments = assign_speakers(segments, mics, edl["offsets"],
                                               edl["program_start"], tighten_spans)
                    labels = default_speaker_labels(mics)
                    apply_speaker_labels(segments, labels)
                    t_txt = base + "_transcript.txt"
                    t_srt = base + "_captions.srt"
                    t_ch = base + "_youtube_chapters.txt"
                    transcribe.write_transcript_txt(segments, t_txt)
                    transcribe.write_srt(segments, t_srt)
                    transcribe.write_chapters_txt(transcribe.build_chapters(segments), t_ch)
                    results.append({"id": "transcript", "label": "Transcript (.txt)", "path": t_txt, "kind": "transcript"})
                    results.append({"id": "srt", "label": "Captions (.srt)", "path": t_srt, "kind": "srt"})
                    results.append({"id": "chapters", "label": "YouTube chapters", "path": t_ch, "kind": "chapters"})
                    transcript_state = {"segments": segments, "speakers": labels}

        if clips_count > 0 and times is not None:
            progress(0.88, "Cutting social clips")
            windows = postproc.pick_clip_windows(times, rms, final_dur, n_clips=clips_count, clip_len=clips_len)
            if windows:
                clip_files = postproc.render_social_clips(
                    out_path, windows, base,
                    progress_cb=lambda p: progress(0.88 + p * 0.08, "Cutting social clips"),
                )
                for ci, (p, kind) in enumerate(clip_files):
                    nice = os.path.basename(p).replace(os.path.basename(base) + "_", "")
                    results.append({"id": f"clip{ci}", "label": f"Social clip: {nice}", "path": p, "kind": kind})

    if have_extras and not (want_video and out_path):
        results.append({
            "id": "extras_skipped", "kind": "info", "path": None,
            "label": "Transcript/clips/pause-tightening were skipped: they work on the "
                     "rendered video, so the 'Rendered video file' output must be turned on.",
        })

    if want_fcpxml:
        progress(0.97, "Writing editable project file")
        fcpxml_path = base + ".fcpxml"
        audio_paths = {n: src_paths[n] for n in audio_only_files if has_audio(n)}
        audio_specs = {n: files[n] for n in audio_only_files if has_audio(n)}
        fcpxml_export.export_fcpxml(
            camera_paths, camera_specs, edl, fcpxml_path,
            audio_paths=audio_paths, audio_specs=audio_specs,
            project_name=f"{episode} (AutoCut)",
        )
        results.append({"id": "fcpxml", "label": "Editable project (FCPXML)", "path": fcpxml_path, "kind": "fcpxml"})

    return results, transcript_state


def run_process(settings):
    """Single-episode flow: process in a background thread."""
    try:
        set_job(stage="processing", progress=0.0, message="Getting ready", error=None,
                results=[], transcript_segments=None, transcript_speakers=None, edit_count=0)
        with LOCK:
            analysis = {
                "files": dict(JOB["files"]),
                "offsets": dict(JOB["offsets"]),
                "video_files": list(JOB["video_files"]),
                "audio_only_files": list(JOB["audio_only_files"]),
                "src_paths": dict(JOB["src_paths"]),
            }

        def progress(p, msg):
            set_job(progress=p, message=msg)

        results, tstate = process_core(analysis, settings, progress)
        set_job(stage="done", progress=1.0, message="Done", results=results,
                transcript_segments=(tstate or {}).get("segments"),
                transcript_speakers=(tstate or {}).get("speakers"),
                export_quality=settings.get("export_quality", "standard"),
                content_start=0.0, content_end=None)
        save_project()
    except Exception as e:
        traceback.print_exc()
        set_job(stage="error", error=str(e))


# ---------------------------------------------------------------------------
# Batch mode: process a whole folder of sessions, one episode per subfolder
# ---------------------------------------------------------------------------

def scan_batch(root):
    """Group media files under `root`: each subfolder with at least one video
    file is an episode; loose media files in the root form one more episode."""
    root = os.path.abspath(os.path.expanduser(root.strip().strip('"')))
    if not os.path.isdir(root):
        raise ValueError(f"That folder wasn't found: {root}")

    def media_in(d):
        out = {}
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            return out
        for fn in entries:
            p = os.path.join(d, fn)
            if os.path.isfile(p) and os.path.splitext(fn)[1].lower() in MEDIA_EXT:
                out[fn] = p
        return out

    groups = []
    for entry in sorted(os.listdir(root)):
        sub = os.path.join(root, entry)
        if os.path.isdir(sub):
            files = media_in(sub)
            n_video = sum(1 for f in files if os.path.splitext(f)[1].lower() in MEDIA_VIDEO_EXT)
            if files and n_video > 0:
                groups.append({"name": safe_episode_name(entry), "dir": sub, "files": files, "n_video": n_video})
    loose = media_in(root)
    n_video = sum(1 for f in loose if os.path.splitext(f)[1].lower() in MEDIA_VIDEO_EXT)
    if loose and n_video > 0:
        groups.append({"name": safe_episode_name(os.path.basename(root)), "dir": root,
                       "files": loose, "n_video": n_video})
    return root, groups


def run_batch(root, settings):
    try:
        _, groups = scan_batch(root)
        if not groups:
            set_job(stage="error", error="No episodes found in that folder.")
            return
        all_results = []
        summary = []
        total = len(groups)
        for i, g in enumerate(groups):
            set_job(batch={"index": i + 1, "total": total, "episode": g["name"]},
                    progress=0.0, message="Starting")
            try:
                reset_work()

                def progress(p, msg):
                    set_job(progress=p, message=msg)

                analysis = analyze_core(g["files"], lambda p, m: progress(p * 0.25, m))
                ep_settings = dict(settings)
                ep_settings["episode_name"] = g["name"]
                results, _tstate = process_core(analysis, ep_settings,
                                                lambda p, m: progress(0.25 + p * 0.75, m))
                for r in results:
                    r = dict(r)
                    r["id"] = f"ep{i}_{r['id']}"
                    r["label"] = f"{g['name']}: {r['label']}"
                    all_results.append(r)
                summary.append({"episode": g["name"], "ok": True, "error": None})
            except Exception as e:
                traceback.print_exc()
                summary.append({"episode": g["name"], "ok": False, "error": str(e)})
        set_job(stage="done", progress=1.0, message="Batch finished",
                results=all_results, batch=None, batch_summary=summary)
    except Exception as e:
        traceback.print_exc()
        set_job(stage="error", error=str(e), batch=None)


# ---------------------------------------------------------------------------
# Transcript editing: cut the video by striking out lines of the transcript
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Project files: auto-save the session after every finished cut, resume later
# ---------------------------------------------------------------------------

PROJECT_KEYS = ("results", "transcript_segments", "transcript_speakers", "edit_count",
                "export_quality", "content_start", "content_end")


def save_project():
    """Write a .autocut.json next to the outputs so the session can be resumed."""
    try:
        with LOCK:
            if not JOB["results"]:
                return
            video = next((r for r in JOB["results"] if r["kind"] == "video" and r.get("path")), None)
            data = {k: JOB[k] for k in PROJECT_KEYS}
        if not video:
            return
        stem = os.path.splitext(os.path.basename(video["path"]))[0]
        data["saved_at"] = time.strftime("%Y-%m-%d %H:%M")
        data["name"] = stem
        with open(os.path.join(OUTPUT_DIR, f"{stem}.autocut.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        traceback.print_exc()  # saving must never break the run


def list_projects():
    out = []
    for fn in sorted(os.listdir(OUTPUT_DIR), reverse=True):
        if not fn.endswith(".autocut.json"):
            continue
        try:
            with open(os.path.join(OUTPUT_DIR, fn), encoding="utf-8") as f:
                data = json.load(f)
            video = next((r for r in data.get("results", [])
                          if r.get("kind") == "video" and r.get("path")), None)
            out.append({
                "file": fn,
                "name": data.get("name", fn),
                "saved_at": data.get("saved_at", ""),
                "video_exists": bool(video and os.path.isfile(video["path"])),
                "has_transcript": bool(data.get("transcript_segments")),
            })
        except Exception:
            continue
    return out


def load_project(fn):
    path = os.path.join(OUTPUT_DIR, os.path.basename(fn))
    if not os.path.isfile(path):
        raise ValueError("That project file wasn't found.")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    results = [r for r in data.get("results", []) if not r.get("path") or os.path.isfile(r["path"])]
    if not any(r.get("kind") == "video" for r in results):
        raise ValueError("The video for that project has been moved or deleted.")
    with LOCK:
        JOB.clear()
        JOB.update(fresh_job())
        JOB.update(
            stage="done", progress=1.0, message="Project loaded", results=results,
            transcript_segments=data.get("transcript_segments"),
            transcript_speakers=data.get("transcript_speakers"),
            edit_count=int(data.get("edit_count", 0) or 0),
            export_quality=data.get("export_quality", "standard"),
            content_start=float(data.get("content_start", 0.0) or 0.0),
            content_end=data.get("content_end"),
        )


def run_brand(spec):
    """Attach intro/outro bumpers and burn animated text overlays onto the
    current video, shifting the transcript to match."""
    try:
        with LOCK:
            results = [dict(r) for r in JOB["results"]]
            segments = [dict(s) for s in (JOB["transcript_segments"] or [])] or None
            labels = dict(JOB["transcript_speakers"] or {})
            quality = JOB["export_quality"]
        editor.set_encode_profile(quality)
        cur = next((r for r in results if r["id"] == "video" and r.get("path")), None)
        if not cur or not os.path.isfile(cur["path"]):
            set_job(stage="error", error="The current video file is missing.")
            return
        src = cur["path"]
        info = media_utils.probe(src)
        w, h, fps = info["width"], info["height"], info["fps"] or 30.0
        content_dur = info["duration"]

        overlays = spec.get("overlays") or []
        intro = spec.get("intro") or None
        outro = spec.get("outro") or None
        if not overlays and not intro and not outro:
            set_job(stage="error", error="Nothing to add -- pick an intro/outro or add a text overlay.")
            return

        work = os.path.join(WORK_DIR, "brand")
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work, exist_ok=True)
        stem = os.path.splitext(os.path.basename(src))[0]
        new_video = os.path.join(os.path.dirname(src), f"{stem}_brand.mp4")
        new_base = os.path.splitext(new_video)[0]

        content = src
        filters = build_overlay_filters(overlays, h) if overlays else []
        need_join = bool(intro or outro)
        if filters or need_join:
            # re-encode the episode once (with titles if any) so every piece of
            # the final join shares identical encoding
            set_job(progress=0.05, message="Drawing titles" if filters else "Preparing episode")
            content = os.path.join(work, "content.mp4")
            cmd = [media_utils.ffmpeg_path(), "-y", "-nostdin", "-i", src]
            if filters:
                cmd += ["-vf", ",".join(filters)]
            else:
                cmd += ["-vf", f"fps={fps},setsar=1"]
            cmd += editor.video_enc_args(fps) + editor.audio_enc_args() + [content]
            media_utils.run(cmd)

        intro_dur = outro_dur = 0.0
        parts = []
        if intro:
            set_job(progress=0.5, message="Attaching intro")
            ip = os.path.join(work, "intro.mp4")
            intro_dur = _prep_bumper(intro, w, h, fps, ip)
            parts.append(ip)
        parts.append(content)
        if outro:
            set_job(progress=0.65, message="Attaching outro")
            op = os.path.join(work, "outro.mp4")
            outro_dur = _prep_bumper(outro, w, h, fps, op)
            parts.append(op)

        set_job(progress=0.8, message="Joining everything")
        if len(parts) > 1:
            joined = os.path.join(work, "joined.mp4")
            editor.concat_segments(parts, joined)
            editor.faststart_remux(joined, new_video)
        else:
            editor.faststart_remux(content, new_video)

        new_entries = [{"id": "video", "label": "Episode video (branded)", "path": new_video, "kind": "video"}]
        if segments:
            set_job(progress=0.92, message="Shifting the transcript")
            if intro_dur > 0:
                for s in segments:
                    s["start"] = float(s["start"]) + intro_dur
                    s["end"] = float(s["end"]) + intro_dur
            apply_speaker_labels(segments, labels)
            t_txt = new_base + "_transcript.txt"
            t_srt = new_base + "_captions.srt"
            t_ch = new_base + "_youtube_chapters.txt"
            transcribe.write_transcript_txt(segments, t_txt)
            transcribe.write_srt(segments, t_srt)
            transcribe.write_chapters_txt(transcribe.build_chapters(segments), t_ch)
            new_entries += [
                {"id": "transcript", "label": "Transcript (.txt)", "path": t_txt, "kind": "transcript"},
                {"id": "srt", "label": "Captions (.srt)", "path": t_srt, "kind": "srt"},
                {"id": "chapters", "label": "YouTube chapters", "path": t_ch, "kind": "chapters"},
            ]
        for r in results:
            if r["id"] in ("video", "transcript", "srt", "chapters"):
                r["id"] = f"{r['id']}_prebrand"
                r["label"] = f"Before branding: {r['label']}"
        set_job(stage="done", progress=1.0, message="Done", results=new_entries + results,
                transcript_segments=segments,
                content_start=intro_dur,
                content_end=intro_dur + content_dur)
        save_project()
    except Exception as e:
        traceback.print_exc()
        set_job(stage="error", error=str(e))


def run_transcript_edit(edit):
    try:
        with LOCK:
            segments = [dict(s) for s in (JOB["transcript_segments"] or [])]
            labels = dict(JOB["transcript_speakers"] or {})
            results = [dict(r) for r in JOB["results"]]
            edit_count = JOB["edit_count"]
            quality = JOB["export_quality"]
            content_start = float(JOB["content_start"] or 0.0)
            content_end = JOB["content_end"]
        editor.set_encode_profile(quality)
        if not segments:
            set_job(stage="error", error="No transcript to edit.")
            return
        cur = next((r for r in results if r["id"] == "video" and r.get("path")), None)
        if not cur or not os.path.isfile(cur["path"]):
            set_job(stage="error", error="The current video file is missing.")
            return
        src = cur["path"]

        deleted = set()
        for i in edit.get("deleted", []) or []:
            try:
                deleted.add(int(i))
            except (TypeError, ValueError):
                pass
        for k, v in (edit.get("texts", {}) or {}).items():
            try:
                ki = int(k)
            except (TypeError, ValueError):
                continue
            if 0 <= ki < len(segments) and isinstance(v, str) and v.strip():
                segments[ki]["text"] = v.strip()
        for k, v in (edit.get("speakers", {}) or {}).items():
            if k in labels and isinstance(v, str) and v.strip():
                labels[k] = v.strip()[:40]

        info = media_utils.probe(src)
        duration = info["duration"] or (segments[-1]["end"] + 1.0)

        kept_segments = [s for i, s in enumerate(segments) if i not in deleted]
        if not kept_segments:
            set_job(stage="error", error="Every line was deleted -- nothing left to keep.")
            return

        out_dir = os.path.dirname(src)
        stem = os.path.splitext(os.path.basename(src))[0]
        new_base = os.path.join(out_dir, f"{stem}_edit{edit_count + 1}")
        new_video = new_base + ".mp4"

        removed_total = 0.0
        if deleted:
            cut_ranges = cut_ranges_from_deleted(segments, deleted, duration)
            # never cut into an attached intro or outro
            lo = content_start
            hi = float(content_end) if content_end else duration
            cut_ranges = [(max(s, lo), min(e, hi)) for s, e in cut_ranges]
            cut_ranges = [(s, e) for s, e in cut_ranges if e - s > 0.01]
            keep_spans = postproc.spans_after_cuts(cut_ranges, duration)
            if not keep_spans:
                set_job(stage="error", error="Those cuts would remove the whole video.")
                return
            set_job(progress=0.05, message=f"Cutting {len(cut_ranges)} section(s) out")
            work = os.path.join(WORK_DIR, "transcript_edit")
            shutil.rmtree(work, ignore_errors=True)
            postproc.cut_video_spans(
                src, keep_spans, new_video, work,
                progress_cb=lambda p: set_job(progress=0.05 + p * 0.8,
                                              message="Re-cutting the video"),
            )
            for s in kept_segments:
                s["start"] = postproc.time_after_cuts(cut_ranges, float(s["start"]))
                s["end"] = max(s["start"] + 0.2, postproc.time_after_cuts(cut_ranges, float(s["end"])))
            removed_total = sum(e - s for s, e in cut_ranges)
        else:
            shutil.copyfile(src, new_video)  # text/speaker-name-only edit

        set_job(progress=0.9, message="Rewriting transcript files")
        apply_speaker_labels(kept_segments, labels)
        t_txt = new_base + "_transcript.txt"
        t_srt = new_base + "_captions.srt"
        t_ch = new_base + "_youtube_chapters.txt"
        transcribe.write_transcript_txt(kept_segments, t_txt)
        transcribe.write_srt(kept_segments, t_srt)
        transcribe.write_chapters_txt(transcribe.build_chapters(kept_segments), t_ch)

        # retire the old primary entries, promote the new cut
        for r in results:
            if r["id"] in ("video", "transcript", "srt", "chapters"):
                r["id"] = f"{r['id']}_v{edit_count}"
                r["label"] = f"Before edit #{edit_count + 1}: {r['label']}"
        new_entries = [
            {"id": "video", "label": "Episode video (edited)", "path": new_video, "kind": "video"},
            {"id": "transcript", "label": "Transcript (.txt)", "path": t_txt, "kind": "transcript"},
            {"id": "srt", "label": "Captions (.srt)", "path": t_srt, "kind": "srt"},
            {"id": "chapters", "label": "YouTube chapters", "path": t_ch, "kind": "chapters"},
        ]
        if removed_total > 0:
            new_entries.append({"id": f"edit_note_{edit_count + 1}", "kind": "info", "path": None,
                                "label": f"Transcript edit removed {removed_total:.0f} seconds"})
        set_job(stage="done", progress=1.0, message="Done", results=new_entries + results,
                transcript_segments=kept_segments, transcript_speakers=labels,
                edit_count=edit_count + 1,
                content_end=(float(content_end) - removed_total) if content_end else None)
        save_project()
    except Exception as e:
        traceback.print_exc()
        set_job(stage="error", error=str(e))


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

CTYPE_BY_EXT = {
    ".mp4": "video/mp4",
    ".fcpxml": "application/xml",
    ".txt": "text/plain; charset=utf-8",
    ".srt": "application/x-subrip",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "TellinghouseMediaAutoCut/1.2"

    _LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def _local_ok(self):
        """Only answer requests addressed to this machine (blocks DNS rebinding),
        and only accept browser POSTs that come from our own pages (blocks
        drive-by cross-site requests to localhost)."""
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            host = host.split("]", 1)[0].lstrip("[")
        else:
            host = host.split(":", 1)[0]
        if host not in self._LOCAL_HOSTS:
            return False
        if self.command == "POST":
            origin = self.headers.get("Origin")
            if origin:  # our own pages always have a local origin; anything else is foreign
                try:
                    ohost = (urlsplit(origin).hostname or "").lower()
                except ValueError:
                    return False
                if ohost not in self._LOCAL_HOSTS:
                    return False
        return True

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message, status=400):
        self._send_json({"error": message}, status=status)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def do_GET(self):
        if not self._local_ok():
            return self._send_error_json("Forbidden", status=403)
        parsed = urlsplit(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/" or path == "/index.html" or path == "/app.html":
            return self._serve_file(os.path.join(WEB_DIR, "app.html"), "text/html")

        if path == "/api/status":
            with LOCK:
                snapshot = {
                    "stage": JOB["stage"],
                    "progress": JOB["progress"],
                    "message": JOB["message"],
                    "error": JOB["error"],
                    "files": {n: {k: v for k, v in f.items() if k not in ("path",)} for n, f in JOB["files"].items()},
                    "offsets": JOB["offsets"],
                    "confidence": JOB["confidence"],
                    "reference": JOB["reference"],
                    "video_files": JOB["video_files"],
                    "audio_only_files": JOB["audio_only_files"],
                    "trim_duration": JOB["trim_duration"],
                    "keep_full_duration": JOB["keep_full_duration"],
                    "results": [
                        {"id": r["id"], "label": r["label"], "kind": r["kind"],
                         "name": os.path.basename(r["path"]) if r.get("path") else None}
                        for r in JOB["results"]
                    ],
                    "has_video": any(r["kind"] == "video" for r in JOB["results"]),
                    "batch": JOB["batch"],
                    "batch_summary": JOB["batch_summary"],
                    "transcribe_available": TRANSCRIBE_OK,
                    "output_dir": OUTPUT_DIR,
                    "luts": sorted(f for f in os.listdir(LUT_DIR) if f.lower().endswith(".cube")),
                    "transcript_ready": bool(JOB["transcript_segments"]),
                }
            return self._send_json(snapshot)

        if path == "/api/projects":
            return self._send_json({"projects": list_projects()})

        if path == "/api/transcript":
            with LOCK:
                segs = JOB["transcript_segments"]
                labels = dict(JOB["transcript_speakers"] or {})
            if not segs:
                return self._send_error_json(
                    "No transcript available. Make a video with the transcript option turned on first.",
                    status=404)
            return self._send_json({
                "segments": [
                    {"i": i, "start": round(s["start"], 2), "end": round(s["end"], 2),
                     "text": s["text"], "speaker": s.get("speaker")}
                    for i, s in enumerate(segs)
                ],
                "speakers": labels,
            })

        if path == "/api/download":
            rid = qs.get("id", [""])[0] or qs.get("file", [""])[0]
            with LOCK:
                match = next((r for r in JOB["results"] if r["id"] == rid and r.get("path")), None)
                if match is None and rid == "video":
                    match = next((r for r in JOB["results"] if r["kind"] == "video" and r.get("path")), None)
                p = match["path"] if match else None
            if not p or not os.path.isfile(p):
                return self._send_error_json("Not ready yet", status=404)
            ctype = CTYPE_BY_EXT.get(os.path.splitext(p)[1].lower(), "application/octet-stream")
            return self._serve_file(p, ctype, as_attachment=True)

        if path == "/api/color_preview":
            name = unquote(qs.get("name", [""])[0])
            with LOCK:
                src = JOB["src_paths"].get(name)
                dur = JOB["files"].get(name, {}).get("duration", 0.0)
            if not src or not os.path.isfile(src):
                return self._send_error_json("Unknown track", status=404)
            cfg = {
                "exposure": qs.get("exposure", ["0"])[0],
                "contrast": qs.get("contrast", ["1"])[0],
                "saturation": qs.get("saturation", ["1"])[0],
                "warmth": qs.get("warmth", ["0"])[0],
                "lut": qs.get("lut", [""])[0] or None,
            }
            framing = build_framing_vf({
                "zoom": qs.get("zoom", ["1"])[0],
                "panx": qs.get("panx", ["0"])[0],
                "pany": qs.get("pany", ["0"])[0],
            })
            vf = "scale=480:-2"
            if framing:
                vf = f"{framing},{vf}"
            extra = build_color_vf(cfg)
            if extra:
                vf = f"{vf},{extra}"
            out_jpg = os.path.join(WORK_DIR, f"preview_{threading.get_ident()}_{int(time.time()*1000)}.jpg")
            try:
                media_utils.run([
                    media_utils.ffmpeg_path(), "-y", "-nostdin",
                    "-ss", f"{max(0.0, dur * 0.4):.3f}", "-i", src,
                    "-frames:v", "1", "-vf", vf, "-q:v", "4", out_jpg,
                ])
                with open(out_jpg, "rb") as f:
                    data = f.read()
            except Exception as e:
                return self._send_error_json(f"Preview failed: {e}", status=500)
            finally:
                try:
                    os.remove(out_jpg)
                except OSError:
                    pass
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (ConnectionError, BrokenPipeError):
                pass
            return

        self._send_error_json("Not found", status=404)

    def _serve_file(self, path, ctype, as_attachment=False):
        """Serve a file, honouring HTTP Range requests so the browser's video
        player can seek in the preview."""
        if not os.path.isfile(path):
            return self._send_error_json("Not found", status=404)
        size = os.path.getsize(path)
        start, end, status = 0, size - 1, 200

        range_header = self.headers.get("Range", "")
        if range_header.startswith("bytes=") and size > 0:
            try:
                spec = range_header.split("=", 1)[1].split(",")[0].strip()
                s, _, e = spec.partition("-")
                if s:
                    start = int(s)
                    end = int(e) if e else size - 1
                elif e:
                    start = max(0, size - int(e))
                    end = size - 1
                end = min(end, size - 1)
                if 0 <= start <= end:
                    status = 206
                else:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
            except ValueError:
                start, end, status = 0, size - 1, 200

        length = end - start + 1
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if as_attachment:
                self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(path)}"')
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (ConnectionError, BrokenPipeError):
            pass  # browser closed the connection (normal while scrubbing video)

    def do_POST(self):
        if not self._local_ok():
            return self._send_error_json("Forbidden", status=403)
        parsed = urlsplit(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/upload":
            name = unquote(qs.get("name", [""])[0])
            overwrite = qs.get("overwrite", ["0"])[0] == "1"
            is_lut = qs.get("kind", [""])[0] == "lut"
            if not name or "/" in name or "\\" in name or ":" in name or name.startswith("."):
                return self._send_error_json("Invalid filename")
            is_bumper = qs.get("kind", [""])[0] == "bumper"
            if is_bumper:
                if os.path.splitext(name)[1].lower() not in MEDIA_VIDEO_EXT:
                    return self._send_error_json("Intro/outro must be a video file")
                dest = os.path.join(BUMPER_DIR, name)
                length = int(self.headers.get("Content-Length", 0))
                remaining = length
                with open(dest, "wb") as f:
                    while remaining > 0:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
                return self._send_json({"ok": True, "name": name, "size": length, "kind": "bumper"})
            if is_lut:
                if not name.lower().endswith(".cube"):
                    return self._send_error_json("LUTs must be .cube files")
                stored = name
                dest = os.path.join(LUT_DIR, stored)
                length = int(self.headers.get("Content-Length", 0))
                remaining = length
                with open(dest, "wb") as f:
                    while remaining > 0:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
                return self._send_json({"ok": True, "name": stored, "size": length, "kind": "lut"})
            length = int(self.headers.get("Content-Length", 0))
            # Pick the stored name and create the file in one atomic step, so two
            # same-named files uploaded at the same moment (two identical cameras,
            # dropped together) can't both claim the same filename.
            with UPLOAD_NAME_LOCK:
                stored = name if overwrite else unique_upload_name(name)
                dest = os.path.join(UPLOAD_DIR, stored)
                f = open(dest, "wb")
            try:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            finally:
                f.close()
            return self._send_json({"ok": True, "name": stored, "size": length})

        if path == "/api/analyze":
            body = self._read_json_body()
            names = body.get("files", [])
            with LOCK:
                if JOB["stage"] in ("analyzing", "processing"):
                    return self._send_error_json("Busy with the current step -- give it a moment.", status=409)
                JOB.update(stage="analyzing", progress=0.0, message="Reading files", error=None,
                           batch=None, batch_summary=None)
            t = threading.Thread(target=run_analyze, args=(names,), daemon=True)
            t.start()
            return self._send_json({"ok": True})

        if path == "/api/process":
            body = self._read_json_body()
            with LOCK:
                if JOB["stage"] not in ("ready", "done", "error"):
                    return self._send_error_json("Already processing", status=409)
                if not JOB["files"]:
                    return self._send_error_json("Analyze your footage first.", status=400)
                JOB.update(stage="processing", progress=0.0, message="Getting ready", error=None)
            t = threading.Thread(target=run_process, args=(body,), daemon=True)
            t.start()
            return self._send_json({"ok": True})

        if path == "/api/brand":
            body = self._read_json_body()
            with LOCK:
                if JOB["stage"] in ("analyzing", "processing"):
                    return self._send_error_json("Busy with the current step -- give it a moment.", status=409)
                if not any(r["kind"] == "video" and r.get("path") for r in JOB["results"]):
                    return self._send_error_json("Make a video first.", status=400)
                JOB.update(stage="processing", progress=0.0, message="Adding intro and titles", error=None)
            t = threading.Thread(target=run_brand, args=(body,), daemon=True)
            t.start()
            return self._send_json({"ok": True})

        if path == "/api/project_load":
            body = self._read_json_body()
            with LOCK:
                if JOB["stage"] in ("analyzing", "processing"):
                    return self._send_error_json("Busy with the current step -- give it a moment.", status=409)
            try:
                load_project(body.get("file", ""))
                return self._send_json({"ok": True})
            except Exception as e:
                return self._send_error_json(str(e))

        if path == "/api/transcript_edit":
            body = self._read_json_body()
            with LOCK:
                if JOB["stage"] in ("analyzing", "processing"):
                    return self._send_error_json("Busy with the current step -- give it a moment.", status=409)
                if not JOB["transcript_segments"]:
                    return self._send_error_json("No transcript to edit.", status=400)
                JOB.update(stage="processing", progress=0.0, message="Applying transcript edits", error=None)
            t = threading.Thread(target=run_transcript_edit, args=(body,), daemon=True)
            t.start()
            return self._send_json({"ok": True})

        if path == "/api/batch_scan":
            body = self._read_json_body()
            try:
                root, groups = scan_batch(body.get("path", ""))
                return self._send_json({"ok": True, "root": root, "episodes": [
                    {"name": g["name"], "n_files": len(g["files"]), "n_video": g["n_video"]}
                    for g in groups
                ]})
            except Exception as e:
                return self._send_error_json(str(e))

        if path == "/api/batch_run":
            body = self._read_json_body()
            with LOCK:
                if JOB["stage"] in ("analyzing", "processing"):
                    return self._send_error_json("Busy with the current step -- give it a moment.", status=409)
                JOB.clear()
                JOB.update(fresh_job())
                JOB.update(stage="processing", progress=0.0, message="Starting batch", error=None)
            t = threading.Thread(target=run_batch,
                                 args=(body.get("path", ""), body.get("settings", {}) or {}),
                                 daemon=True)
            t.start()
            return self._send_json({"ok": True})

        if path == "/api/reset":
            with LOCK:
                if JOB["stage"] in ("analyzing", "processing"):
                    return self._send_error_json("Still working -- wait for the current step to finish.", status=409)
                JOB.clear()
                JOB.update(fresh_job())
            reset_work()
            for f in os.listdir(UPLOAD_DIR):
                try:
                    os.remove(os.path.join(UPLOAD_DIR, f))
                except OSError:
                    pass
            for f in os.listdir(LUT_DIR):
                try:
                    os.remove(os.path.join(LUT_DIR, f))
                except OSError:
                    pass
            return self._send_json({"ok": True})

        self._send_error_json("Not found", status=404)


def _fatal(title, message):
    """Show a fatal startup error: console if we have one, message box if not."""
    print("=" * 70)
    print(message)
    print("=" * 70)
    if FROZEN and os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)  # MB_ICONERROR
            return
        except Exception:
            pass
    try:
        input("Press Enter to exit...")
    except (EOFError, RuntimeError):
        pass


def main():
    if not media_utils.has_ffmpeg():
        _fatal("Tellinghouse AutoCut",
               "ffmpeg/ffprobe was not found on your PATH.\n"
               "Install ffmpeg first -- see README.md for instructions.")
        return

    httpd = None
    port = None
    for attempt_port in range(8765, 8785):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", attempt_port), Handler)
            port = attempt_port
            break
        except OSError:
            continue

    if httpd is None:
        _fatal("Tellinghouse AutoCut",
               "Could not find a free port (8765-8784).\n"
               "Close other copies of AutoCut and try again.")
        return

    url = f"http://127.0.0.1:{port}/"
    print(f"Tellinghouse Media AutoCut is running at {url}")
    print(f"Finished videos are saved to: {OUTPUT_DIR}")
    if not TRANSCRIBE_OK:
        print("(Transcription engine not installed yet -- run RUN_ME.bat to set it up.)")
    print("Leave this window open while you use it. Close it when you're done.")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
