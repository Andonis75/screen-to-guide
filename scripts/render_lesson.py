"""Render an offline reading lesson; media paths are relative to --output-dir.

Usage: python scripts/render_lesson.py --manifest lesson-manifest.json --output-dir DIR
Existing guide.html, guide.md, or lesson.json require --overwrite-guide.
No network access, media copying, or recorded application actions are performed.
"""
from __future__ import annotations

import argparse
import html
import json
import math
from pathlib import Path
import re
from urllib.parse import quote, urlsplit


def _text(value, name, *, nonempty=False):
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ValueError(f"{name} must be {'nonempty ' if nonempty else ''}text")
    return value


def _array(value, name):
    if not isinstance(value, list) or len(value) > 5000:
        raise ValueError(f"{name} must be an array of at most 5000 items")
    return value


def _texts(value, name):
    return [_text(item, name) for item in _array(value, name)]


def _seconds(value, name):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or value < 0 or value > 2**40 or not math.isfinite(value)):
        raise ValueError(f"{name} must be finite nonnegative seconds")
    return value


def _object(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _url(value, name):
    _text(value, name)
    if not value:
        return value
    if re.search(r"[\s<>\"\\\x00-\x1f\x7f]", value):
        raise ValueError(f"{name} must be a plain HTTP(S) URL")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme.lower() in {"http", "https"} and parsed.hostname
                 and parsed.username is None and parsed.password is None)
        parsed.port  # Validate malformed ports, without making a request.
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be a plain HTTP(S) URL")
    return value


def _media(value, root, kind):
    _text(value, kind, nonempty=True)
    parts = value.split("/")
    if (any(part in {"", ".", ".."} or part.endswith((" ", ".")) for part in parts)
            or re.search(r"[\\:%?#<>\"|*\x00-\x1f\x7f]", value)
            or any(re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part, re.I)
                   for part in parts)):
        raise ValueError(f"Unsafe relative {kind} path: {value!r}")
    extensions = ({".jpg", ".jpeg", ".png", ".webp", ".gif"} if kind == "image"
                  else {".mp4", ".webm", ".ogg", ".m4v", ".mov"})
    if Path(value).suffix.lower() not in extensions:
        raise ValueError(f"Unsupported {kind} extension")
    target = (root / value).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"{kind} path escapes output directory")
    if not target.is_file():
        raise ValueError(f"Local {kind} file does not exist: {value}")
    return value


