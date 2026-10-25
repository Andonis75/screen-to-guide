"""Bounded local media evidence; timestamps come from decoded PTS, never FPS."""
from __future__ import annotations

import json
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

MAX_DURATION_SECONDS = 1800
MAX_FILE_BYTES = 2 * 1024**3
MAX_FRAMES = 96
MAX_CANDIDATES = 1200
THUMB_WIDTH, THUMB_HEIGHT = 160, 90


class MediaError(ValueError):
    """The source or local media tool could not produce trustworthy evidence."""


def _source_path(source: Path) -> Path:
    value = os.fspath(source)
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value) and not re.match(r"^[a-zA-Z]:[\\/]", value):
        raise MediaError("Media input must be a local file, not a URL.")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise MediaError(f"Media file does not exist: {path}")
    if not 0 < path.stat().st_size <= MAX_FILE_BYTES:
        raise MediaError("Media must be nonempty and at most 2 GiB.")
    return path


def _run(command: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"Local media processing exceeded {timeout:g} seconds.") from exc
    except OSError as exc:
        raise MediaError(f"Could not start local media tool: {command[0]}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace")[-1600:]
        raise MediaError(f"Local media processing failed: {detail.strip()}")
    return result


def probe_media(source: Path, ffprobe: str) -> dict:
    """Read bounded metadata and reject missing, oversized, or overlong video."""
    source = _source_path(source)
    result = _run([str(ffprobe), "-v", "error", "-protocol_whitelist", "file,pipe",
                   "-show_entries", "format=duration,start_time:stream=codec_type,codec_name,width,height,duration,start_time,pix_fmt,color_transfer:stream_disposition=attached_pic",
                   "-of", "json", str(source)], timeout=30)
    try:
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        video = next(s for s in streams if s.get("codec_type") == "video"
                     and not s.get("disposition", {}).get("attached_pic"))
        durations = [float(s["duration"]) for s in [data.get("format", {}), video]
                     if s.get("duration") not in (None, "N/A")]
        duration = max(durations)
        width, height = int(video["width"]), int(video["height"])
        if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION_SECONDS:
            raise MediaError("Video duration must be between 0 and 1800 seconds.")
        if not 0 < width <= 16384 or not 0 < height <= 16384 or width * height > 40_000_000:
            raise MediaError("Video dimensions exceed the supported decoding bounds.")
    except (KeyError, TypeError, StopIteration, ValueError) as exc:
        if isinstance(exc, MediaError):
            raise
        raise MediaError("Input must contain a video stream with valid dimensions and duration.") from exc
    def number(stream, key, default=None):
        try:
            value = float(stream.get(key, "N/A"))
            return value if math.isfinite(value) else default
        except (TypeError, ValueError):
            return default
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    origin = number(data.get("format", {}), "start_time", 0.0)
    return {"duration_seconds": duration, "width": width, "height": height,
            "has_audio": any(s.get("codec_type") == "audio" for s in streams),
            "video_codec": video.get("codec_name", "unknown"), "audio_codec": audio.get("codec_name"),
            "pixel_format": video.get("pix_fmt"), "color_transfer": video.get("color_transfer"),
            "start_time_seconds": origin, "video_start_seconds": number(video, "start_time", origin),
            "video_duration_seconds": number(video, "duration"),
            "audio_start_seconds": number(audio, "start_time", origin), "audio_duration_seconds": number(audio, "duration")}


def _input(ffmpeg: str, source: Path, *, threads: int | None = None) -> list[str]:
    return [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-nostats", "-copyts", "-start_at_zero",
            "-protocol_whitelist", "file,pipe", "-t", str(MAX_DURATION_SECONDS)] + (
                ["-threads", str(threads), "-filter_threads", "2"] if threads else []) + ["-i", str(source)]


def _timestamps(stderr: bytes) -> list[tuple[int, float]]:
    log = stderr.decode("utf-8", errors="replace")
    base = re.search(r"config in time_base:\s*(\d+)/(\d+)", log)
    if not base or int(base[2]) == 0:
        raise MediaError("FFmpeg did not report the decoded frame time base.")
    numerator, denominator = int(base[1]), int(base[2])
    points = [(int(p), int(p) * numerator / denominator)
              for p in re.findall(r"\bn:\s*\d+\s+pts:\s*(-?\d+)\s+pts_time:", log)]
    if not points or any(not 0 <= t <= MAX_DURATION_SECONDS for _, t in points):
        raise MediaError("Decoded presentation timestamps are missing or out of range.")
    if any(b[1] <= a[1] for a, b in zip(points, points[1:])):
        raise MediaError("Decoded presentation timestamps are not strictly increasing.")
    return points


