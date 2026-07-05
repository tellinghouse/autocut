"""Active-speaker detection: decide, moment to moment, which camera should be on
screen, based on which track's microphone is currently loudest -- with hysteresis
so the edit doesn't flicker between cameras.
"""

import numpy as np


def frame_rms(audio, sr, frame_sec=0.2, hop_sec=0.05):
    """Short-time RMS energy of a mono signal.

    Returns (times, rms) where times[i] is the center time (seconds) of the i-th
    analysis window and rms[i] is its root-mean-square energy.
    """
    frame_n = max(1, int(frame_sec * sr))
    hop_n = max(1, int(hop_sec * sr))
    n = len(audio)
    if n == 0:
        return np.array([]), np.array([])

    n_frames = max(1, 1 + (n - frame_n) // hop_n) if n >= frame_n else 1
    times = np.empty(n_frames, dtype=np.float64)
    rms = np.empty(n_frames, dtype=np.float64)
    for i in range(n_frames):
        start = i * hop_n
        end = min(start + frame_n, n)
        window = audio[start:end]
        rms[i] = float(np.sqrt(np.mean(np.square(window)))) if len(window) else 0.0
        times[i] = (start + end) / 2.0 / sr
    return times, rms


def smooth(values, taps=3):
    if taps <= 1 or len(values) == 0:
        return values
    kernel = np.ones(taps) / taps
    return np.convolve(values, kernel, mode="same")


def resample_to_grid(times, values, grid_times):
    """Nearest-neighbour resample of a (times, values) energy curve onto grid_times."""
    if len(times) == 0:
        return np.full(len(grid_times), -np.inf)
    idx = np.searchsorted(times, grid_times)
    idx = np.clip(idx, 0, len(times) - 1)
    idx_prev = np.clip(idx - 1, 0, len(times) - 1)
    # pick whichever of idx/idx_prev is actually closer
    use_prev = np.abs(times[idx_prev] - grid_times) <= np.abs(times[idx] - grid_times)
    chosen = np.where(use_prev, idx_prev, idx)
    return values[chosen]


def detect_active_segments(
    grid_times,
    energies_by_track,
    availability_by_track,
    min_shot_sec=2.0,
    switch_margin=1.15,
    silence_percentile=25,
):
    """Decide which track is 'on camera' at every point on a shared time grid.

    grid_times: 1D array of time steps (seconds), evenly spaced.
    energies_by_track: {track_id: array same length as grid_times}
    availability_by_track: {track_id: bool array same length as grid_times}

    Returns a list of segments: [{"start": t0, "end": t1, "track_id": id}, ...]
    covering [grid_times[0], grid_times[-1] + step] contiguously (a track_id of
    None marks a gap where no track has any footage at all).
    """
    track_ids = list(energies_by_track.keys())
    n = len(grid_times)
    if n == 0 or not track_ids:
        return []

    step = grid_times[1] - grid_times[0] if n > 1 else 1.0

    # Adaptive silence floor per track: a low percentile of its own energy while
    # available, so quiet mics and hot mics are each judged against their own
    # noise floor rather than a single global threshold.
    floors = {}
    for tid in track_ids:
        e = energies_by_track[tid]
        avail = availability_by_track[tid]
        active_vals = e[avail] if avail.any() else e
        floors[tid] = float(np.percentile(active_vals, silence_percentile)) if len(active_vals) else 0.0

    def energy_at(tid, i):
        if not availability_by_track[tid][i]:
            return -np.inf
        return energies_by_track[tid][i]

    segments = []
    current = None  # currently active track_id, or None while in a total gap
    seg_start = grid_times[0]
    last_switch_idx = 0
    prev_avail_set = frozenset()

    for i in range(n):
        avail_now = [tid for tid in track_ids if availability_by_track[tid][i]]
        avail_set = frozenset(avail_now)
        newly_available = not avail_set.issubset(prev_avail_set)
        prev_avail_set = avail_set

        if not avail_now:
            desired = None
        elif current is None or current not in avail_now or newly_available:
            # A camera just became available (or nothing was active yet, or the
            # active one dropped out): re-evaluate immediately instead of waiting
            # out the hysteresis timer, so a camera that starts recording mid-show
            # is cut to right away if it's the one talking.
            desired = max(avail_now, key=lambda t: energy_at(t, i))
        else:
            best_tid = max(avail_now, key=lambda t: energy_at(t, i))
            best_e = energy_at(best_tid, i)
            current_e = energy_at(current, i)
            time_since_switch = (i - last_switch_idx) * step
            if (
                time_since_switch >= min_shot_sec
                and best_tid != current
                and best_e > floors.get(best_tid, 0.0)
                and best_e > current_e * switch_margin
            ):
                desired = best_tid
            else:
                desired = current

        if desired != current:
            if i > 0:
                segments.append({"start": seg_start, "end": grid_times[i], "track_id": current})
            seg_start = grid_times[i]
            current = desired
            last_switch_idx = i

    end_time = grid_times[-1] + step
    segments.append({"start": seg_start, "end": end_time, "track_id": current})

    # Merge any accidental zero-length / adjacent same-track segments.
    merged = []
    for seg in segments:
        if seg["end"] <= seg["start"]:
            continue
        if merged and merged[-1]["track_id"] == seg["track_id"]:
            merged[-1]["end"] = seg["end"]
        else:
            merged.append(dict(seg))
    return merged