def validate_lesson(manifest, output_dir):
    """Return normalized data without mutating input; retain coverage only as JSON metadata."""
    _object(manifest, "manifest")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise ValueError("Expected schema_version 1")
    if manifest.get("kind") != "lesson":
        raise ValueError("Expected kind 'lesson'")
    # Also reject non-JSON values and NaN/Infinity in internal metadata.
    manifest = json.loads(json.dumps(manifest, ensure_ascii=False, allow_nan=False))
    root = Path(output_dir).resolve()
    source = _object(manifest.get("source"), "source")
    duration = _seconds(source.get("duration_seconds"), "source.duration_seconds")
    if duration <= 0:
        raise ValueError("source.duration_seconds must be positive")
    source_data = {"name": _text(source.get("name"), "source.name", nonempty=True),
                   "duration_seconds": duration}
    for key in ("url", "author", "sha256"):
        if key in source:
            source_data[key] = (_url(source[key], "source.url") if key == "url"
                                else _text(source[key], f"source.{key}"))
    if source_data.get("sha256") and not re.fullmatch(r"[0-9a-fA-F]{64}", source_data["sha256"]):
        raise ValueError("source.sha256 must contain 64 hexadecimal characters")
    for key in ("width", "height"):
        if key in source:
            if type(source[key]) is not int or not 1 <= source[key] <= 32768:
                raise ValueError(f"source.{key} must be an integer from 1 to 32768")
            source_data[key] = source[key]
    data = {"schema_version": 1, "kind": "lesson",
            "title": _text(manifest.get("title"), "title", nonempty=True),
            "summary": _text(manifest.get("summary", ""), "summary"), "source": source_data,
            "prerequisites": _texts(manifest.get("prerequisites", []), "prerequisites"),
            "sections": [], "glossary": [], "references": []}
    if manifest.get("video") is not None:
        data["video"] = _media(manifest["video"], root, "video")
    sections = _array(manifest.get("sections"), "sections")
    if not sections:
        raise ValueError("sections must contain at least one chapter")
    ids, previous_end = set(), 0
    for index, section in enumerate(sections):
        prefix = f"sections[{index}]"
        _object(section, prefix)
        section_id = _text(section.get("id"), prefix + ".id", nonempty=True)
        if section_id in ids:
            raise ValueError("Section ids must be unique")
        ids.add(section_id)
        start = _seconds(section.get("start_seconds"), prefix + ".start_seconds")
        end = _seconds(section.get("end_seconds"), prefix + ".end_seconds")
        if end < start or end > duration or start < previous_end:
            raise ValueError("Sections must be ordered, non-overlapping, and within source duration")
        previous_end = end
        item = {"id": section_id, "title": _text(section.get("title"), prefix + ".title", nonempty=True),
                "start_seconds": start, "end_seconds": end,
                "paragraphs": _texts(section.get("paragraphs", []), prefix + ".paragraphs"),
                "bullets": _texts(section.get("bullets", []), prefix + ".bullets"),
                "steps": _texts(section.get("steps", []), prefix + ".steps"),
                "code_blocks": [], "images": [], "note": _text(section.get("note", ""), prefix + ".note")}
        for block in _array(section.get("code_blocks", []), prefix + ".code_blocks"):
            _object(block, "code block")
            language = _text(block.get("language", ""), "code language")
            if not re.fullmatch(r"[a-zA-Z0-9_+#.\-]{0,40}", language):
                raise ValueError("Code language must be a single safe language token")
            item["code_blocks"].append({"language": language, "code": _text(block.get("code"), "code")})
        previous_image = start
        for image in _array(section.get("images", []), prefix + ".images"):
            _object(image, "image")
            timestamp = _seconds(image.get("timestamp_seconds"), "image.timestamp_seconds")
            if timestamp < previous_image or timestamp > end:
                raise ValueError("Images must be chronological and within their chapter interval")
            previous_image = timestamp
            item["images"].append({"path": _media(image.get("path"), root, "image"),
                                   "timestamp_seconds": timestamp,
                                   "caption": _text(image.get("caption", ""), "image.caption")})
        data["sections"].append(item)
    for item in _array(manifest.get("glossary", []), "glossary"):
        _object(item, "glossary entry")
        data["glossary"].append({"term": _text(item.get("term"), "term", nonempty=True),
                                 "definition": _text(item.get("definition"), "definition")})
    for item in _array(manifest.get("references", []), "references"):
        _object(item, "reference")
        data["references"].append({"title": _text(item.get("title"), "reference.title", nonempty=True),
                                   "url": _url(item.get("url"), "reference.url")})
        if not data["references"][-1]["url"]:
            raise ValueError("reference.url must not be empty")
    if "coverage" in manifest:
        data["coverage"] = _object(manifest["coverage"], "coverage")
    return data


def _md(text):
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~\-])", r"\\\1", html.escape(text, quote=False))


