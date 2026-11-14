#!/usr/bin/env python3
"""Find review candidates by combining physical scene cuts with timeline boundaries.

Read-only for media. Exit 0 = scan completed, NOT visual acceptance.
Requires Python 3 standard library, FFmpeg and FFprobe. Never deletes shots.
"""
import argparse
from fractions import Fraction
import json
import math
from pathlib import Path
import re
import subprocess
import sys


def run(args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr[-4000:] or "Media command failed")
    return result


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def analyze_boundaries(scene_times, duration, fps, shots, timeline_fps, minimum):
    """Quantize into the export frame clock; labels retain the origin of each cut."""
    points = {}

    def add(seconds, kind):
        frame = max(0, min(round(duration * fps), round(seconds * fps)))
        points.setdefault(frame, set()).add(kind)

    add(0, "file-start")
    add(duration, "file-end")
    for time in scene_times:
        if 0 < time < duration:
            add(time, "physical-scene")
    for shot in shots:
        if not isinstance(shot, dict):
            raise ValueError("Each timeline shot must be an object")
        a, b = float(shot["from"]), float(shot["to"])
        if not (math.isfinite(a) and math.isfinite(b) and 0 <= a < b):
            raise ValueError("Timeline shot range must satisfy 0 <= from < to")
        if b / timeline_fps > duration + 1 / timeline_fps:
            raise ValueError("Timeline shot exceeds video duration; check baseline/fps")
        add(a / timeline_fps, "timeline-start")
        add(b / timeline_fps, "timeline-end")
    ordered = sorted(points)
    candidates = []
    for a, b in zip(ordered, ordered[1:]):
        seconds = (b - a) / fps
        if 0 < seconds < minimum:
            candidates.append({
                "from_frame": a, "to_frame_exclusive": b,
                "from_seconds": a / fps, "to_seconds": b / fps,
                "duration_seconds": seconds, "frames": b - a,
                "start_boundary": sorted(points[a]),
                "end_boundary": sorted(points[b]),
                "status": "requires_visual_review",
            })
    return candidates


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("video", type=Path)
    ap.add_argument("--timeline", type=Path)
    ap.add_argument("--report", required=True, type=Path)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--ffprobe", default="ffprobe")
    ap.add_argument("--expected-fps", type=positive, default=60)
    ap.add_argument("--scene-threshold", type=positive, default=0.28)
    ap.add_argument("--min-shot-seconds", type=positive, default=0.55)
    ap.add_argument("--focus", type=float, action="append", default=[])
    ap.add_argument("--context", type=positive, default=0.35)
    ap.add_argument("--review-dir", type=Path)
    ap.add_argument("--fail-on-candidates", action="store_true")
    args = ap.parse_args()
    video = args.video.resolve(strict=True)
    if args.scene_threshold >= 1:
        ap.error("--scene-threshold must be below 1")
    if args.context > 3:
        ap.error("--context may not exceed 3 seconds per contact sheet")
    if args.focus and not args.review_dir:
        ap.error("--focus requires --review-dir")
    protected = {video}
    if args.timeline:
        protected.add(args.timeline.resolve(strict=True))
    if args.report.resolve() in protected:
        ap.error("Report must not overwrite an input")

    probe = json.loads(run([args.ffprobe, "-v", "error", "-show_streams",
                            "-show_format", "-of", "json", str(video)]).stdout)
    stream = next((s for s in probe["streams"] if s["codec_type"] == "video"), None)
    if stream is None:
        raise ValueError("No video stream")
    fps = float(Fraction(stream["avg_frame_rate"]))
    nominal = float(Fraction(stream["r_frame_rate"]))
    if fps <= 0:
        raise ValueError("Invalid frame rate")
    duration = float(stream.get("duration") or probe["format"]["duration"])
    frames = int(stream.get("nb_frames") or round(duration * fps))
    warnings = []
    if abs(fps - args.expected_fps) > 0.01:
        warnings.append("Average fps differs from the requested export fps")
    if abs(fps - nominal) > 0.01:
        warnings.append("Possible VFR: inspect PTS; reported frame indexes are approximate")
    warnings.append("CFR/PTS continuity, freeze detection, audio and perceptual quality are separate checks")
    shots, timeline_fps = [], fps
    if args.timeline:
        timeline = json.loads(args.timeline.read_text(encoding="utf-8"))
        if not isinstance(timeline, dict) or "fps" not in timeline or "shots" not in timeline:
            raise ValueError("Timeline needs {fps, shots: [{from, to, ...}]}")
        shots, timeline_fps = timeline["shots"], float(timeline["fps"])
        if not isinstance(shots, list) or not math.isfinite(timeline_fps) or timeline_fps <= 0:
            raise ValueError("Invalid timeline fps or shots")
    else:
        warnings.append("No timeline: soft transitions may hide short leading/trailing source shots")

    vf = f"setpts=PTS-STARTPTS,scale=320:180,select='gt(scene,{args.scene_threshold})',showinfo"
    result = run([args.ffmpeg, "-hide_banner", "-nostdin", "-i", str(video),
                  "-an", "-vf", vf, "-fps_mode", "vfr", "-f", "null", "-"])
    cuts = sorted(set(float(t) for t in re.findall(r"pts_time:([\d.+eE-]+)", result.stderr)))
    candidates = analyze_boundaries(cuts, duration, fps, shots, timeline_fps, args.min_shot_seconds)
    review = []
    for index, focus in enumerate(args.focus):
        if not math.isfinite(focus) or not 0 <= focus < duration:
            raise ValueError("Focus time must be inside the video")
        a = max(0, math.floor((focus - args.context) * fps))
        b = min(frames - 1, math.ceil((focus + args.context) * fps))
        count = b - a + 1
        rows = math.ceil(count / 5)
        args.review_dir.mkdir(parents=True, exist_ok=True)
        dest = args.review_dir / f"focus-{index+1:02d}-{focus:.3f}s.jpg"
        filt = f"select='between(n,{a},{b})',scale=384:216,tile=5x{rows}:padding=2:margin=2:color=black"
        run([args.ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(video),
             "-an", "-vf", filt, "-frames:v", "1", "-q:v", "2", str(dest)])
        review.append({"focus_seconds": focus, "first_frame": a, "last_frame": b,
                       "count": count, "columns": 5, "order": "left-to-right, then top-to-bottom",
                       "seconds_per_frame": 1 / fps, "file": str(dest.resolve()),
                       "viewed": False})
    report = {
        "video": str(video), "width": stream["width"], "height": stream["height"],
        "fps": fps, "nominal_fps": nominal, "duration_seconds": duration, "frames": frames,
        "scene_threshold": args.scene_threshold, "min_shot_seconds": args.min_shot_seconds,
        "physical_scene_cuts_seconds": cuts, "timeline_provided": bool(args.timeline),
        "short_shot_candidates": candidates, "review_sheets": review, "warnings": warnings,
        "scan_completed": True, "visual_qa_status": "not_performed_by_script",
        "accepted": False,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(args.report.resolve()), "candidates": len(candidates),
                      "visual_qa": "required", "review_sheets": len(review)}))
    return 2 if args.fail_on_candidates and candidates else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, RuntimeError, ZeroDivisionError) as error:
        print(f"Audit failed: {error}", file=sys.stderr)
        sys.exit(1)
