"""Export an EDL as an FCPXML project: an editable timeline for Premiere Pro,
DaVinci Resolve, or Final Cut that a human can open and fine-tune.

Design: the primary storyline holds the auto-cut sequence (exactly what the
rendered video shows). Every *other* camera's full, synced footage is attached
as a connected "lane" above the very first clip, and every separate mic/recorder
file as a lane below it -- so nothing is thrown away and the editor can
reveal/swap angles or grab the clean audio by hand.

This intentionally sticks to the plain, well-documented FCPXML building blocks
(resources/asset/format, spine/asset-clip/gap, lane) rather than the
multicam-clip schema, since there's no way to test-import into real NLE
software from this environment -- simpler XML means fewer ways to fail.
"""

import math
import os
import xml.etree.ElementTree as ET
from xml.dom import minidom

_FPS_CANDIDATES = [
    (1001, 24000, 23.976),
    (1, 24, 24.0),
    (1, 25, 25.0),
    (1001, 30000, 29.97),
    (1, 30, 30.0),
    (1, 50, 50.0),
    (1001, 60000, 59.94),
    (1, 60, 60.0),
]


def pick_frame_duration(fps):
    """Return (num, den) such that one frame lasts num/den seconds."""
    if not fps or fps <= 0:
        fps = 30.0
    dnum, dden, _ = min(_FPS_CANDIDATES, key=lambda c: abs(c[2] - fps))
    return dnum, dden


def fmt_time(seconds, frame_duration):
    """Format a time value as a frame-accurate FCPXML rational, e.g. '12345/30000s'."""
    dnum, dden = frame_duration
    seconds = max(0.0, seconds)
    frames = int(round(seconds * dden / dnum))
    num = frames * dnum
    den = dden
    g = math.gcd(num, den) if num else 0
    if g > 1:
        num //= g
        den //= g
    if num == 0:
        return "0s"
    return f"{num}/{den}s"


def _path_to_uri(path):
    abspath = os.path.abspath(path)
    # file:// URIs want forward slashes even on Windows-authored paths.
    abspath = abspath.replace("\\", "/")
    if not abspath.startswith("/"):
        abspath = "/" + abspath
    from urllib.parse import quote
    return "file://" + quote(abspath)


def _clip_window(place_at_local, source_len, program_len):
    """Fit a full-length source clip into the program window.

    place_at_local: where the clip's own t=0 would land, relative to the start
    of the program (may be negative if the clip started before the program).
    Returns (local_offset, source_start, duration), or None if the clip lies
    entirely outside the program.
    """
    source_start = 0.0
    local = place_at_local
    dur = source_len
    if local < 0:
        source_start = -local   # trim the head that predates the program
        dur += local
        local = 0.0
    if local >= program_len:
        return None
    dur = min(dur, program_len - local)
    if dur <= 0.001:
        return None
    return local, source_start, dur