def _time(seconds):
    ticks = math.floor(seconds * 10)
    hours, minutes = divmod(ticks // 600, 60)
    prefix = f"{hours:02d}:" if hours else ""
    return f"{prefix}{minutes:02d}:{ticks // 10 % 60:02d}.{ticks % 10}"


def lesson_markdown(data):
    """Make exactly the Markdown offered by the offline download button."""
    source = data["source"]
    lines = ["# " + _md(data["title"]), ""]
    if data["summary"]:
        lines += [_md(data["summary"]), ""]
    lines += ["来源：" + _md(source["name"]), ""]
    if source.get("author"):
        lines += ["作者：" + _md(source["author"]), ""]
    if source.get("url"):
        lines += ["[查看来源](<" + quote(source["url"], safe=":/?#@!$&*+,;=%") + ">)", ""]
    if data.get("video"):
        lines += ["[打开本地视频](<" + quote(data["video"], safe="/") + ">)", ""]
    if data["prerequisites"]:
        lines += ["## 开始前", ""]
        lines += ["- " + _md(text).replace("\n", "\n  ") for text in data["prerequisites"]] + [""]
    for number, section in enumerate(data["sections"], 1):
        lines += [f"## {number}. {_md(section['title'])}", "",
                  f"视频时间：{_time(section['start_seconds'])}–{_time(section['end_seconds'])}", ""]
        for paragraph in section["paragraphs"]:
            lines += [_md(paragraph), ""]
        if section["bullets"]:
            lines += ["- " + _md(text).replace("\n", "\n  ") for text in section["bullets"]] + [""]
        if section["steps"]:
            lines += ["### 动手步骤", ""]
            lines += [f"{n}. " + _md(text).replace("\n", "\n   ")
                      for n, text in enumerate(section["steps"], 1)] + [""]
        for block in section["code_blocks"]:
            runs = re.findall(r"`+", block["code"])
            fence = "`" * max(3, max(map(len, runs), default=0) + 1)
            lines += [fence + block["language"], block["code"], fence, ""]
        for image in section["images"]:
            caption = image["caption"] or section["title"]
            lines += [f"![{_md(caption)}](<{quote(image['path'], safe='/')}>)", "",
                      _time(image["timestamp_seconds"]) + " · " + _md(caption), ""]
        if section["note"]:
            lines += ["**补充说明：** " + _md(section["note"]), ""]
    if data["glossary"]:
        lines += ["## 术语速查", ""]
        for item in data["glossary"]:
            lines += ["**" + _md(item["term"]) + "**：" + _md(item["definition"]), ""]
    if data["references"]:
        lines += ["## 参考资料", ""]
        for item in data["references"]:
            lines += ["- [" + _md(item["title"]) + "](<" + quote(item["url"], safe=":/?#@!$&*+,;=%") + ">)"]
        lines.append("")
    return "\n".join(lines)


def render_lesson(manifest, output_dir, *, overwrite_guide=False):
    """Write only guide.html, guide.md, lesson.json; all media must already exist."""
    root = Path(output_dir).resolve()
    data = validate_lesson(manifest, root)
    paths = {"html": root / "guide.html", "markdown": root / "guide.md", "json": root / "lesson.json"}
    for target in paths.values():
        if target.is_symlink() or not target.resolve().is_relative_to(root):
            raise ValueError("Output file must not be a symlink or escape output directory")
        if target.exists():
            if not target.is_file():
                raise ValueError(f"Output target is not a file: {target.name}")
            if not overwrite_guide:
                raise FileExistsError(f"{target.name} already exists; pass --overwrite-guide")
    markdown = lesson_markdown(data)
    reading_data = {key: value for key, value in data.items() if key != "coverage"}
    serialized = json.dumps({"lesson": reading_data, "markdown": markdown}, ensure_ascii=False, allow_nan=False)
    embedded = serialized.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    template = (Path(__file__).resolve().parents[1] / "assets" / "lesson.html").read_text(encoding="utf-8")
    replacements = {"__LESSON_TITLE__": html.escape(data["title"], quote=True), "__LESSON_DATA__": embedded}
    page = re.sub(r"__LESSON_TITLE__|__LESSON_DATA__", lambda match: replacements[match.group()], template)
    root.mkdir(parents=True, exist_ok=True)
    for key, content in (("html", page), ("markdown", markdown),
                         ("json", json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")):
        paths[key].write_text(content, encoding="utf-8")
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--overwrite-guide", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
        paths = render_lesson(manifest, args.output_dir, overwrite_guide=args.overwrite_guide)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    print(json.dumps({key: str(value) for key, value in paths.items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
