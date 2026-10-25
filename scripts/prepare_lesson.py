"""Prepare local teaching-video materials, not a finished tutorial.

ASR is saved first so an author can read the complete narration while frame
extraction, OCR and browser-playback preparation continue. No VLM is invoked.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from screen_guide import media, inference

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".runtime" / "venv"
VENV_PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def progress(message):
    print(message, file=sys.stderr, flush=True)


def configure_local_environment():
    cache = ROOT / ".runtime" / "cache"
    for name, path in {
        "HF_HOME": cache / "huggingface", "HF_HUB_CACHE": cache / "huggingface" / "hub",
        "HF_XET_CACHE": cache / "huggingface" / "xet", "TMP": cache / "tmp", "TEMP": cache / "tmp",
    }.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    tempfile.tempdir = str(cache / "tmp")
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def new_output(path, source):
    target = Path(path).expanduser()
    if target.is_symlink():
        raise ValueError("输出目录必须是新目录或空目录，不接受符号链接。")
    target = target.resolve()
    if target == source or (target.exists() and (not target.is_dir() or any(target.iterdir()))):
        raise ValueError("输出目录必须是新目录或空目录；不会覆盖输入或已有材料。")
    target.mkdir(parents=True, exist_ok=True)
    (target / "analysis").mkdir()
    return target


def write_json(path, value):
    # A reader should never see half-written checkpoint JSON.
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def timestamp(seconds):
    millis = round(seconds * 1000)
    return f"{millis // 60000:02d}:{millis // 1000 % 60:02d}.{millis % 1000:03d}"


def save_transcript(path, segments, status):
    lines = ["教学视频语音转写材料（自动转写，尚未编写教材正文）", f"状态：{status}", ""]
    if not segments:
        lines.append("未获得语音转写内容。请查看 lesson-evidence.json 中的 ASR 状态与警告。")
    for segment in segments:
        lines.append(f"[{timestamp(segment['start_seconds'])} --> {timestamp(segment['end_seconds'])}] {segment['text']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_transcript(value, duration):
    segments = value.get("transcript") if isinstance(value, dict) else value
    if not isinstance(segments, list) or len(segments) > 50000:
        raise ValueError("转写 JSON 必须为逐段数组，或包含 transcript 数组的对象。")
    previous = -1.0
    result = []
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
            raise ValueError(f"转写第{index + 1}段缺少文本。")
        start, end = segment.get("start_seconds"), segment.get("end_seconds")
        if any(isinstance(t, bool) or not isinstance(t, (float, int)) or not math.isfinite(t)
               for t in (start, end)) or not 0 <= start <= end <= duration + 1e-6 or start < previous:
            raise ValueError(f"转写第{index + 1}段时间无效、乱序或超出视频时长。")
        result.append({"start_seconds": start, "end_seconds": end, "text": segment["text"]})
        previous = start
    return result


def coverage_index(duration, frames, transcript):
    """Index actual material per window; never label an absent frame as present."""
    result = []
    for index in range(math.ceil(duration / 30)):
        start, end = index * 30, min((index + 1) * 30, duration)
        selected = [f for f in frames if start <= f["timestamp_seconds"] < end
                    or (index == math.ceil(duration / 30) - 1 and f["timestamp_seconds"] == end)]
        segment_ids = [i for i, s in enumerate(transcript)
                       if s["start_seconds"] < end and s["end_seconds"] > start]
        nearest = []
        if not selected and frames:
            frame = min(frames, key=lambda f: abs(f["timestamp_seconds"] - (start + end) / 2))
            nearest = [{"frame_id": frame["id"], "timestamp_seconds": frame["timestamp_seconds"]}]
        result.append({"id": f"coverage-{index + 1:03d}", "start_seconds": start, "end_seconds": end,
                       "frame_ids": [f["id"] for f in selected], "transcript_indices": segment_ids,
                       "has_frame_in_window": bool(selected), "has_transcript_in_window": bool(segment_ids),
                       "nearest_frames_outside_window": nearest})
    return result


def contact_sheets(frames, destination):
    from PIL import Image, ImageDraw, ImageFont
    font_path = Path("C:/Windows/Fonts/consola.ttf")
    font = ImageFont.truetype(str(font_path), 16) if font_path.is_file() else ImageFont.load_default()
    sheets = []
    for offset in range(0, len(frames), 12):
        group = frames[offset:offset + 12]
        canvas = Image.new("RGB", (1960, math.ceil(len(group) / 4) * 304 + 8), "#edf1f5")
        draw = ImageDraw.Draw(canvas)
        for i, frame in enumerate(group):
            x, y = 8 + (i % 4) * 488, 8 + (i // 4) * 304
            with Image.open(destination / frame["image"]) as source:
                picture = source.convert("RGB")
                picture.thumbnail((480, 270))
                canvas.paste(picture, (x + (480 - picture.width) // 2, y + (270 - picture.height) // 2))
            draw.text((x, y + 274), f"{frame['id']} | {timestamp(frame['timestamp_seconds'])}", font=font, fill="#172b45")
        name = f"analysis/contact-{len(sheets) + 1:03d}.jpg"
        canvas.save(destination / name, quality=88)
        sheets.append({"image": name, "frame_ids": [f["id"] for f in group]})
    return sheets


def prepare(options):
    started = time.perf_counter()
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    tools = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe")}
    if not all(tools.values()):
        raise ValueError("请将本地 FFmpeg 和 ffprobe 加入 PATH 后运行。")
    source = Path(options.input).expanduser().resolve()
    # Shared probe enforces the existing local-file, 2 GiB and 30-minute bounds.
    metadata = media.probe_media(source, tools["ffprobe"])
    model_dir = (ROOT / config["asr_model_dir"]).resolve()
    if metadata["has_audio"] and not options.transcript_json:
        required = [model_dir / (stem + suffix) for stem in ("openvino_encoder_model", "openvino_decoder_model")
                    for suffix in (".xml", ".bin")]
        if any(not p.is_file() or p.stat().st_size == 0 for p in required):
            raise ValueError("本地 ASR 模型不完整；本入口不下载模型，请先准备配置中的本地语音模型。")
    destination = new_output(options.output_dir, source)
    source_hash = sha256(source)
    manifest = {"schema_version": 1, "material_type": "teaching_video_evidence",
                "purpose": "供 Agent 阅读的整段教学材料，尚未生成教材或可跟做教程。",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source": {"name": source.name, "sha256": source_hash, **metadata},
                "video": None, "frames": [], "transcript": [], "contact_sheets": [], "coverage": [],
                "processing": {"status": "preparing", "mode": "lesson_materials", "local_only": True,
                               "vlm_used": False, "skill_version": config["version"], "asr": {"status": "pending"},
                               "ocr": {"status": "pending" if not options.no_ocr else "disabled"},
                               "sampling": {"max_frames": options.max_frames, "sample_interval_seconds": 1.0,
                                            "method": "shared extract_frames: temporal coverage plus scene changes",
                                            "timestamp_source": "decoded source PTS; source start_time shifted to zero",
                                            "image_method": "shared extract_frames source-frame export; maximum width 1920"},
                               "tools": tools, "warnings": []}}
    output = destination / "lesson-evidence.json"

    def checkpoint():
        manifest["coverage"] = coverage_index(metadata["duration_seconds"], manifest["frames"], manifest["transcript"])
        manifest["processing"]["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        write_json(output, manifest)

    checkpoint()
    try:
        progress("先提取整段音轨并本地转写；完成后立即保存 transcript.txt")
        if options.transcript_json:
            imported_path = Path(options.transcript_json).expanduser().resolve()
            if not imported_path.is_file() or imported_path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("导入转写必须是本地 JSON 文件，且不超过16MiB。")
            imported = json.loads(imported_path.read_text(encoding="utf-8-sig"))
            imported_source = imported.get("source", {}) if isinstance(imported, dict) else {}
            if not isinstance(imported_source, dict):
                raise ValueError("导入转写的 source 必须是对象。")
            if imported_source.get("sha256") and imported_source["sha256"] != source_hash:
                raise ValueError("导入转写绑定的源视频 SHA256 与本次输入不一致。")
            manifest["transcript"] = validate_transcript(imported, metadata["duration_seconds"])
            manifest["processing"]["asr"] = {
                "status": "imported" if any(s["text"].strip() for s in manifest["transcript"]) else "imported_empty",
                "origin": "explicit --transcript-json; no ASR run in this execution",
                "source_file": str(imported_path), "source_sha256": sha256(imported_path),
                "segment_count": len(manifest["transcript"]), "full_audio_submitted": False,
                "completeness": "Imported segment coverage is indexed, not assumed complete."}
            if not metadata["has_audio"]:
                manifest["processing"]["warnings"].append("源视频没有音轨；此处文字来自显式导入的外部转写。")
            if manifest["processing"]["asr"]["status"] == "imported_empty":
                manifest["processing"]["warnings"].append("导入转写为空，未获得旁白内容。")
            audio = None
        else:
            audio = media.extract_audio(source, destination, ffmpeg=tools["ffmpeg"], ffprobe=tools["ffprobe"])
        if not options.transcript_json and audio is None:
            manifest["processing"]["asr"] = {"status": "no_audio", "device": "CPU"}
            manifest["processing"]["warnings"].append("源视频没有音轨，未生成语音转写。")
        elif not options.transcript_json:
            manifest["processing"]["asr"] = {"status": "running", "device": "CPU", "model_dir": str(model_dir)}
            checkpoint()
            with redirect_stdout(sys.stderr):
                manifest["transcript"] = inference.transcribe(audio, model_dir, "CPU", progress)
            status = "transcribed" if any(s["text"].strip() for s in manifest["transcript"]) else "empty_transcript"
            provenance = model_dir / "model_source.json"
            model_source = json.loads(provenance.read_text(encoding="utf-8")) if provenance.is_file() else {"local_path": str(model_dir)}
            manifest["processing"]["asr"].update(status=status, model_source=model_source,
                                                 segment_count=len(manifest["transcript"]), full_audio_submitted=True)
            if status == "empty_transcript":
                manifest["processing"]["warnings"].append("音轨已处理，但 ASR 没有返回有效文字；未获得完整旁白内容。")
        transcript_path = destination / "analysis" / "transcript.txt"
        save_transcript(transcript_path, manifest["transcript"], manifest["processing"]["asr"]["status"])
        manifest["processing"]["status"] = "asr_ready"
        checkpoint()
        progress(f"ASR_COMPLETE status={manifest['processing']['asr']['status']} segments={len(manifest['transcript'])} transcript={transcript_path}")

        progress(f"提取全片覆盖与场景变化画面，最多 {options.max_frames} 帧")
        manifest["frames"] = media.extract_frames(source, destination, ffmpeg=tools["ffmpeg"], ffprobe=tools["ffprobe"],
                                                   max_frames=options.max_frames, sample_interval=1.0)
        manifest["contact_sheets"] = contact_sheets(manifest["frames"], destination)
        manifest["processing"]["status"] = "frames_ready"
        checkpoint()
        if not options.no_ocr:
            with redirect_stdout(sys.stderr):
                ocr = inference.add_ocr(manifest["frames"], destination, ROOT, progress)
            manifest["processing"]["ocr"] = {"status": "completed", **ocr}
        checkpoint()
        progress("准备可本地播放的视频副本")
        playback = media.ensure_playback_video(source, destination, ffmpeg=tools["ffmpeg"], ffprobe=tools["ffprobe"], metadata=metadata)
        manifest["video"] = playback["video"]
        manifest["processing"]["playback"] = playback["metadata"]
        manifest["processing"]["warnings"].extend(playback["warnings"])
        if sha256(source) != source_hash or playback["metadata"]["source_sha256"] != source_hash:
            raise ValueError("处理期间源视频发生变化，请使用稳定文件重新生成材料。")
        manifest["processing"]["status"] = "complete"
        checkpoint()
        return {"ok": True, "material_type": manifest["material_type"], "output_dir": str(destination),
                "manifest": str(output), "transcript": str(transcript_path), "asr_status": manifest["processing"]["asr"]["status"],
                "frame_count": len(manifest["frames"]), "segment_count": len(manifest["transcript"]),
                "elapsed_seconds": manifest["processing"]["elapsed_seconds"], "warnings": manifest["processing"]["warnings"]}
    except Exception as exc:
        if manifest["processing"]["asr"]["status"] == "running":
            manifest["processing"]["asr"]["status"] = "failed"
        manifest["processing"].update(status="failed", error=str(exc))
        checkpoint()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="本地教学视频；沿用最多30分钟、2GiB限制")
    parser.add_argument("--output-dir", required=True, help="全新或空的输出目录")
    parser.add_argument("--max-frames", type=int, default=64, help="最多1至96张实际画面，默认64")
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--transcript-json", help="显式导入已有逐段转写，校验时间边界并标记 imported 来源；默认仍本地ASR")
    options = parser.parse_args(argv)
    if not 1 <= options.max_frames <= 96:
        parser.error("--max-frames 必须为1至96")
    try:
        configure_local_environment()
        result = prepare(options)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if VENV_PYTHON.is_file() and Path(sys.prefix).resolve() != VENV.resolve():
        raise SystemExit(subprocess.call([str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]]))
    raise SystemExit(main())
