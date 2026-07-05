"""Post-production steps that run on the finished render:

- tighten_pauses: find stretches of dead air and jump-cut them out
  (the classic talking-head edit).
- pick_clip_windows / render_social_clips: find the highest-energy moments and
  cut them as vertical (9:16) shorts plus a widescreen copy of each.

Everything works on the final rendered file, so timestamps in transcripts,
chapters, and clips always match what the viewer sees.
"""

import os

import numpy as np

from . import media_utils
from .active_speaker import frame_rms, smooth
from .editor import concat_segments, faststart_remux, video_enc_args, audio_enc_args


# ---------------------------------------------------------------------------
# Shared: energy curve of a finished file
# ---------------------------------------------------------------------------

def analyze_media_rms(media_path, work_dir, tag="post", sr=16000, frame_sec=0.2, hop_sec=0.05):
    """Extract audio and return (times, smoothed_rms, duration_sec, wav_path)."""
    os.makedirs(work_dir, exist_ok=True)
    wav = os.path.join(work_dir, f"{tag}.analysis.wav")
    media_utils.extract_audio_wav(media_path, wav, sr=sr)
    audio, sr2 = media_utils.read_wav_mono(wav)
    duration = len(audio) / float(sr2)
    times, rms = frame_rms(audio, sr2, frame_sec=frame_sec, hop_sec=hop_sec)
    return times, smooth(rms, taps=5), duration, wav


# ---------------------------------------------------------------------------
# Pause tightening (jump cuts)
# ---------------------------------------------------------------------------

def find_pause_cuts(times, rms, duration, min_pause=2.0, keep_pause=0.5, thr=None):
    """Return ([(cut_start, cut_end)], threshold): ranges of dead air to remove.

    A pause must last at least `min_pause` seconds to be cut, and `keep_pause`
    seconds of breathing room are left behind (half on each side) so the edit
    doesn't feel robotic.
    """
    if len(times) == 0:
        return [], 0.0
    if thr is None:
        active = rms[rms > 0]
        thr = max(1e-3, float(np.percentile(active, 20)) * 0.8) if len(active) else 1e-3
    silent = rms < thr

    cuts = []
    i, n = 0, len(times)
    while i < n:
        if not silent[i]:
            i += 1
            continue
        j = i
        while j < n and silent[j]:
            j += 1
        s = 0.0 if i == 0 else float(times[i])
        e = duration if j >= n else float(times[j - 1])
        if (e - s) >= min_pause:
            pad = keep_pause / 2.0
            cs, ce = s + pad, e - pad
            if ce - cs > 0.05:
                cuts.append((cs, ce))
        i = j
    return cuts, float(thr)


def spans_after_cuts(cuts, duration, min_keep=0.25):
    """Complement of the cut list over [0, duration): the spans to keep."""
    spans = []
    pos = 0.0
    for s, e in cuts:
        if s - pos >= min_keep:
            spans.append((pos, s))
        pos = max(pos, e)
    if duration - pos >= min_keep:
        spans.append((pos, duration))
    return spans


def _encode_span(src, start, dur, out_path, fps, has_audio):
    cmd = [media_utils.ffmpeg_path(), "-y", "-nostdin",
           "-ss", f"{max(0.0, start):.3f}", "-i", src, "-t", f"{max(0.05, dur):.3f}",
           "-vf", f"fps={fps},setsar=1"] + video_enc_args(fps)
    if has_audio:
        cmd += audio_enc_args()
    else:
        cmd += ["-an"]
    cmd += [out_path]
    media_utils.run(cmd)


def cut_video_spans(src, keep_spans, out_path, work_dir, progress_cb=None):
    """Re-cut `src` keeping only `keep_spans` (list of (start, end) in seconds),
    concatenated in order. Returns the new duration."""
    os.makedirs(work_dir, exist_ok=True)
    info = media_utils.probe(src)
    fps = info["fps"] or 30.0
    seg_paths = []
    for i, (s, e) in enumerate(keep_spans):
        p = os.path.join(work_dir, f"keep_{i:05d}.mp4")
        _encode_span(src, s, e - s, p, fps, info["has_audio"])
        seg_paths.append(p)
        if progress_cb:
            progress_cb((i + 1) / max(1, len(keep_spans)))
    joined = os.path.join(work_dir, "spans_join.mp4")
    concat_segments(seg_paths, joined)
    faststart_remux(joined, out_path, with_audio=info["has_audio"])
    return sum(e - s for s, e in keep_spans)


def time_after_cuts(cut_ranges, t):
    """Map a time in the ORIGINAL file to its position after `cut_ranges` are
    removed. (cut_ranges must be sorted and non-overlapping.)"""
    removed = 0.0
    for s, e in cut_ranges:
        if s >= t:
            break
        removed += min(e, t) - s
    return max(0.0, t - removed)


