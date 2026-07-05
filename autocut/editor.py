"""Timeline construction and rendering: turns synced tracks + a cut list into a
finished video (or the pieces needed for an FCPXML export).
"""

import os
from collections import Counter

import numpy as np

from . import media_utils
from .active_speaker import frame_rms, smooth, resample_to_grid, detect_active_segments


# ---------------------------------------------------------------------------
# Timeline / EDL construction
# ---------------------------------------------------------------------------

def compute_program_range(camera_ids, offsets, durations, mode):
    """Compute the shared [program_start, program_end) window on the common timeline.

    mode="trim": the overlap common to every camera (safe, no gaps).
    mode="keep_full": spans from the earliest camera to the latest camera; cameras
    that don't cover the whole range simply aren't available outside their span.
    """
    starts = {cid: offsets.get(cid, 0.0) for cid in camera_ids}
    ends = {cid: offsets.get(cid, 0.0) + durations[cid] for cid in camera_ids}

    if mode == "trim":
        program_start = max(starts.values())
        program_end = min(ends.values())
    else:
        program_start = min(starts.values())
        program_end = max(ends.values())

    if program_end <= program_start:
        raise ValueError(
            "These tracks don't overlap enough to build a timeline. "
            "Double-check the detected sync offsets (see the Analyze step)."
        )
    return program_start, program_end, starts, ends


def build_edl(
    camera_ids,
    offsets,
    durations,
    energy_audio_by_camera,
    mode="keep_full",
    hop_sec=0.05,
    frame_sec=0.2,
    min_shot_sec=2.0,
    switch_margin=1.15,
):
    """Build the edit decision list: which camera is on screen, and when.

    camera_ids: list of video track ids (the on-screen candidates).
    offsets: {track_id: offset_seconds} for every track id referenced below
        (camera ids *and* whatever audio ids they use for energy analysis).
    durations: {camera_id: duration_seconds} of each camera's own video.
    energy_audio_by_camera: {camera_id: (audio_array, sr, audio_track_id)} -- the
        audio used to gauge "who's talking" for this camera (its own mic, or a
        paired lav/recorder file), plus the id of that audio so its own sync
        offset is used to place it on the program timeline correctly.

    Returns a dict: program_start, program_end, segments (program-time; a
    segment's track_id of None means nobody has footage at that moment).
    """
    program_start, program_end, starts, ends = compute_program_range(
        camera_ids, offsets, durations, mode
    )

    grid_times = np.arange(program_start, program_end, hop_sec)
    if len(grid_times) < 2:
        grid_times = np.linspace(program_start, program_end, 2)

    energies_by_track = {}
    availability_by_track = {}
    for cid in camera_ids:
        audio, sr, audio_id = energy_audio_by_camera[cid]
        times, rms = frame_rms(audio, sr, frame_sec=frame_sec, hop_sec=hop_sec)
        audio_offset = offsets.get(audio_id, offsets.get(cid, 0.0))
        times_program = times + audio_offset
        rms_smoothed = smooth(rms, taps=3)
        energies_by_track[cid] = resample_to_grid(times_program, rms_smoothed, grid_times)
        availability_by_track[cid] = (grid_times >= starts[cid] - 1e-6) & (grid_times <= ends[cid] + 1e-6)

    segments = detect_active_segments(
        grid_times,
        energies_by_track,
        availability_by_track,
        min_shot_sec=min_shot_sec,
        switch_margin=switch_margin,
    )

    return {
        "program_start": program_start,
        "program_end": program_end,
        "track_starts": starts,
        "track_ends": ends,
        "segments": segments,
        "offsets": dict(offsets),
    }


# ---------------------------------------------------------------------------
# Target format selection
# ---------------------------------------------------------------------------

def pick_target_spec(camera_infos, w_override=None, h_override=None, fps_override=None):
    """Pick a common resolution/fps to normalize all cameras to."""
    vids = [c for c in camera_infos if c.get("width") and c.get("height")]
    if vids:
        res_counts = Counter((c["width"], c["height"]) for c in vids)
        (w, h), _ = res_counts.most_common(1)[0]
        fps_counts = Counter(round(c["fps"], 2) for c in vids if c.get("fps"))
        fps = fps_counts.most_common(1)[0][0] if fps_counts else 30.0
    else:
        w, h, fps = 1280, 720, 30.0
    return (w_override or w), (h_override or h), (fps_override or fps)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