def _select(points: list[tuple[int, float]], scores: list[float], maximum: int, threshold: float) -> dict[int, str]:
    if maximum == 1 or len(points) == 1:
        return {0: "start"}
    chosen = {0: "start", len(points) - 1: "end"}
    coverage = min(maximum, max(3, math.ceil(maximum / 2)))
    for i in range(1, coverage - 1):
        target = points[0][1] + (points[-1][1] - points[0][1]) * i / (coverage - 1)
        index = min(range(len(points)), key=lambda j: abs(points[j][1] - target))
        chosen.setdefault(index, "interval")
    for index in sorted(range(1, len(points) - 1), key=lambda j: scores[j], reverse=True):
        if len(chosen) >= maximum or scores[index] < threshold:
            break
        chosen.setdefault(index, "change")
    return chosen


def extract_frames(source: Path, output_dir: Path, *, ffmpeg: str, ffprobe: str,
                   max_frames: int = 24, sample_interval: float = 1.0,
                   scene_threshold: float = 0.015) -> list[dict]:
    """Select changes plus temporal coverage, then export exact source frames.

    Analysis uses at most 1200 tiny grayscale frames (about 17 MiB). A second
    pass selects integer presentation timestamps, preserving VFR evidence.
    The last sample is labeled end; its distance from EOF is at most one sample
    interval plus a source-frame gap. Short transitions between samples may be missed.
    """
    if isinstance(max_frames, bool) or not isinstance(max_frames, int) or not 1 <= max_frames <= MAX_FRAMES:
        raise MediaError(f"max_frames must be an integer between 1 and {MAX_FRAMES}.")
    if not math.isfinite(sample_interval) or not 0 < sample_interval <= MAX_DURATION_SECONDS:
        raise MediaError("sample_interval must be a positive finite duration up to 1800 seconds.")
    if not math.isfinite(scene_threshold) or not 0 <= scene_threshold <= 1:
        raise MediaError("scene_threshold must be between 0 and 1.")
    source = _source_path(source)
    metadata = probe_media(source, ffprobe)
    interval = max(sample_interval, metadata["duration_seconds"] / (MAX_CANDIDATES - 1))
    timeout = min(900, max(60, metadata["duration_seconds"] * 1.5 + 30))
    selection = f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{interval:.12g})'"
    result = _run(_input(ffmpeg, source) + ["-map", "0:V:0", "-an", "-sn", "-dn", "-vf",
                  f"{selection},scale={THUMB_WIDTH}:{THUMB_HEIGHT},format=gray,showinfo",
                  "-frames:v", str(MAX_CANDIDATES), "-fps_mode", "passthrough", "-f", "rawvideo", "pipe:1"], timeout)
    points = _timestamps(result.stderr)
    frame_size = THUMB_WIDTH * THUMB_HEIGHT
    if len(points) > MAX_CANDIDATES or len(result.stdout) != len(points) * frame_size:
        raise MediaError("Sample pixels and presentation timestamps do not match.")
    scores = [0.0]
    pixels = memoryview(result.stdout)
    for i in range(1, len(points)):
        previous, current = pixels[(i - 1) * frame_size:i * frame_size], pixels[i * frame_size:(i + 1) * frame_size]
        scores.append(sum(abs(a - b) for a, b in zip(previous, current)) / (255 * frame_size))
    selected = _select(points, scores, max_frames, scene_threshold)
    indices = sorted(selected)
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    frame_dir = destination / "frames"
    frame_dir.mkdir(exist_ok=True)
    # Temporary outputs avoid leaving plausible evidence after a failed export.
    with tempfile.TemporaryDirectory(prefix=".frame-export-", dir=destination) as temporary:
        exact = "+".join(f"eq(pts,{points[index][0]})" for index in indices)
        export = _run(_input(ffmpeg, source) + ["-map", "0:V:0", "-an", "-sn", "-dn", "-vf",
                      f"select='{exact}',scale='min(1920,iw)':-2,showinfo", "-frames:v", str(len(indices)),
                      "-fps_mode", "passthrough", "-q:v", "2", str(Path(temporary) / "frame-%04d.jpg")], timeout)
        exported = _timestamps(export.stderr)
        files = sorted(Path(temporary).glob("frame-*.jpg"))
        expected = [points[index] for index in indices]
        if exported != expected or len(files) != len(indices) or any(p.stat().st_size == 0 for p in files):
            raise MediaError("Exported screenshots do not match selected presentation timestamps.")
        frames = []
        for ordinal, (index, file) in enumerate(zip(indices, files), 1):
            name = f"frame-{ordinal:04d}"
            if source == (frame_dir / f"{name}.jpg").resolve():
                raise MediaError("An output screenshot would overwrite the source.")
            file.replace(frame_dir / f"{name}.jpg")
            frames.append({"id": name, "timestamp_seconds": points[index][1],
                           "image": f"frames/{name}.jpg", "reason": selected[index],
                           "change_score": round(scores[index], 6)})
    return frames


