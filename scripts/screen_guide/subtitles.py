"""Read existing timed captions as evidence, without network or model calls.

The coverage gate is a heuristic, not a completeness/accuracy guarantee. It
checks the beginning/end and rejects almost empty tracks; internal silent gaps
are reported but do not by themselves reject a valid caption track.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
from pathlib import Path
import re

MAX_BYTES = 16 * 1024 * 1024
MAX_SEGMENTS = 50000
_TIMING = re.compile(r"^\s*(\d+:\d{2}(?::\d{2})?[.,]\d{3})\s+-->\s+(\d+:\d{2}(?::\d{2})?[.,]\d{3})(?:\s+.*)?$")
_TAGS = re.compile(r"</?(?:b|i|u|s|a|font|c|v|lang|ruby|rt|span|div|p|br|em|strong|script|style|code|pre|small|sub|sup|mark|ul|ol|li|table|tr|td|th|thead|tbody|h[1-6])(?=[\s.>/])[^>]*>|<\d+:\d{2}(?::\d{2})?\.\d{3}>", re.I)


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"字幕 {name} 必须是有限数值。")
    return float(value)


def _clean(text):
    if not isinstance(text, str):
        raise ValueError("字幕文本必须是字符串。")
    # Preserve literal code placeholders such as <TOKEN>; remove caption and
    # common HTML formatting tags, not arbitrary angle-bracketed words.
    text = re.sub(r"</?(?:div|p|br|li|tr|h[1-6])(?=[\s>/])[^>]*>", " ", text, flags=re.I)
    text = _TAGS.sub("", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _time(value):
    parts = value.replace(",", ".").split(":")
    try:
        seconds = float(parts[-1])
        minutes = int(parts[-2])
        hours = int(parts[0]) if len(parts) == 3 else 0
    except (ValueError, IndexError) as exc:
        raise ValueError(f"字幕时间格式无效：{value}") from exc
    if len(parts) not in (2, 3) or not 0 <= seconds < 60 or not 0 <= minutes < 60:
        raise ValueError(f"字幕时间格式无效：{value}")
    return hours * 3600 + minutes * 60 + seconds


def _parse_timed_text(text, kind):
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if kind == "vtt":
        lines = text.splitlines()
        if not lines or not re.match(r"^WEBVTT(?:\s|$)", lines[0]):
            raise ValueError("WebVTT 字幕缺少 WEBVTT 文件头。")
        lines.pop(0)
        while lines and re.match(r"^(?:Kind|Language|X-TIMESTAMP-MAP)[:=]", lines[0], re.I):
            lines.pop(0)
        text = "\n".join(lines).strip()
    segments = []
    for block in re.split(r"\n[ \t]*\n+", text) if text else []:
        lines = block.splitlines()
        if kind == "vtt" and re.match(r"^(?:NOTE(?:\s|$)|STYLE$|REGION$)", lines[0]):
            continue
        timing_lines = [i for i, line in enumerate(lines) if "-->" in line]
        if len(timing_lines) != 1 or timing_lines[0] not in (0, 1):
            raise ValueError("字幕段落缺少唯一有效的时间范围。")
        index = timing_lines[0]
        match = _TIMING.fullmatch(lines[index])
        if not match:
            raise ValueError(f"字幕时间范围无效：{lines[index]}")
        segments.append({"start_seconds": _time(match[1]), "end_seconds": _time(match[2]),
                         "text": "\n".join(lines[index + 1:])})
        if len(segments) > MAX_SEGMENTS:
            raise ValueError("字幕超过 50000 段。")
    return segments


def _parse_json(value):
    if isinstance(value, list):
        return value, "transcript-json"
    if not isinstance(value, dict):
        raise ValueError("字幕 JSON 必须是对象或逐段数组。")
    if "transcript" in value:
        return value["transcript"], "transcript-json"
    if "body" in value:
        body = value["body"]
        if not isinstance(body, list) or len(body) > MAX_SEGMENTS:
            raise ValueError("Bilibili 字幕 body 必须是最多 50000 段的数组。")
        if any(not isinstance(item, dict) for item in body):
            raise ValueError("Bilibili 字幕段落必须是对象。")
        return [{"start_seconds": item.get("from"), "end_seconds": item.get("to"),
                 "text": item.get("content")} for item in body], "bilibili-json"
    if "events" in value:
        events = value["events"]
        if not isinstance(events, list) or len(events) > MAX_SEGMENTS:
            raise ValueError("JSON3 events 必须是最多 50000 段的数组。")
        segments = []
        for event in events:
            if not isinstance(event, dict):
                raise ValueError("JSON3 event 必须是对象。")
            parts = event.get("segs", [])
            if not isinstance(parts, list) or any(not isinstance(part, dict) or not isinstance(part.get("utf8"), str) for part in parts):
                raise ValueError("JSON3 segs 必须包含 utf8 文本。")
            text = "".join(part["utf8"] for part in parts)
            if not text.strip():
                continue  # Window/style-only events contain no caption text.
            start = _number(event.get("tStartMs"), "tStartMs") / 1000
            duration = _number(event.get("dDurationMs"), "dDurationMs") / 1000
            segments.append({"start_seconds": start, "end_seconds": start + duration, "text": text})
        return segments, "json3"
    raise ValueError("未识别字幕 JSON 结构；支持 transcript、Bilibili body 和 JSON3 events。")


def _normalize(segments, duration):
    if not isinstance(segments, list) or len(segments) > MAX_SEGMENTS:
        raise ValueError("字幕必须是最多 50000 段的数组。")
    normalized = []
    previous = -1.0
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise ValueError(f"字幕第 {index + 1} 段必须是对象。")
        start = _number(segment.get("start_seconds"), "start_seconds")
        end = _number(segment.get("end_seconds"), "end_seconds")
        if not 0 <= start <= end <= duration + 1e-6 or start < previous:
            raise ValueError(f"字幕第 {index + 1} 段时间倒置、乱序或超出视频时长。")
        previous = start
        text = _clean(segment.get("text"))
        if not text:
            continue
        if end == start:
            raise ValueError(f"字幕第 {index + 1} 段有文字但持续时间为零。")
        start, end = min(start, duration), min(end, duration)
        # Exact repeated rolling captions merge only when overlapping/adjacent.
        # Distinct strings and later deliberate repetitions retain their words.
        if normalized and text == normalized[-1]["text"] and start <= normalized[-1]["end_seconds"] + 0.05:
            normalized[-1]["end_seconds"] = max(end, normalized[-1]["end_seconds"])
        else:
            normalized.append({"start_seconds": start, "end_seconds": end, "text": text})
    return normalized


def _coverage(segments, duration):
    intervals = []
    for segment in segments:
        start, end = segment["start_seconds"], segment["end_seconds"]
        if intervals and start <= intervals[-1][1]:
            intervals[-1][1] = max(intervals[-1][1], end)
        else:
            intervals.append([start, end])
    first = intervals[0][0] if intervals else None
    last = intervals[-1][1] if intervals else None
    covered = sum(end - start for start, end in intervals)
    gaps = ([first] + [right[0] - left[1] for left, right in zip(intervals, intervals[1:])] + [duration - last]) if intervals else [duration]
    # At most 15 seconds or 5% of a long lesson; subsecond clips use 10%.
    tolerance = min(15.0, max(0.05, min(duration * 0.1, max(1.0, duration * 0.05))))
    coverage = {"duration_seconds": duration, "first_start_seconds": first,
                "last_end_seconds": last, "span_seconds": (last - first) if intervals else 0.0,
                "caption_seconds": round(covered, 6), "caption_fraction": round(covered / duration, 6),
                "largest_gap_seconds": round(max(gaps), 6), "boundary_tolerance_seconds": tolerance,
                "method": "timing heuristic; internal gaps may be silence; completeness is not assumed"}
    if not segments:
        return coverage, False, "字幕没有有效文本。"
    if first > tolerance or duration - last > tolerance:
        return coverage, False, "字幕未覆盖视频首尾的主要时间范围，可能只有部分内容。"
    # Two isolated tiny cues at opposite ends must not masquerade as a full track.
    # Large internal silences are otherwise allowed and exposed for Agent review.
    if covered < min(duration * 0.05, 3.0):
        return coverage, False, "字幕时间范围过于稀疏，仅有极少有效字幕。"
    return coverage, True, "字幕时间范围通过基础检查；仍需结合语音、画面核对内容。"


def load_subtitles(path: Path, duration: float, source_sha256: str | None = None) -> dict:
    """Return normalized captions and provenance; invalid data raises ValueError."""
    duration = _number(duration, "视频时长")
    if duration <= 0:
        raise ValueError("视频时长必须大于零。")
    path = Path(path).expanduser().resolve()
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("字幕必须是本地文件，且不超过 16 MiB。")
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("字幕超过 16 MiB。")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("字幕需要 UTF-8 编码。") from exc
    suffix = path.suffix.lower()
    binding = None
    if suffix in {".json", ".json3"}:
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("字幕 JSON 格式无效。") from exc
        if isinstance(value, dict) and "source" in value:
            source = value["source"]
            if not isinstance(source, dict):
                raise ValueError("字幕 JSON 的 source 必须是对象。")
            binding = source.get("sha256")
            if binding is not None:
                if not isinstance(binding, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", binding):
                    raise ValueError("字幕绑定的源视频 SHA256 格式无效。")
                if source_sha256 and binding.lower() != source_sha256.lower():
                    raise ValueError("字幕绑定的源视频 SHA256 与本次输入不一致。")
        segments, kind = _parse_json(value)
    elif suffix in {".srt", ".vtt"}:
        kind = suffix[1:]
        segments = _parse_timed_text(text, kind)
    else:
        raise ValueError("字幕格式仅支持 SRT、VTT、JSON 和 JSON3。")
    segments = _normalize(segments, duration)
    coverage, usable, reason = _coverage(segments, duration)
    return {"segments": segments, "metadata": {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
            "format": kind, "segment_count": len(segments), "source_sha256": binding,
            "coverage": coverage, "usable": usable, "reason": reason}}


def find_subtitles(video_path: Path) -> list[Path]:
    """Find same-stem sidecars only; prefer Chinese, then unlabelled, then others."""
    video = Path(video_path).expanduser().resolve()
    stem = video.stem.casefold()
    candidates = []
    for path in video.parent.iterdir():
        if not path.is_file() or path.suffix.lower() not in {".srt", ".vtt", ".json3"}:
            continue
        name = path.stem.casefold()
        language = "" if name == stem else name[len(stem) + 1:] if name.startswith(stem + ".") else None
        # Reject descriptive filenames (e.g. lesson.backup.vtt) and unrelated
        # nearby captions. Language suffixes follow BCP-47-shaped tags.
        if language is None or (language and not re.fullmatch(r"(?:[a-z]{2,3})(?:[-_][a-z0-9]{2,8})*", language)):
            continue
        chinese = bool(re.match(r"^(?:ai[-_])?(?:zh|cmn|yue)(?:[-_]|$)", language))
        rank = 0 if chinese else 1 if not language else 2
        explicit_auto = bool(re.match(r"^ai[-_]", language))
        candidates.append((rank, explicit_auto, language, path.name.casefold(), path))
    return [item[-1] for item in sorted(candidates)]
