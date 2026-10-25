"""Public local-only entry point for the screen-to-guide Skill."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".runtime" / "venv"
VENV_PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def progress(message):
    print(str(message), file=sys.stderr, flush=True)


def configuration():
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def parser():
    result = argparse.ArgumentParser(description="录屏成册：本地录屏 → 可人工复核的图文手册")
    result.add_argument("input", nargs="?", help="本地录屏文件")
    actions = result.add_mutually_exclusive_group()
    for action in ("setup", "doctor", "capabilities", "prepare-model"):
        actions.add_argument("--" + action, action="store_true")
    actions.add_argument("--render-manifest", metavar="JSON", help="从已复核 JSON 重建指南，不运行模型")
    result.add_argument("--overwrite-guide", action="store_true", help="允许重建 guide.html、guide.md、steps.json")
    result.add_argument("--kind", choices=("vlm", "asr"), default="vlm")
    result.add_argument("--model-id")
    result.add_argument("--model-dir")
    result.add_argument("--mode", choices=("analyze", "evidence"), default="analyze")
    result.add_argument("--output-dir")
    result.add_argument("--max-frames", type=int, default=12)
    result.add_argument("--sample-interval", type=float, default=1.0)
    result.add_argument("--device", default=None)
    result.add_argument("--no-ocr", action="store_true")
    result.add_argument("--transcribe", action="store_true")
    result.add_argument("--no-copy-video", action="store_true")
    result.add_argument("--ffmpeg", default="ffmpeg")
    result.add_argument("--ffprobe", default="ffprobe")
    result.add_argument("--format", choices=("text", "json"), default="text")
    return result


def model_state(directory, kind="vlm"):
    directory = Path(directory).resolve()
    stems = (["openvino_language_model", "openvino_vision_embeddings_model", "openvino_text_embeddings_model",
              "openvino_tokenizer", "openvino_detokenizer"] if kind == "vlm"
             else ["openvino_encoder_model", "openvino_decoder_model"])
    if kind == "vlm" and (directory / "openvino_vision_embeddings_merger_model.xml").exists():
        stems.append("openvino_vision_embeddings_merger_model")
    required = [stem + suffix for stem in stems for suffix in (".xml", ".bin")]
    missing = [name for name in required if not (directory / name).is_file() or not (directory / name).stat().st_size]
    return {"path": str(directory), "assets_present": not missing, "missing": missing}


def tool_state(value):
    path = shutil.which(value)
    if not path:
        return {"available": False, "path": None}
    try:
        check = subprocess.run([path, "-version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        return {"available": check.returncode == 0, "path": str(Path(path).resolve()),
                "version": check.stdout.splitlines()[0] if check.stdout else ""}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "path": path, "error": str(exc)}


def doctor(options, config):
    packages = {}
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        name = line.split("==")[0].strip()
        if not name or name.startswith("#"):
            continue
        try:
            packages[name] = {"installed": True, "version": importlib.metadata.version(name)}
        except importlib.metadata.PackageNotFoundError:
            packages[name] = {"installed": False, "version": None}
    devices, device_error = [], None
    try:
        with redirect_stdout(sys.stderr):
            import openvino as ov
            devices = list(ov.Core().available_devices)
    except (ImportError, RuntimeError) as exc:
        device_error = str(exc)
    vlm = model_state(options.model_dir or ROOT / config["model_dir"])
    asr = model_state(ROOT / config["asr_model_dir"], "asr")
    tools = {"ffmpeg": tool_state(options.ffmpeg), "ffprobe": tool_state(options.ffprobe)}
    return {"ok": True, "skill": "screen-to-guide", "version": config["version"],
            "python": {"executable": sys.executable, "version": sys.version.split()[0],
                       "dedicated_environment": Path(sys.prefix).resolve() == VENV.resolve()},
            "dependencies": packages, "tools": tools, "models": {"vlm": vlm, "asr": asr},
            "devices": devices, "device_error": device_error,
            "capabilities": {"evidence": all(t["available"] for t in tools.values()),
                             "analyze_assets_present": vlm["assets_present"], "transcribe_assets_present": asr["assets_present"],
                             "local_only_processing": True, "model_download_requires_explicit_prepare": True,
                             "max_frames": 96, "max_duration_seconds": 1800, "max_file_bytes": 2147483648}}


def setup_environment():
    uv = shutil.which("uv")
    candidate = Path.home() / ".local" / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    if not uv and candidate.is_file():
        uv = str(candidate)
    if not VENV_PYTHON.is_file():
        VENV.parent.mkdir(parents=True, exist_ok=True)
        if uv:
            command = [uv, "venv", "--python", "3.11", str(VENV)]
        else:
            old_python = ROOT.parent / "local-doc-guardian" / ".runtime" / "venv" / "Scripts" / "python.exe"
            bootstrap = str(old_python) if old_python.is_file() else sys.executable
            check = subprocess.run([bootstrap, "-c", "import sys; print('.'.join(map(str,sys.version_info[:2])))"],
                                   capture_output=True, text=True, timeout=15, check=True)
            if check.stdout.strip() != "3.11":
                raise ValueError("请先安装 Python 3.11 并配置 PATH，再运行 --setup；uv 为可选安装加速工具。")
            command = [bootstrap, "-m", "venv", str(VENV)]
        progress("创建本 Skill 独立的 Python 3.11 环境")
        subprocess.run(command, stdout=sys.stderr, stderr=sys.stderr, check=True, timeout=600)
    progress("安装本 Skill 的固定版本依赖")
    command = ([uv, "pip", "install", "--python", str(VENV_PYTHON)] if uv
               else [str(VENV_PYTHON), "-m", "pip", "install", "--disable-pip-version-check"])
    subprocess.run(command + ["-r", str(ROOT / "requirements.txt")], stdout=sys.stderr, stderr=sys.stderr, check=True, timeout=1800)
    return {"ok": True, "action": "setup", "python": str(VENV_PYTHON), "model_downloaded": False,
            "next": "scripts/run.ps1 --prepare-model"}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def output_directory(value, source):
    if value:
        destination = Path(value).expanduser().resolve()
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = ROOT / ".runtime" / "outputs" / f"{source.stem}-{stamp}-{uuid.uuid4().hex[:8]}"
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("输出目录必须是新目录或空目录，以免覆盖既有资料。")
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def render_manifest(options):
    from screen_guide.render import render_guide, _validate
    source = Path(options.render_manifest).expanduser().resolve()
    if not source.is_file() or source.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("复核 JSON 应为不超过 20 MiB 的本地文件。")
    destination = Path(options.output_dir).expanduser().resolve() if options.output_dir else source.parent
    try:
        manifest = json.loads(source.read_text(encoding="utf-8-sig"))
        # Validate every field before accessing media references or writing files.
        manifest = _validate(manifest, destination)
    except RecursionError as exc:
        raise ValueError("复核 JSON 嵌套过深，请使用指南页面导出的结构。") from exc
    for name in ("guide.html", "guide.md", "steps.json"):
        target = destination / name
        if target.exists() and not options.overwrite_guide:
            raise ValueError("指南导出文件已存在；确认重建时添加 --overwrite-guide。")
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError("指南输出目标必须是普通文件。")
    references = [manifest.get("video")] + [frame.get("image") for frame in manifest.get("frames", [])]
    references += [step.get("image") for step in manifest.get("steps", [])]
    for relative in filter(None, references):
        if not isinstance(relative, str):
            raise ValueError("指南媒体路径应为相对路径文字。")
        target = (destination / relative).resolve()
        if not target.is_relative_to(destination) or not target.is_file():
            raise ValueError(f"指南媒体文件缺失或越界：{relative}。请用 --output-dir 指向原指南目录。")
    paths = render_guide(manifest, destination)
    return {"ok": True, "action": "render-manifest", "output_dir": str(destination),
            "files": {key: str(value) for key, value in paths.items()}}


def process_video(options, config):
    from screen_guide.media import probe_media, extract_frames, extract_audio, ensure_playback_video
    from screen_guide.inference import add_ocr, analyze_frames, evidence_steps, transcribe
    from screen_guide.render import render_guide
    started = time.perf_counter()
    model_dir = Path(options.model_dir or ROOT / config["model_dir"]).expanduser().resolve()
    if options.mode == "analyze" and not model_state(model_dir)["assets_present"]:
        raise ValueError("本地视觉模型尚未完整准备。先运行 --prepare-model，或显式使用 --mode evidence。")
    resolved_tools = {name: tool_state(getattr(options, name)) for name in ("ffmpeg", "ffprobe")}
    if not all(item["available"] for item in resolved_tools.values()):
        raise ValueError("请安装 FFmpeg/ffprobe 并加入 PATH，或传入 --ffmpeg 与 --ffprobe 的完整路径。")
    ffmpeg, ffprobe = resolved_tools["ffmpeg"]["path"], resolved_tools["ffprobe"]["path"]
    metadata = probe_media(options.input, ffprobe)
    source = Path(options.input).expanduser().resolve()
    asr_dir = (ROOT / config["asr_model_dir"]).resolve()
    if options.transcribe and metadata["has_audio"] and not model_state(asr_dir, "asr")["assets_present"]:
        raise ValueError("本地语音模型尚未准备。先运行 --prepare-model --kind asr。")
    destination = output_directory(options.output_dir, source)
    progress("校验录屏并提取带原始时间戳的截图")
    original_hash = sha256(source)
    frames = extract_frames(source, destination, ffmpeg=ffmpeg, ffprobe=ffprobe,
                            max_frames=options.max_frames, sample_interval=options.sample_interval)
    warnings, ocr_metadata, transcript = [], None, []
    if not options.no_ocr:
        with redirect_stdout(sys.stderr):
            ocr_metadata = add_ocr(frames, destination, ROOT, progress)
    if options.transcribe:
        audio = extract_audio(source, destination, ffmpeg=ffmpeg, ffprobe=ffprobe)
        if audio is None:
            warnings.append("源录屏没有音轨，未生成旁白转写。")
        else:
            with redirect_stdout(sys.stderr):
                transcript = transcribe(audio, asr_dir, options.device or config["device"], progress)
    config = dict(config, resolved_model_dir=str(model_dir), device=options.device or config["device"],
                  duration_seconds=metadata["duration_seconds"], transcript=transcript)
    model_metadata = {"model": None, "device": None}
    if options.mode == "analyze":
        with redirect_stdout(sys.stderr):
            steps, model_metadata = analyze_frames(frames, destination, config, progress)
        warnings.extend(model_metadata.pop("warnings", []))
    else:
        steps = evidence_steps(frames, metadata["duration_seconds"])
        warnings.append("证据模式：仅保存截图与识别文字，尚未判断操作步骤。")
    for step in steps:
        step["review_status"] = "pending"
    if sha256(source) != original_hash:
        raise ValueError("处理期间源录屏发生变化；请使用稳定的文件重新生成。")
    packaged_video, playback_metadata = None, None
    if not options.no_copy_video:
        progress("准备浏览器播放副本并校验时间轴")
        playback = ensure_playback_video(source, destination, ffmpeg=ffmpeg, ffprobe=ffprobe, metadata=metadata)
        packaged_video, playback_metadata = playback["video"], playback["metadata"]
        warnings.extend(playback["warnings"])
        if playback_metadata["source_sha256"] != original_hash:
            raise ValueError("处理期间源录屏发生变化；播放副本与截图来源校验不一致。")
    manifest = {"schema_version": 1, "title": source.stem + " · 操作手册草稿",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source": {"name": source.name, "sha256": original_hash, **metadata},
                "processing": {"mode": options.mode, "local_only": True, **model_metadata, "ocr": ocr_metadata, "playback": playback_metadata,
                               "elapsed_seconds": round(time.perf_counter() - started, 3), "warnings": warnings},
                "video": packaged_video, "frames": frames, "steps": steps, "transcript": transcript}
    paths = render_guide(manifest, destination)
    return {"ok": True, "mode": options.mode, "output_dir": str(destination), "frame_count": len(frames),
            "step_count": len(steps), "warnings": warnings, "files": {key: str(value) for key, value in paths.items()}}


def main(arguments=None):
    options = parser().parse_args(arguments)
    try:
        cache = ROOT / ".runtime" / "cache"
        for name, path in {
            "UV_CACHE_DIR": cache / "uv", "PIP_CACHE_DIR": cache / "pip",
            "UV_PYTHON_INSTALL_DIR": ROOT / ".runtime" / "python",
            "HF_HOME": cache / "huggingface", "HF_HUB_CACHE": cache / "huggingface" / "hub",
            "HF_XET_CACHE": cache / "huggingface" / "xet", "TMP": cache / "tmp", "TEMP": cache / "tmp",
        }.items():
            path.mkdir(parents=True, exist_ok=True)
            os.environ[name] = str(path)
        import tempfile
        tempfile.tempdir = str(cache / "tmp")
        if options.input and (options.setup or options.doctor or options.capabilities or options.prepare_model or options.render_manifest):
            raise ValueError("管理命令或重建指南命令无需同时传入录屏。")
        if options.overwrite_guide and not options.render_manifest:
            raise ValueError("--overwrite-guide 仅用于 --render-manifest。")
        config = configuration()
        if options.setup:
            result = setup_environment()
        elif options.prepare_model:
            from prepare_models import prepare
            for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
                os.environ.pop(key, None)
            os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
            with redirect_stdout(sys.stderr):
                provenance = prepare(options.kind, options.model_id)
            result = {"ok": True, "action": "prepare-model", "kind": options.kind, "source": provenance}
        else:
            os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")
            if options.doctor or options.capabilities:
                result = doctor(options, config)
            elif options.render_manifest:
                result = render_manifest(options)
            elif options.input:
                result = process_video(options, config)
            else:
                raise ValueError("请传入本地录屏，或运行 --doctor 查看环境。")
        if options.format == "json" or options.doctor or options.capabilities:
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        elif result.get("files"):
            print("已生成：" + result["files"]["html"])
            for warning in result.get("warnings", []):
                progress(warning)
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError) as exc:
        message = str(exc)
        progress(message)
        if options.format == "json":
            print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if "--setup" not in sys.argv and VENV_PYTHON.is_file() and Path(sys.prefix).resolve() != VENV.resolve():
        raise SystemExit(subprocess.call([str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]]))
    raise SystemExit(main())
