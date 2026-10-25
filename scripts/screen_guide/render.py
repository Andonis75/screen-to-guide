"""Render a portable, offline review guide from an untrusted JSON manifest."""
from __future__ import annotations

import html
import json
import math
import re
from pathlib import Path
from urllib.parse import quote

from .actions import validate_action_evidence

STATUSES = {"pending": "待复核", "confirmed": "已确认", "uncertain": "存疑"}
UNVERIFIED_NOTE = "无证据草稿：未提供抽帧记录，时间与截图未经来源关联校验。"


def _text(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    return value


def _seconds(value, name):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or value < 0 or value > (2**53 // 10) or not math.isfinite(value)):
        raise ValueError(f"{name} must be finite nonnegative seconds")
    return value


def _texts(value, name):
    if not isinstance(value, list) or len(value) > 5000 or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a bounded text array")
    return value


def _media_path(value, root, kind):
    if value is None:
        return None
    _text(value, kind)
    parts = value.split("/")
    if (not value or any(p in {"", ".", ".."} or p.endswith((" ", ".")) for p in parts)
            or re.search(r"[\\:%?#\x00-\x1f\x7f]", value)):
        raise ValueError(f"Unsafe local {kind} path: {value!r}")
    extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"} if kind == "image" else {".mp4", ".webm", ".ogg", ".m4v", ".mov"}
    if Path(value).suffix.lower() not in extensions:
        raise ValueError(f"Unsupported {kind} extension")
    if not (root / value).resolve().is_relative_to(root.resolve()):
        raise ValueError(f"{kind} path escapes output directory")
    return value


def _validate(manifest, root):
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or isinstance(manifest.get("schema_version"), bool):
        raise ValueError("Expected manifest schema_version 1")
    # Reject non-JSON values and NaN before embedding. Never mutate caller data.
    data = json.loads(json.dumps(manifest, ensure_ascii=False, allow_nan=False))
    for key, default in (("title", "操作手册草稿"), ("created_at", "")):
        data[key] = _text(data.get(key, default), key)
    for key in ("source", "processing"):
        data.setdefault(key, {})
        if not isinstance(data[key], dict):
            raise ValueError(f"{key} must be an object")
    duration = (_seconds(data["source"]["duration_seconds"], "source.duration_seconds")
                if "duration_seconds" in data["source"] else None)
    if "tutorial" in data:
        tutorial = data["tutorial"]
        if not isinstance(tutorial, dict):
            raise ValueError("tutorial must be an object")
        for field in ("summary", "completion"):
            tutorial[field] = _text(tutorial.get(field, ""), f"tutorial.{field}")
        tutorial["prerequisites"] = _texts(tutorial.get("prerequisites", []), "tutorial.prerequisites")
        tutorial.setdefault("example_settings", [])
        if not isinstance(tutorial["example_settings"], list) or len(tutorial["example_settings"]) > 5000:
            raise ValueError("tutorial.example_settings must be a bounded array")
        for setting in tutorial["example_settings"]:
            if not isinstance(setting, dict):
                raise ValueError("tutorial settings must be objects")
            for field in ("label", "value"):
                _text(setting.get(field), f"tutorial setting {field}")
    data["video"] = _media_path(data.get("video"), root, "video")
    for key in ("frames", "steps", "transcript"):
        data.setdefault(key, [])
        if not isinstance(data[key], list) or len(data[key]) > 5000:
            raise ValueError(f"{key} must be a bounded array")
    for segment in data["transcript"]:
        if not isinstance(segment, dict):
            raise ValueError("transcript entries must be objects")
        segment["text"] = _text(segment.get("text"), "transcript text")
        segment["start_seconds"] = _seconds(segment.get("start_seconds"), "start_seconds")
        segment["end_seconds"] = _seconds(segment.get("end_seconds"), "end_seconds")
        if segment["end_seconds"] < segment["start_seconds"]:
            raise ValueError("transcript end precedes start")
    for key in ("frames", "steps"):
        seen_ids = set()
        for index, item in enumerate(data[key]):
            if not isinstance(item, dict):
                raise ValueError(f"{key} entries must be objects")
            item["id"] = _text(item.get("id"), "id")
            if not item["id"] or item["id"] in seen_ids:
                raise ValueError(f"{key} id must be nonempty and unique")
            seen_ids.add(item["id"])
            item["timestamp_seconds"] = _seconds(item.get("timestamp_seconds"), "timestamp_seconds")
            if duration is not None and item["timestamp_seconds"] > duration:
                raise ValueError(f"{key} timestamp exceeds source duration")
            item["image"] = _media_path(item.get("image"), root, "image")
            if key == "steps":
                for field in ("title", "description", "review_notes", "outcome", "tip"):
                    item[field] = _text(item.get(field, ""), field)
                item["actions"] = _texts(item.get("actions", []), "actions")
                item["playback_start_seconds"] = _seconds(item.get("playback_start_seconds", item["timestamp_seconds"]), "playback_start_seconds")
                if item["playback_start_seconds"] > item["timestamp_seconds"]:
                    raise ValueError("playback_start_seconds exceeds screenshot timestamp")
                item["review_status"] = _text(item.get("review_status", "pending"), "review_status")
                if item["review_status"] not in STATUSES:
                    raise ValueError("Unknown review_status")
                for field in ("frame_ids", "observed_text"):
                    item.setdefault(field, [])
                    if not isinstance(item[field], list) or not all(isinstance(x, str) for x in item[field]):
                        raise ValueError(f"{field} must be a text array")
                if item.get("end_seconds") is not None:
                    end = _seconds(item["end_seconds"], "end_seconds")
                    if end < item["timestamp_seconds"] or (duration is not None and end > duration):
                        raise ValueError("end_seconds is outside the step/source interval")
    frames = {frame["id"]: frame for frame in data["frames"]}
    for step in data["steps"]:
        refs = step["frame_ids"]
        if any(frame_id not in frames for frame_id in refs):
            raise ValueError("step references an unknown frame id")
        if frames:
            if not refs:
                raise ValueError("step must reference at least one actual frame")
            primary = frames[refs[0]]
            if abs(step["timestamp_seconds"] - primary["timestamp_seconds"]) > 1e-6:
                raise ValueError("step timestamp does not match its primary frame")
            if step["image"] != primary["image"]:
                raise ValueError("step image does not match its primary frame")
        else:
            step["basis"] = "unverified_draft"
            step["evidence_note"] = UNVERIFIED_NOTE
    validate_action_evidence(data)
    return data


def _md(value):
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", html.escape(str(value), quote=False))


def _time(seconds):
    ticks = math.floor(seconds * 10)
    return f"{ticks // 600:02d}:{ticks // 10 % 60:02d}.{ticks % 10}"


def _markdown(data):
    lines = [f"# {_md(data['title'])}", "", f"来源：{_md(data['source'].get('name', '未注明'))}",
             f"生成时间：{_md(data['created_at'])}", "", "人工复核状态以每步标记为准；画面推断不等同于已确认操作。", ""]
    if data["processing"].get("agent_review"):
        lines += ["本稿已由 Agent 对照原始截图修订，非本地模型直出，也不代表用户已确认。", ""]
    for index, step in enumerate(data["steps"], 1):
        lines += [f"## {index}. {_md(step['title'])}", "", f"时间：{_time(step['timestamp_seconds'])} · 状态：{STATUSES[step['review_status']]}", "", _md(step["description"]), ""]
        if step.get("basis") == "unverified_draft":
            lines += [UNVERIFIED_NOTE, ""]
        if step["image"]:
            lines += [f"![步骤 {index} 截图](<{quote(step['image'], safe='/')}>)", ""]
        if step["observed_text"]:
            lines += ["画面文字：" + " / ".join(_md(x) for x in step["observed_text"]), ""]
        if step["review_notes"]:
            lines += ["复核备注：" + _md(step["review_notes"]), ""]
    if not data["steps"]:
        lines += ["暂无步骤。", ""]
    if data["source"].get("has_audio") is not False and data["transcript"]:
        lines += ["## 语音转写（需核对）", ""]
        for segment in data["transcript"]:
            if segment["text"].strip():
                lines += [f"{_time(segment['start_seconds'])}–{_time(segment['end_seconds'])}：{_md(segment['text'])}", ""]
    return "\n".join(lines)


def _tutorial_markdown(data):
    tutorial = data["tutorial"]
    lines = [f"# {_md(data['title'])}", ""]
    if tutorial["summary"]:
        lines += [_md(tutorial["summary"]), ""]
    if tutorial["prerequisites"]:
        lines += ["## 开始前", ""]
        lines += ["- " + _md(item) for item in tutorial["prerequisites"]] + [""]
    if tutorial["example_settings"]:
        lines += ["## 本例参数", "", "| 参数 | 设置 |", "| --- | --- |"]
        for item in tutorial["example_settings"]:
            cells = [_md(item[field]).replace("\r", "").replace("\n", "<br>") for field in ("label", "value")]
            lines.append(f"| {cells[0]} | {cells[1]} |")
        lines.append("")
    for index, step in enumerate(data["steps"], 1):
        lines += [f"## {index}. {_md(step['title'])}", "", f"录屏时间：{_time(step['playback_start_seconds'])}", ""]
        if step["description"]:
            lines += [_md(step["description"]), ""]
        if step["actions"]:
            lines += [f"{number}. {_md(action)}" for number, action in enumerate(step["actions"], 1)] + [""]
        if step["outcome"]:
            lines += ["**完成后：** " + _md(step["outcome"]), ""]
        if step["image"]:
            lines += [f"![步骤 {index}：{_md(step['title'])}](<{quote(step['image'], safe='/')}>)", ""]
        if step["tip"]:
            lines += ["**小提示：** " + _md(step["tip"]), ""]
    if tutorial["completion"]:
        lines += ["## 完成", "", _md(tutorial["completion"]), ""]
    return "\n".join(lines)


def render_guide(manifest: dict, output_dir: Path) -> dict:
    """Write guide.html, guide.md, steps.json; return html/markdown/json Paths.

    Valid relative media may be absent (the viewer reports this explicitly).
    Media assets remain relative to output_dir and must travel with the guide.
    """
    root = Path(output_dir).resolve()
    data = _validate(manifest, root)
    paths = {"html": root / "guide.html", "markdown": root / "guide.md", "json": root / "steps.json"}
    for target in paths.values():
        if target.is_symlink() or not target.resolve().is_relative_to(root):
            raise ValueError("Output file points outside output directory")
    tutorial_mode = "tutorial" in data
    template_name = "tutorial.html" if tutorial_mode else "guide.html"
    template = (Path(__file__).resolve().parents[2] / "assets" / template_name).read_text(encoding="utf-8")
    serialized = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    embedded = serialized.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    replacements = {"__GUIDE_DATA__": embedded, "__GUIDE_TITLE__": html.escape(data["title"], quote=True)}
    page = re.sub(r"__GUIDE_DATA__|__GUIDE_TITLE__", lambda match: replacements[match.group()], template)
    root.mkdir(parents=True, exist_ok=True)
    markdown = _tutorial_markdown(data) if tutorial_mode else _markdown(data)
    for key, content in (("html", page), ("markdown", markdown), ("json", serialized + "\n")):
        paths[key].write_text(content, encoding="utf-8")
    return paths