# Encode profiles: "standard" is fast; "youtube" matches YouTube's recommended
# upload settings (better compression, 2s keyframes, BT.709 tags, 384k audio).
ENCODE_PROFILES = {
    "standard": {"preset": "veryfast", "crf": 20, "abr": "192k", "g2": False, "tags709": False},
    "youtube": {"preset": "medium", "crf": 18, "abr": "384k", "g2": True, "tags709": True},
}
_PROFILE = ENCODE_PROFILES["standard"]


def set_encode_profile(name):
    global _PROFILE
    _PROFILE = ENCODE_PROFILES.get(name, ENCODE_PROFILES["standard"])


def video_enc_args(fps, crf_bump=0):
    p = _PROFILE
    args = ["-c:v", "libx264", "-preset", p["preset"], "-crf", str(p["crf"] + crf_bump),
            "-pix_fmt", "yuv420p"]
    if p["g2"]:
        args += ["-g", str(max(1, int(round(float(fps) * 2))))]
    if p["tags709"]:
        args += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    return args


def audio_enc_args(sr=48000):
    return ["-c:a", "aac", "-ar", str(sr), "-ac", "2", "-b:a", _PROFILE["abr"]]


def _scale_pad_vf(w, h, fps):
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps}"
    )


def cut_segment(src_path, source_start, source_dur, out_path, w, h, fps, with_audio, sr=48000,
                vf_extra=None, vf_pre=None):
    ffmpeg = media_utils.ffmpeg_path()
    vf = _scale_pad_vf(w, h, fps)
    if vf_pre:
        vf = f"{vf_pre},{vf}"    # per-camera framing (zoom/pan), applied at full source resolution
    if vf_extra:
        vf = f"{vf},{vf_extra}"  # per-camera color correction, applied after scaling
    cmd = [ffmpeg, "-y", "-nostdin", "-ss", f"{max(0.0, source_start):.3f}", "-i", src_path,
           "-t", f"{max(0.01, source_dur):.3f}",
           "-vf", vf] + video_enc_args(fps)
    if with_audio:
        cmd += audio_enc_args(sr)
    else:
        cmd += ["-an"]
    cmd += [out_path]
    media_utils.run(cmd)


