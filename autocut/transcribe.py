"""Transcription and chapter generation.

Uses faster-whisper (installed via requirements.txt) to transcribe the finished
episode locally -- nothing is uploaded anywhere. The first transcription
downloads the speech model (one-time, needs internet); after that it's fully
offline. From the transcript we also generate an .srt caption file and a
YouTube-ready chapter list.
"""

import os


def is_available():
    """True if the faster-whisper package is importable."""
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


_MODEL_CACHE = {}

# UI names -> whisper model sizes. "base" is fast and fine for clear podcast
# audio; "small" is noticeably more accurate on tricky audio but ~2-3x slower.
MODEL_CHOICES = {"fast": "base", "accurate": "small"}


def _get_model(size):
    from faster_whisper import WhisperModel
    if size not in _MODEL_CACHE:
        _MODEL_CACHE[size] = WhisperModel(size, device="cpu", compute_type="int8")
    return _MODEL_CACHE[size]


def transcribe_wav(wav_path, model_size="base", duration=None, progress_cb=None):
    """Transcribe a mono WAV. Returns a list of {"start", "end", "text"} segments.

    progress_cb, if given, is called with a 0..1 fraction as segments stream in.
    """
    model = _get_model(model_size)
    segments, info = model.transcribe(wav_path, beam_size=1, vad_filter=True)
    total = duration or float(getattr(info, "duration", 0.0) or 0.0)
    out = []
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        out.append({"start": float(seg.start), "end": float(seg.end), "text": text})
        if progress_cb and total > 0:
            progress_cb(min(1.0, float(seg.end) / total))
    return out


# ---------------------------------------------------------------------------
# Output formats
# ---------------------------------------------------------------------------

def _ts_clock(sec):
    """1:23 or 1:02:03 -- YouTube chapter / transcript style."""
    sec = max(0, int(round(sec)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _ts_srt(sec):
    """00:01:23,456 -- SRT style."""
    sec = max(0.0, float(sec))
    ms = int(round((sec - int(sec)) * 1000))
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_transcript_txt(segments, path):
    """Segments may carry a 'speaker_label'; if so, lines read '[t] Name: text'."""
    with open(path, "w", encoding="utf-8") as f:
        for seg in segments:
            who = seg.get("speaker_label")
            prefix = f"{who}: " if who else ""
            f.write(f"[{_ts_clock(seg['start'])}] {prefix}{seg['text']}\n")
    return path


def write_srt(segments, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            who = seg.get("speaker_label")
            prefix = f"{who}: " if who else ""
            f.write(f"{i}\n{_ts_srt(seg['start'])} --> {_ts_srt(seg['end'])}\n{prefix}{seg['text']}\n\n")
    return path


def _label_from_text(text, max_words=7):
    words = text.replace("\n", " ").split()
    label = " ".join(words[:max_words]).strip(" ,;:-")
    if len(words) > max_words:
        label += "…"
    return label or "Chapter"


def build_chapters(segments, min_gap=1.5, min_spacing_sec=75.0, max_chapters=15):
    """Pick chapter points: the start, plus places where the conversation pauses.

    Returns [(seconds, label)]. Labels are the first few words spoken after the
    pause -- honest, deterministic, and easy to rename by hand.
    """
    if not segments:
        return []
    chapters = [(0.0, "Intro")]
    last_t = 0.0
    prev_end = segments[0]["end"]
    for seg in segments[1:]:
        gap = seg["start"] - prev_end
        if gap >= min_gap and (seg["start"] - last_t) >= min_spacing_sec:
            chapters.append((seg["start"], _label_from_text(seg["text"])))
            last_t = seg["start"]
            if len(chapters) >= max_chapters:
                break
        prev_end = max(prev_end, seg["end"])
    return chapters


def write_chapters_txt(chapters, path):
    """YouTube chapter format: one '0:00 Title' line per chapter."""
    with open(path, "w", encoding="utf-8") as f:
        for t, label in chapters:
            f.write(f"{_ts_clock(t)} {label}\n")
    return path