def time_before_cuts(keep_spans, t):
    """Map a time in the CUT file back to the original file's clock, given the
    keep_spans that produced it."""
    pos = 0.0
    for s, e in keep_spans:
        span_len = e - s
        if t <= pos + span_len:
            return s + (t - pos)
        pos += span_len
    return keep_spans[-1][1] if keep_spans else t


def tighten_pauses(src, out_path, work_dir, min_pause=2.0, keep_pause=0.5, progress_cb=None):
    """Remove long pauses from a finished video. Returns (seconds_removed,
    keep_spans), or None if there was nothing worth cutting (no file written)."""
    os.makedirs(work_dir, exist_ok=True)
    times, rms, duration, _ = analyze_media_rms(src, work_dir, tag="tighten")
    cuts, _thr = find_pause_cuts(times, rms, duration, min_pause=min_pause, keep_pause=keep_pause)
    if not cuts:
        return None
    spans = spans_after_cuts(cuts, duration)
    if not spans or len(spans) == 1 and abs((spans[0][1] - spans[0][0]) - duration) < 0.1:
        return None
    kept = cut_video_spans(src, spans, out_path, work_dir, progress_cb=progress_cb)
    return max(0.0, duration - kept), spans


# ---------------------------------------------------------------------------
# Social clip finder
# ---------------------------------------------------------------------------

def pick_clip_windows(times, rms, duration, n_clips=3, clip_len=45.0):
    """Pick the `n_clips` highest-energy non-overlapping windows of ~clip_len
    seconds, with boundaries snapped to nearby quiet moments for clean cuts."""
    if n_clips <= 0 or len(times) < 4 or duration < 6.0:
        return []
    clip_len = float(min(clip_len, max(6.0, duration * 0.8)))
    if duration <= clip_len + 1.0:
        return [(0.0, duration)]

    step = float(times[1] - times[0]) if len(times) > 1 else 0.05
    wn = max(1, int(clip_len / step))
    if wn >= len(rms):
        return [(0.0, duration)]

    csum = np.cumsum(np.insert(rms.astype(np.float64), 0, 0.0))
    scores = csum[wn:] - csum[:-wn]          # scores[i] = energy of window starting at times[i]
    order = np.argsort(scores)[::-1]

    margin_n = int(5.0 / step)
    chosen = []
    for idx in order:
        if len(chosen) >= n_clips:
            break
        if all(abs(int(idx) - c) > wn + margin_n for c in chosen):
            chosen.append(int(idx))

    def snap(t, lo, hi):
        i0 = max(0, int(lo / step))
        i1 = min(len(rms) - 1, int(hi / step))
        if i1 <= i0:
            return t
        k = i0 + int(np.argmin(rms[i0:i1 + 1]))
        return float(times[k])

    windows = []
    for c in sorted(chosen):
        s = float(times[c])
        e = min(duration, s + clip_len)
        s2 = snap(s, s - 1.5, s + 0.75)
        e2 = snap(e, e - 0.75, e + 1.5)
        if e2 - s2 < 4.0:
            s2, e2 = s, e
        windows.append((max(0.0, s2), min(duration, e2)))
    return windows


def render_social_clips(src, windows, out_base, progress_cb=None):
    """Render each window as a vertical 9:16 short (center crop) plus a
    widescreen copy. Returns a list of (path, kind) tuples."""
    info = media_utils.probe(src)
    fps = info["fps"] or 30.0
    w, h = info["width"], info["height"]
    landscape = w > 0 and h > 0 and (w / h) > (9.0 / 16.0)
    results = []
    total = max(1, len(windows))
    for i, (s, e) in enumerate(windows, 1):
        dur = e - s
        common = [media_utils.ffmpeg_path(), "-y", "-nostdin",
                  "-ss", f"{max(0.0, s):.3f}", "-i", src, "-t", f"{max(0.5, dur):.3f}"]
        tail = video_enc_args(fps, crf_bump=1) + audio_enc_args() + ["-movflags", "+faststart"]

        vert = f"{out_base}_short{i}.mp4"
        if landscape:
            vf = "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920,setsar=1," + f"fps={fps}"
        else:
            vf = f"scale=1080:-2,setsar=1,fps={fps}"
        media_utils.run(common + ["-vf", vf] + tail + [vert])
        results.append((vert, "clip"))

        wide = f"{out_base}_short{i}_wide.mp4"
        media_utils.run(common + ["-vf", f"setsar=1,fps={fps}"] + tail + [wide])
        results.append((wide, "clip"))

        if progress_cb:
            progress_cb(i / total)
    return results