def extract_audio(source: Path, output_dir: Path, *, ffmpeg: str, ffprobe: str | None = None) -> Path | None:
    """Extract at most 30 minutes of local 16 kHz mono PCM; no audio is normal."""
    source = _source_path(source)
    executable = Path(ffmpeg)
    sibling = executable.with_name("ffprobe.exe" if executable.suffix.lower() == ".exe" else "ffprobe")
    metadata = probe_media(source, ffprobe or (str(sibling) if sibling.is_file() else "ffprobe"))
    if not metadata["has_audio"]:
        return None
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / "audio.wav"
    if target.resolve() == source:
        raise MediaError("Audio output would overwrite the source.")
    with tempfile.TemporaryDirectory(prefix=".audio-export-", dir=destination) as temporary:
        wav = Path(temporary) / "audio.wav"
        _run(_input(ffmpeg, source) + ["-loglevel", "error", "-map", "0:a:0", "-vn", "-sn", "-dn",
             "-af", "aresample=async=1:first_pts=0", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
             timeout=min(600, max(60, metadata["duration_seconds"] + 30)))
        if not wav.is_file() or wav.stat().st_size <= 44:
            raise MediaError("Audio extraction did not produce a nonempty PCM WAV.")
        wav.replace(target)
    return target


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _timeline_duration(metadata: dict) -> float:
    origin = metadata["start_time_seconds"]
    streams = ["video", "audio"] if metadata["has_audio"] else ["video"]
    ends = [metadata[kind + "_start_seconds"] + metadata[kind + "_duration_seconds"] - origin
            for kind in streams if metadata.get(kind + "_duration_seconds") is not None]
    return max(ends) if len(ends) == len(streams) else metadata["duration_seconds"] - max(origin, 0)


def ensure_playback_video(source: Path, output_dir: Path, *, ffmpeg: str, ffprobe: str,
                          metadata: dict | None = None) -> dict:
    """Package a browser playback copy without changing source evidence.

    Returns {video: relative path | None, metadata: dict, warnings: list[str]}.
    H.264/yuv420p + AAC MP4 is copied byte-for-byte. Other inputs become a
    lossy, at-most-1080p H.264/AAC playback.mp4; HDR is mapped to SDR. The same
    copyts/start_at_zero origin as screenshot extraction preserves VFR PTS.
    Existing identical source copies can be reused; derived files are never
    overwritten. Conversion failures attach the source with explicit warnings.
    """
    source = _source_path(source)
    required = {"audio_codec", "pixel_format", "color_transfer", "start_time_seconds", "video_start_seconds",
                "video_duration_seconds", "audio_start_seconds", "audio_duration_seconds"}
    metadata = metadata if metadata and required <= metadata.keys() else probe_media(source, ffprobe)
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    source_hash = _digest(source)
    hdr = metadata["color_transfer"] in {"smpte2084", "arib-std-b67"}
    compatible = (source.suffix.lower() in {".mp4", ".m4v"} and metadata["video_codec"] == "h264"
                  and metadata["pixel_format"] == "yuv420p" and not hdr
                  and (not metadata["has_audio"] or metadata["audio_codec"] == "aac"))
    warnings = []
    details = {"derived_copy": False, "lossy": False, "source_sha256": source_hash,
               "timestamp_origin": "source start_time shifted to zero", "source_start_time_seconds": metadata["start_time_seconds"]}

    def attach_source(failed=False):
        name = "source" + source.suffix.lower()
        target = destination / name
        if target.is_symlink() or target.resolve() == source or not target.resolve().is_relative_to(destination):
            raise MediaError("Playback destination would overwrite or escape the source directory.")
        if target.exists():
            if not target.is_file() or _digest(target) != source_hash:
                raise MediaError("An unrelated source playback file already exists.")
        else:
            with tempfile.TemporaryDirectory(prefix=".playback-copy-", dir=destination) as temporary:
                copy = Path(temporary) / name
                shutil.copyfile(source, copy)
                if _digest(copy) != source_hash:
                    raise MediaError("Source changed while creating its playback copy.")
                copy.replace(target)
        supported = source.suffix.lower() in {".mp4", ".m4v", ".mov", ".webm", ".ogg"}
        return {"video": name if supported else None, "warnings": warnings,
                "metadata": {**details, "status": "source_fallback" if failed else "source_copy",
                             "source_attachment": name, "playback_sha256": source_hash,
                             "video_codec": metadata["video_codec"], "audio_codec": metadata["audio_codec"],
                             "duration_seconds": _timeline_duration(metadata)}}

    if compatible:
        return attach_source()
    target = destination / "playback.mp4"
    if target.exists() or target.is_symlink() or target.resolve() == source:
        raise MediaError("playback.mp4 already exists; use a fresh output directory.")
    try:
        with tempfile.TemporaryDirectory(prefix=".playback-transcode-", dir=destination) as temporary:
            encoded = Path(temporary) / "playback.mp4"
            filters = "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
            if hdr:
                filters += ",zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv"
            filters += ",format=yuv420p"
            command = _input(ffmpeg, source, threads=4) + ["-loglevel", "error", "-map", "0:V:0", "-map", "0:a:0?",
                       "-sn", "-dn", "-map_metadata", "-1", "-vf", filters, "-c:v", "libx264", "-preset", "veryfast",
                       "-crf", "20", "-bf", "0", "-threads:v", "4", "-pix_fmt", "yuv420p", "-fps_mode", "passthrough",
                       "-enc_time_base:v", "demux", "-avoid_negative_ts", "disabled", "-c:a", "aac", "-threads:a", "1",
                       "-af", "aresample=async=1:first_pts=0", "-ar", "48000", "-ac", "2", "-b:a", "128k", "-movflags", "+faststart"]
            if hdr:
                command += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
            _run(command + [str(encoded)], timeout=min(3600, max(120, metadata["duration_seconds"] * 8)))
            playback = probe_media(encoded, ffprobe)
            duration_delta = abs(_timeline_duration(playback) - _timeline_duration(metadata))
            start_delta = abs((playback["video_start_seconds"] - playback["start_time_seconds"])
                              - (metadata["video_start_seconds"] - metadata["start_time_seconds"]))
            if (playback["video_codec"] != "h264" or playback["pixel_format"] != "yuv420p"
                    or playback["has_audio"] != metadata["has_audio"] or (playback["has_audio"] and playback["audio_codec"] != "aac")
                    or duration_delta > .5 or start_delta > .05):
                raise MediaError("Playback conversion failed codec, duration, or timestamp alignment validation.")
            if _digest(source) != source_hash:
                raise MediaError("Source changed during playback conversion.")
            output_hash = _digest(encoded)
            encoded.replace(target)
        warnings.append("播放视频为本地生成的有损 H.264/AAC 兼容副本；原始截图与源视频保持独立。")
        if hdr:
            warnings.append("播放副本已将 HDR 映射为 SDR，颜色与原视频可能存在差异。")
        return {"video": "playback.mp4", "warnings": warnings,
                "metadata": {**details, "status": "transcoded", "derived_copy": True, "lossy": True,
                             "hdr_to_sdr": hdr, "playback_sha256": output_hash, "video_codec": playback["video_codec"],
                             "audio_codec": playback["audio_codec"], "pixel_format": playback["pixel_format"],
                             "width": playback["width"], "height": playback["height"], "duration_seconds": _timeline_duration(playback),
                             "duration_delta_seconds": round(duration_delta, 6), "first_video_pts_delta_seconds": round(start_delta, 6),
                             "encoder_threads": 4}}
    except (MediaError, OSError) as exc:
        warnings.append("兼容播放副本生成失败，已附源文件；浏览器可能仍需其他播放器。" + str(exc)[-500:])
        return attach_source(failed=True)