def make_black_segment(duration, out_path, w, h, fps, with_audio, sr=48000):
    ffmpeg = media_utils.ffmpeg_path()
    duration = max(0.05, duration)
    cmd = [ffmpeg, "-y", "-nostdin",
           "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r={fps}:d={duration:.3f}"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={sr}:cl=stereo", "-t", f"{duration:.3f}"]
    cmd += video_enc_args(fps)
    if with_audio:
        cmd += ["-c:a", "aac"]
    else:
        cmd += ["-an"]
    cmd += [out_path]
    media_utils.run(cmd)


def concat_segments(segment_paths, out_path):
    list_file = out_path + ".list.txt"
    with open(list_file, "w") as f:
        for p in segment_paths:
            escaped = os.path.abspath(p).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")
    cmd = [media_utils.ffmpeg_path(), "-y", "-nostdin", "-f", "concat", "-safe", "0",
           "-i", list_file, "-c", "copy", out_path]
    media_utils.run(cmd)
    os.remove(list_file)


def build_mixed_audio(audio_sources, program_start, program_end, out_path, sr=48000):
    """audio_sources: list of (path, offset_seconds). Builds one continuous mixed
    audio bed spanning [program_start, program_end) with each source placed/trimmed
    to its correct position on that timeline.
    """
    inputs = []
    filter_parts = []
    labels = []
    total_dur = program_end - program_start
    for idx, (path, offset) in enumerate(audio_sources):
        delay_sec = offset - program_start
        inputs += ["-i", path]
        label_in = f"[{idx}:a]"
        label_out = f"[a{idx}]"
        if delay_sec >= 0:
            delay_ms = int(round(delay_sec * 1000))
            filt = f"{label_in}adelay={delay_ms}:all=1{label_out}"
        else:
            trim_sec = -delay_sec
            filt = f"{label_in}atrim=start={trim_sec:.3f},asetpts=PTS-STARTPTS{label_out}"
        filter_parts.append(filt)
        labels.append(label_out)

    mix_inputs = "".join(labels)
    # amix keeps levels clip-safe (quiet); loudnorm then brings the mix up to the
    # standard podcast loudness (-16 LUFS) so output volume is consistent.
    filter_parts.append(
        f"{mix_inputs}amix=inputs={len(labels)}:duration=longest:normalize=1,"
        f"loudnorm=I=-16:TP=-1.5:LRA=11[mixed]"
    )
    filter_complex = ";".join(filter_parts)
    cmd = [media_utils.ffmpeg_path(), "-y", "-nostdin"] + inputs + [
        "-filter_complex", filter_complex,
        "-map", "[mixed]", "-t", f"{max(0.05, total_dur):.3f}",
        "-ar", str(sr), "-ac", "2", "-c:a", "aac", "-b:a", _PROFILE["abr"],
        out_path,
    ]
    media_utils.run(cmd)


def mux_video_audio(video_path, audio_path, out_path):
    cmd = [media_utils.ffmpeg_path(), "-y", "-nostdin",
           "-i", video_path, "-i", audio_path,
           "-map", "0:v:0", "-map", "1:a:0",
           "-c:v", "copy", "-c:a", "aac", "-b:a", _PROFILE["abr"],
           "-movflags", "+faststart", "-shortest",
           out_path]
    media_utils.run(cmd)


def faststart_remux(in_path, out_path, with_audio=True):
    cmd = [media_utils.ffmpeg_path(), "-y", "-nostdin", "-i", in_path]
    if with_audio:
        cmd += ["-c", "copy"]
    else:
        cmd += ["-c:v", "copy", "-an"]
    cmd += ["-movflags", "+faststart", out_path]
    media_utils.run(cmd)


def render_program(
    camera_paths,
    edl,
    audio_mode,
    work_dir,
    out_path,
    mixed_audio_sources=None,
    target_w=None,
    target_h=None,
    target_fps=None,
    sr=48000,
    progress_cb=None,
    vf_extra_by_camera=None,
    vf_pre_by_camera=None,
):
    """Render the final video from an EDL.

    camera_paths: {camera_id: original file path}
    edl: dict returned by build_edl()
    audio_mode: "switch" (each cut keeps its own camera's audio) or
        "mixed" (a separate continuous audio bed plays under the video cuts).
    mixed_audio_sources: required if audio_mode == "mixed": list of
        (path, offset_seconds) for every mic that should be in the mix.
    vf_extra_by_camera: optional {camera_id: ffmpeg filter string} applied to
        that camera's segments (color correction / LUTs).
    """
    os.makedirs(work_dir, exist_ok=True)
    segments = [s for s in edl["segments"] if (s["end"] - s["start"]) > 0.02]
    offsets = edl["offsets"]
    n = len(segments)

    if target_w is None or target_h is None or target_fps is None:
        raise ValueError("target_w/target_h/target_fps must be provided by the caller")

    seg_paths = []
    want_audio_per_segment = audio_mode == "switch"
    for i, seg in enumerate(segments):
        dur = seg["end"] - seg["start"]
        seg_out = os.path.join(work_dir, f"seg_{i:05d}.mp4")
        if seg["track_id"] is None:
            make_black_segment(dur, seg_out, target_w, target_h, target_fps, want_audio_per_segment, sr=sr)
        else:
            cid = seg["track_id"]
            src = camera_paths[cid]
            source_start = seg["start"] - offsets.get(cid, 0.0)
            vf_extra = (vf_extra_by_camera or {}).get(cid)
            vf_pre = (vf_pre_by_camera or {}).get(cid)
            cut_segment(src, source_start, dur, seg_out, target_w, target_h, target_fps,
                        want_audio_per_segment, sr=sr, vf_extra=vf_extra, vf_pre=vf_pre)
        seg_paths.append(seg_out)
        if progress_cb:
            progress_cb((i + 1) / max(1, n) * 0.7, f"Cutting clip {i + 1}/{n}")

    if audio_mode == "switch":
        concat_out = os.path.join(work_dir, "concat.mp4")
        concat_segments(seg_paths, concat_out)
        if progress_cb:
            progress_cb(0.9, "Finalizing video")
        faststart_remux(concat_out, out_path, with_audio=True)
    else:
        if not mixed_audio_sources:
            raise ValueError("mixed_audio_sources is required when audio_mode='mixed'")
        video_only = os.path.join(work_dir, "video_only.mp4")
        concat_segments(seg_paths, video_only)
        if progress_cb:
            progress_cb(0.8, "Mixing audio")
        mixed_audio_path = os.path.join(work_dir, "mixed_audio.m4a")
        build_mixed_audio(mixed_audio_sources, edl["program_start"], edl["program_end"], mixed_audio_path, sr=sr)
        if progress_cb:
            progress_cb(0.9, "Finalizing video")
        mux_video_audio(video_only, mixed_audio_path, out_path)

    if progress_cb:
        progress_cb(1.0, "Done")
    return out_path
