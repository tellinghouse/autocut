"""Thin wrappers around ffmpeg/ffprobe: probing media info and pulling analysis audio."""

import json
import os
import shutil
import subprocess
import wave

import numpy as np


class MediaError(RuntimeError):
    pass


def _find_tool(name, env_var):
    override = os.environ.get(env_var)
    if override and os.path.isfile(override):
        return override
    found = shutil.which(name)
    if found:
        return found
    # Common Windows install locations, in case ffmpeg isn't on PATH.
    for candidate in (
        r"C:\ffmpeg\bin\%s.exe" % name,
        r"C:\Program Files\ffmpeg\bin\%s.exe" % name,
    ):
        if os.path.isfile(candidate):
            return candidate
    raise MediaError(
        f"Could not find '{name}'. Install ffmpeg and make sure it's on your PATH "
        f"(see README.md for instructions)."
    )


def ffmpeg_path():
    return _find_tool("ffmpeg", "AUTOCUT_FFMPEG")


def ffprobe_path():
    return _find_tool("ffprobe", "AUTOCUT_FFPROBE")


def run(cmd, check=True):
    """Run a subprocess command, returning CompletedProcess. Raises MediaError on failure."""
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and proc.returncode != 0:
        raise MediaError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr[-4000:]}"
        )
    return proc


def probe(path):
    """Return a dict describing a media file: duration, video/audio stream info."""
    cmd = [
        ffprobe_path(),
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    proc = run(cmd)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise MediaError(f"Could not read media info for {path}: {e}")

    fmt = data.get("format", {})
    duration = float(fmt.get("duration", 0.0) or 0.0)

    video_stream = None
    audio_stream = None
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and video_stream is None:
            # Skip single-frame "video" streams that are really embedded cover art.
            if s.get("disposition", {}).get("attached_pic") == 1:
                continue
            video_stream = s
        elif s.get("codec_type") == "audio" and audio_stream is None:
            audio_stream = s

    width = height = 0
    fps = 0.0
    if video_stream:
        width = int(video_stream.get("width") or 0)
        height = int(video_stream.get("height") or 0)
        rate = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or "0/1"
        try:
            num, den = rate.split("/")
            den = float(den)
            fps = float(num) / den if den else 0.0
        except Exception:
            fps = 0.0
        if not duration:
            duration = float(video_stream.get("duration", 0.0) or 0.0)

    sample_rate = 0
    if audio_stream:
        sample_rate = int(audio_stream.get("sample_rate") or 0)
        if not duration:
            duration = float(audio_stream.get("duration", 0.0) or 0.0)

    return {
        "path": path,
        "duration": duration,
        "has_video": video_stream is not None,
        "has_audio": audio_stream is not None,
        "width": width,
        "height": height,
        "fps": fps,
        "sample_rate": sample_rate,
    }


def extract_audio_wav(path, out_path, sr=16000, start=None, duration=None):
    """Extract mono PCM16 WAV audio from any media file at a fixed sample rate."""
    cmd = [ffmpeg_path(), "-y", "-nostdin"]
    if start is not None:
        cmd += ["-ss", f"{max(0.0, start):.3f}"]
    cmd += ["-i", path]
    if duration is not None:
        cmd += ["-t", f"{max(0.0, duration):.3f}"]
    cmd += [
        "-vn",
        "-ac", "1",
        "-ar", str(sr),
        "-acodec", "pcm_s16le",
        "-f", "wav",
        out_path,
    ]
    run(cmd)
    return out_path


def read_wav_mono(path):
    """Read a mono PCM16 WAV file into a float32 numpy array in [-1, 1]. Returns (audio, sr)."""
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)
        sampwidth = wf.getsampwidth()
        n_channels = wf.getnchannels()
    if sampwidth != 2:
        raise MediaError(f"Expected 16-bit PCM audio, got sampwidth={sampwidth} for {path}")
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if n_channels > 1:
        audio = audio.reshape(-1, n_channels).mean(axis=1)
    return audio, sr


def has_ffmpeg():
    try:
        ffmpeg_path()
        ffprobe_path()
        return True
    except MediaError:
        return False