def export_fcpxml(
    camera_paths,
    camera_specs,
    edl,
    out_path,
    audio_paths=None,
    audio_specs=None,
    event_name="AutoCut Event",
    project_name="AutoCut Sequence",
):
    """Write an FCPXML project file.

    camera_paths: {camera_id: absolute file path}
    camera_specs: {camera_id: {"width":.., "height":.., "fps":.., "duration":..}}
    edl: dict returned by editor.build_edl()
    audio_paths/audio_specs: optional separate mic/recorder files, attached as
        synced audio lanes below the storyline.
    """
    audio_paths = audio_paths or {}
    audio_specs = audio_specs or {}

    segments = [s for s in edl["segments"] if (s["end"] - s["start"]) > 0.001]
    offsets = edl["offsets"]
    program_start = edl["program_start"]
    program_len = edl["program_end"] - edl["program_start"]

    # Pick one sequence-wide format from whichever camera has the most on-screen time.
    screen_time = {}
    for s in segments:
        if s["track_id"] is not None:
            screen_time[s["track_id"]] = screen_time.get(s["track_id"], 0.0) + (s["end"] - s["start"])
    main_cam = max(screen_time, key=screen_time.get) if screen_time else next(iter(camera_specs))
    seq_fps = camera_specs[main_cam].get("fps") or 30.0
    seq_w = camera_specs[main_cam].get("width") or 1280
    seq_h = camera_specs[main_cam].get("height") or 720
    frame_dur = pick_frame_duration(seq_fps)

    fcpxml = ET.Element("fcpxml", version="1.9")
    resources = ET.SubElement(fcpxml, "resources")
    fmt_id = "fmt1"
    ET.SubElement(
        resources, "format",
        id=fmt_id,
        name=f"FFVideoFormat{seq_h}p{round(seq_fps)}",
        frameDuration=f"{frame_dur[0]}/{frame_dur[1]}s",
        width=str(seq_w), height=str(seq_h),
    )

    asset_ids = {}
    for i, (cid, path) in enumerate(camera_paths.items()):
        spec = camera_specs.get(cid, {})
        aid = f"asset{i+1}"
        asset_ids[cid] = aid
        dur = spec.get("duration", 0.0)
        ET.SubElement(
            resources, "asset",
            id=aid, name=os.path.basename(path),
            src=_path_to_uri(path),
            start="0s",
            duration=fmt_time(dur, frame_dur),
            hasVideo="1" if spec.get("has_video", True) else "0",
            hasAudio="1" if spec.get("has_audio", True) else "0",
            format=fmt_id,
        )
    for j, (aid_name, path) in enumerate(audio_paths.items()):
        spec = audio_specs.get(aid_name, {})
        aid = f"audio{j+1}"
        asset_ids[aid_name] = aid
        ET.SubElement(
            resources, "asset",
            id=aid, name=os.path.basename(path),
            src=_path_to_uri(path),
            start="0s",
            duration=fmt_time(spec.get("duration", 0.0), frame_dur),
            hasVideo="0",
            hasAudio="1",
        )

    library = ET.SubElement(fcpxml, "library")
    event = ET.SubElement(library, "event", name=event_name)
    project = ET.SubElement(event, "project", name=project_name)
    sequence = ET.SubElement(
        project, "sequence",
        format=fmt_id,
        duration=fmt_time(program_len, frame_dur),
        tcStart="0s", tcFormat="NDF",
    )
    spine = ET.SubElement(sequence, "spine")

    first_element_written = False
    for seg in segments:
        local_offset = seg["start"] - program_start
        dur = seg["end"] - seg["start"]
        if seg["track_id"] is None:
            el = ET.SubElement(
                spine, "gap",
                name="No camera available",
                offset=fmt_time(local_offset, frame_dur),
                duration=fmt_time(dur, frame_dur),
                start="0s",
            )
        else:
            cid = seg["track_id"]
            source_start = seg["start"] - offsets.get(cid, 0.0)
            el = ET.SubElement(
                spine, "asset-clip",
                ref=asset_ids[cid],
                name=os.path.basename(camera_paths[cid]),
                offset=fmt_time(local_offset, frame_dur),
                start=fmt_time(source_start, frame_dur),
                duration=fmt_time(dur, frame_dur),
                format=fmt_id,
            )

        if not first_element_written:
            first_element_written = True
            # Attach every *other* camera's full footage as connected lanes above
            # the first spine item (and each separate mic file as an audio lane
            # below it), trimmed so everything stays in sync with the program.
            lane = 1
            for cid, path in camera_paths.items():
                if seg["track_id"] is not None and cid == seg["track_id"]:
                    continue
                spec = camera_specs.get(cid, {})
                window = _clip_window(
                    offsets.get(cid, 0.0) - program_start,
                    spec.get("duration", 0.0),
                    program_len,
                )
                if window is None:
                    continue
                w_local, w_src_start, w_dur = window
                ET.SubElement(
                    el, "asset-clip",
                    ref=asset_ids[cid],
                    lane=str(lane),
                    name=f"{os.path.basename(path)} (full, not cut)",
                    offset=fmt_time(w_local, frame_dur),
                    start=fmt_time(w_src_start, frame_dur),
                    duration=fmt_time(w_dur, frame_dur),
                    format=fmt_id,
                )
                lane += 1
            audio_lane = -1
            for aid_name, path in audio_paths.items():
                spec = audio_specs.get(aid_name, {})
                window = _clip_window(
                    offsets.get(aid_name, 0.0) - program_start,
                    spec.get("duration", 0.0),
                    program_len,
                )
                if window is None:
                    continue
                w_local, w_src_start, w_dur = window
                ET.SubElement(
                    el, "asset-clip",
                    ref=asset_ids[aid_name],
                    lane=str(audio_lane),
                    name=f"{os.path.basename(path)} (mic)",
                    offset=fmt_time(w_local, frame_dur),
                    start=fmt_time(w_src_start, frame_dur),
                    duration=fmt_time(w_dur, frame_dur),
                )
                audio_lane -= 1

    rough = ET.tostring(fcpxml, encoding="unicode")
    pretty = minidom.parseString(rough).toprettyxml(indent="    ")
    # minidom adds its own XML declaration; replace it and add the FCPXML doctype.
    body = pretty.split("?>", 1)[1].lstrip()
    final_xml = '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n' + body

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(final_xml)
    return out_path
