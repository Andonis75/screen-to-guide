"""Export bounded consecutive source-frame evidence without rewriting its PTS.

This helper does not identify clicks or infer intent. ``--start``/``--end`` use
the same zero-origin playback timeline as the main guide; source PTS remain
available separately for verification. It never runs a model or network call.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from fractions import Fraction
import json
import math
from pathlib import Path
import re
import sys
import tempfile

from screen_guide import media

MAX_SEQUENCE_SECONDS = 30
MAX_SEQUENCE_FRAMES = 600


def _bounds(start: float, end: float, fps: float) -> None:
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) for value in (start, end, fps)):
        raise media.MediaError("start, end and fps must be finite numbers.")
    if not 0 <= start < end or end - start > MAX_SEQUENCE_SECONDS:
        raise media.MediaError("Use 0 <= start < end and an interval of at most 30 seconds.")
    if not .5 <= fps <= 30:
        raise media.MediaError("fps must be between 0.5 and 30.")
    if math.floor((end - start) * fps + 1e-9) + 1 > MAX_SEQUENCE_FRAMES:
        raise media.MediaError("Requested interval can exceed 600 frames; shorten it or lower fps.")


def _source_points(stderr: bytes) -> tuple[list[tuple[int, Fraction]], str]:
    log = stderr.decode("utf-8", errors="replace")
    base = re.search(r"config in time_base:\s*(\d+)/(\d+)", log)
    if not base or int(base[2]) == 0:
        raise media.MediaError("FFmpeg did not report the source frame time base.")
    ratio = Fraction(int(base[1]), int(base[2]))
    points = [(int(value), int(value) * ratio)
              for value in re.findall(r"\bn:\s*\d+\s+pts:\s*(-?\d+)\s+pts_time:", log)]
    if not points or any(b[0] <= a[0] for a, b in zip(points, points[1:])):
        raise media.MediaError("Missing or non-increasing source PTS.")
    return points, f"{ratio.numerator}/{ratio.denominator}"


def _contact_sheets(frames: list[dict], root: Path) -> list[dict]:
    from PIL import Image, ImageDraw, ImageFont
    font_path = Path("C:/Windows/Fonts/consola.ttf")
    font = ImageFont.truetype(str(font_path), 16) if font_path.is_file() else ImageFont.load_default()
    width, height, label, padding, columns, rows = 480, 270, 26, 8, 4, 3
    sheets = []
    for start in range(0, len(frames), columns * rows):
        group = frames[start:start + columns * rows]
        actual_rows = math.ceil(len(group) / columns)
        sheet = Image.new("RGB", (columns * (width + padding) + padding,
                                  actual_rows * (height + label + padding) + padding), "#edf1f5")
        draw = ImageDraw.Draw(sheet)
        for index, frame in enumerate(group):
            x = padding + index % columns * (width + padding)
            y = padding + index // columns * (height + label + padding)
            with Image.open(root / frame["image"]) as source:
                picture = source.convert("RGB")
                picture.thumbnail((width, height))
                sheet.paste(picture, (x + (width - picture.width) // 2, y))
            draw.text((x, y + height + 3), f"{frame['id']} | {frame['timestamp_seconds']:.6f}s",
                      fill="#172b45", font=font)
        name = f"contact-{len(sheets) + 1:03d}.jpg"
        sheet.save(root / name, quality=88)
        sheets.append({"image": name, "frame_ids": [frame["id"] for frame in group]})
    return sheets


def sample_sequence(source: Path, output_dir: Path, *, start: float, end: float,
                    fps: float = 8, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> dict:
    """Write actual source screenshots, their PTS records and contact sheets."""
    _bounds(start, end, fps)
    source = media._source_path(source)
    output = Path(output_dir).expanduser()
    if output.is_symlink():
        raise media.MediaError("The sequence output must not be a symbolic link.")
    output = output.resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise media.MediaError("Sequence output must be new or empty; existing evidence is never overwritten.")
    metadata = media.probe_media(source, ffprobe)
    duration = media._timeline_duration(metadata)
    if end > duration + 1e-6:
        raise media.MediaError(f"end exceeds the zero-origin source duration ({duration:.6f} seconds).")
    origin = Fraction(str(metadata["start_time_seconds"]))
    absolute_start, absolute_end = float(origin) + start, float(origin) + end
    source_hash = media._digest(source)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".sequence-export-", dir=output) as temporary:
        temporary = Path(temporary)
        # Keep raw source PTS here. Only timestamp_seconds below uses the
        # main guide's start_time-shifted-to-zero convention.
        command = [item for item in media._input(ffmpeg, source, threads=4) if item != "-start_at_zero"]
        interval = max(0, 1 / fps - 1e-9)
        select = (f"select='between(t,{absolute_start:.12g},{absolute_end:.12g})*"
                  f"if(isnan(prev_selected_t),1,gte(t-prev_selected_t,{interval:.12g}))',showinfo")
        command += ["-map", "0:V:0", "-an", "-sn", "-dn", "-vf", select,
                    "-frames:v", str(MAX_SEQUENCE_FRAMES), "-fps_mode", "passthrough", "-q:v", "2",
                    str(temporary / "sequence-%04d.jpg")]
        result = media._run(command, timeout=min(900, max(60, duration * 1.5 + 30)))
        points, time_base = _source_points(result.stderr)
        images = sorted(temporary.glob("sequence-*.jpg"))
        if len(points) != len(images) or len(images) > MAX_SEQUENCE_FRAMES:
            raise media.MediaError("Source PTS and exported screenshots do not match.")
        frames = []
        for ordinal, ((pts, absolute_time), image_path) in enumerate(zip(points, images), 1):
            timestamp = float(absolute_time - origin)
            if not start - 1e-6 <= timestamp <= end + 1e-6 or image_path.stat().st_size == 0:
                raise media.MediaError("Exported frame lies outside the requested interval or is empty.")
            frames.append({"id": f"sequence-{ordinal:04d}", "image": image_path.name,
                           "timestamp_seconds": timestamp, "source_pts": pts,
                           "source_time_base": time_base, "sha256": media._digest(image_path)})
        if media._digest(source) != source_hash:
            raise media.MediaError("Source changed during sequence extraction.")
        sheets = _contact_sheets(frames, temporary)
        (temporary / "extraction.log").write_bytes(result.stderr)
        manifest = {
            "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
            "source": {"name": source.name, "sha256": source_hash, "width": metadata["width"],
                       "height": metadata["height"], "duration_seconds": duration,
                       "source_start_time_seconds": float(origin)},
            "sampling": {"requested_start_seconds": start, "requested_end_seconds": end,
                         "requested_fps": fps, "frame_count": len(frames),
                         "actual_start_seconds": frames[0]["timestamp_seconds"],
                         "actual_end_seconds": frames[-1]["timestamp_seconds"],
                         "timestamp_origin": "source start_time shifted to zero",
                         "method": "select existing source frames; no fps conversion, crop or resize",
                         "note": "Sampling can miss events between frames; cursor position alone does not prove a click."},
            "frames": frames, "contact_sheets": sheets,
        }
        (temporary / "sequence.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                                 encoding="utf-8")
        for item in temporary.iterdir():
            target = output / item.name
            if target.exists() or target.is_symlink():
                raise media.MediaError("Sequence output changed during extraction; refusing overwrite.")
        for item in temporary.iterdir():
            item.replace(output / item.name)
    return manifest


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--end", type=float, required=True)
    parser.add_argument("--fps", type=float, default=8)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args(argv)
    try:
        data = sample_sequence(args.video, args.output_dir, start=args.start, end=args.end, fps=args.fps,
                               ffmpeg=args.ffmpeg, ffprobe=args.ffprobe)
    except (media.MediaError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "manifest": str(args.output_dir.resolve() / "sequence.json"),
                      "frame_count": len(data["frames"]), "contact_sheets": len(data["contact_sheets"])},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
