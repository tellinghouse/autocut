"""Auto-sync: find the time offset of each track relative to a reference track using
FFT-based audio cross-correlation, so tracks that were started at slightly different
times can be lined up on one common timeline.
"""

import numpy as np


def _next_pow2(n):
    p = 1
    while p < n:
        p *= 2
    return p


def full_convolve(a, b):
    """Linear convolution of 1D arrays a and b via zero-padded FFT (no wraparound)."""
    n = len(a) + len(b) - 1
    nfft = _next_pow2(n)
    A = np.fft.rfft(a, nfft)
    B = np.fft.rfft(b, nfft)
    result = np.fft.irfft(A * B, nfft)
    return result[:n]


def cross_correlate_full(a, b):
    """Equivalent to np.correlate(a, b, mode='full') but computed via FFT.

    Returns (corr, lags) where corr[i] is the correlation at lag lags[i], and
    lags[i] = i - (len(b) - 1). A positive lag L means: a[n + L] lines up with
    b[n], i.e. content that appears in b's file at time t appears in a's file
    at time (t + L) -- b is delayed by L relative to a.
    """
    corr = full_convolve(a, b[::-1])
    lags = np.arange(len(corr)) - (len(b) - 1)
    return corr, lags


def estimate_offset(ref_audio, other_audio, sr, max_offset_sec=600.0, analysis_sec=300.0):
    """Estimate how many seconds `other_audio` is delayed relative to `ref_audio`.

    Both arrays must be mono, sampled at `sr` Hz, starting at each file's own time 0.
    Returns (offset_seconds, confidence) where offset_seconds > 0 means the other
    track's file-time 0 lands *later* on the reference's timeline (it started
    recording after the reference did).
    """
    analysis_n = int(analysis_sec * sr)
    max_offset_n = int(max_offset_sec * sr)

    ref_win = ref_audio[: min(len(ref_audio), analysis_n)]
    other_win = other_audio[: min(len(other_audio), analysis_n + 2 * max_offset_n)]

    if len(ref_win) < sr * 0.5 or len(other_win) < sr * 0.5:
        return 0.0, 0.0

    # A silent (or missing-audio) track can't be correlated: report zero offset
    # with zero confidence instead of picking a meaningless peak.
    if float(np.max(np.abs(ref_win))) < 1e-4 or float(np.max(np.abs(other_win))) < 1e-4:
        return 0.0, 0.0

    # Normalize (zero-mean, unit-ish energy) so confidence is comparable across tracks.
    ref_norm = ref_win - ref_win.mean()
    other_norm = other_win - other_win.mean()

    corr, lags = cross_correlate_full(ref_norm, other_norm)

    mask = np.abs(lags) <= max_offset_n
    if not np.any(mask):
        mask[:] = True
    masked_corr = np.where(mask, corr, -np.inf)

    peak_idx = int(np.argmax(masked_corr))
    peak_val = corr[peak_idx]
    offset_samples = lags[peak_idx]
    offset_seconds = float(offset_samples) / sr

    # Confidence: how far the peak stands out from the noise floor of the correlation.
    finite = corr[mask]
    std = float(np.std(finite)) if len(finite) > 1 else 0.0
    med = float(np.median(finite)) if len(finite) else 0.0
    z = (float(peak_val) - med) / std if std > 1e-9 else 0.0
    confidence = float(np.clip(z / 15.0, 0.0, 1.0))  # heuristic 0..1 scale

    return offset_seconds, confidence


def compute_offsets(audio_by_id, sr, max_offset_sec=600.0, analysis_sec=300.0, reference_id=None):
    """Given {track_id: mono_audio_array} all at sample rate `sr`, compute each track's
    offset (seconds) relative to a chosen (or auto-picked) reference track.

    Returns (reference_id, {track_id: (offset_seconds, confidence)}).
    The reference track itself gets offset 0.0 and confidence 1.0.
    """
    if not audio_by_id:
        return reference_id, {}

    if reference_id is None or reference_id not in audio_by_id:
        reference_id = max(audio_by_id, key=lambda k: len(audio_by_id[k]))

    ref_audio = audio_by_id[reference_id]
    results = {reference_id: (0.0, 1.0)}
    for tid, audio in audio_by_id.items():
        if tid == reference_id:
            continue
        offset, confidence = estimate_offset(
            ref_audio, audio, sr, max_offset_sec=max_offset_sec, analysis_sec=analysis_sec
        )
        results[tid] = (offset, confidence)
    return reference_id, results
