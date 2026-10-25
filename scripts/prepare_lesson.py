"""Prepare local teaching-video materials, not a finished tutorial.

Subtitles or local ASR are saved first so an author can read the narration while
frame extraction, OCR and browser-playback preparation continue. No VLM is invoked.
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
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from screen_guide import media, inference
from screen_guide import subtitles
from screen_guide.lesson_cache import MaterialCache, atomic_json, atomic_text

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


def new_output(path, source, *, resume=False):
    target = Path(path).expanduser()
    if target.is_symlink():
        raise ValueError("输出目录必须是新目录或空目录，不接受符号链接。")
    target = target.resolve()
    if target == source or (target.exists() and (not target.is_dir() or (not resume and any(target.iterdir())))):
        raise ValueError("输出目录必须是新目录或空目录；不会覆盖输入或已有材料。")
    target.mkdir(parents=True, exist_ok=True)
    if resume:
        # Check before any resumed writer touches an existing path. On Windows
        # junctions are reparse points even when is_symlink() is false.
        for parent, directories, files in os.walk(target, followlinks=False):
            for name in directories + files:
                item = Path(parent) / name
                attributes = getattr(item.lstat(), 'st_file_attributes', 0)
                if item.is_symlink() or attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
                    raise ValueError(f'恢复目录中存在链接，请使用普通本地文件：{item.name}')
    analysis = target / "analysis"
    if analysis.is_symlink() or not analysis.resolve().is_relative_to(target):
        raise ValueError("材料目录不接受外部链接。")
    analysis.mkdir(exist_ok=resume)
    return target


def select_transcript(options, source, source_hash, duration):
    """Select material, recording failed auto candidates without silently trusting them."""
    explicit = getattr(options, 'transcript_json', None)
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('导入转写必须是本地 JSON 文件，且不超过16MiB。')
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        origin = data.get('source', {}) if isinstance(data, dict) else {}
        if not isinstance(origin, dict) or (origin.get('sha256') and origin['sha256'] != source_hash):
            raise ValueError('导入转写的源视频 SHA256 无效或不一致。')
        segments = validate_transcript(data, duration)
        return segments, {'status': 'imported' if any(s['text'].strip() for s in segments) else 'imported_empty',
                         'origin': 'explicit --transcript-json; no ASR run in this execution',
                         'source_file': str(path), 'source_sha256': sha256(path),
                         'segment_count': len(segments), 'full_audio_submitted': False,
                         'completeness': 'Imported segment coverage is indexed, not assumed complete.'}, []
    warnings = []
    subtitle_file = getattr(options, 'subtitle_file', None)
    candidates = ([Path(subtitle_file).expanduser().resolve()] if subtitle_file else
                  [] if getattr(options, 'no_subtitles', False) else subtitles.find_subtitles(source))
    for path in candidates:
        try:
            result = subtitles.load_subtitles(path, duration, source_hash)
        except (ValueError, OSError) as exc:
            if subtitle_file:
                raise ValueError(f'指定字幕读取失败：{exc}') from exc
            warnings.append(f'未采用字幕 {path.name}：{exc}')
            continue
        if result['metadata']['usable']:
            return result['segments'], {'status': 'subtitles', 'origin': 'local subtitle file; ASR skipped',
                'source_file': str(path), 'source_sha256': result['metadata']['sha256'],
                'segment_count': len(result['segments']), 'full_audio_submitted': False,
                'subtitle': result['metadata'],
                'completeness': 'Timestamp screening passed; accuracy and completeness still require video review.'}, warnings
        warnings.append(f"未采用字幕 {path.name}：{result['metadata']['reason']}；回退本地转写。")
    return None, None, warnings


def write_json(path, value):
    atomic_json(path, value)


def timestamp(seconds):
    millis = round(seconds * 1000)
    return f"{millis // 60000:02d}:{millis // 1000 % 60:02d}.{millis % 1000:03d}"


def save_transcript(path, segments, status):
    lines = ["教学视频语音转写材料（自动转写，尚未编写教材正文）", f"状态：{status}", ""]
    if not segments:
        lines.append("未获得语音转写内容。请查看 lesson-evidence.json 中的 ASR 状态与警告。")
    for segment in segments:
        lines.append(f"[{timestamp(segment['start_seconds'])} --> {timestamp(segment['end_seconds'])}] {segment['text']}")
    atomic_text(path, "\n".join(lines) + "\n")


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
    config = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
    tools = {name: shutil.which(name) for name in ('ffmpeg', 'ffprobe')}
    if not all(tools.values()):
        raise ValueError('请将本地 FFmpeg 和 ffprobe 加入 PATH 后运行。')
    source = Path(options.input).expanduser().resolve()
    metadata = media.probe_media(source, tools['ffprobe'])
    source_hash = sha256(source)
    imported, imported_info, subtitle_warnings = select_transcript(options, source, source_hash, metadata['duration_seconds'])
    resume = getattr(options, 'resume', False)
    destination = new_output(options.output_dir, source, resume=resume)
    model_dir = (ROOT / config['asr_model_dir']).resolve()
    identity = {'pipeline_version': 1, 'source_sha256': source_hash,
                'max_frames': options.max_frames, 'no_ocr': options.no_ocr,
                'transcript_source': imported_info.get('source_sha256') if imported_info else None,
                'transcript_mode': imported_info.get('status') if imported_info else 'local_asr',
                'asr_model': [config['asr_model_id'], config['asr_model_revision']]}
    cache = MaterialCache(destination, identity, resume=resume)
    manifest = {'schema_version': 1, 'material_type': 'teaching_video_evidence',
        'purpose': '供 Agent 阅读的整段教学材料，尚未生成教材或可跟做教程。',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'source': {'name': source.name, 'sha256': source_hash, **metadata},
        'video': None, 'frames': [], 'transcript': [], 'contact_sheets': [], 'coverage': [],
        'processing': {'status': 'preparing', 'mode': 'lesson_materials', 'local_only': True,
            'vlm_used': False, 'skill_version': config['version'], 'asr': {'status': 'pending'},
            'ocr': {'status': 'pending' if not options.no_ocr else 'disabled'},
            'sampling': {'max_frames': options.max_frames, 'sample_interval_seconds': 1.0,
                'method': 'shared extract_frames: temporal coverage plus scene changes',
                'timestamp_source': 'decoded source PTS; source start_time shifted to zero',
                'image_method': 'shared extract_frames source-frame export; maximum width 1920'},
            'tools': tools, 'warnings': list(subtitle_warnings), 'resume_requested': resume,
            'reused_stages': [], 'stage_seconds': {}}}
    output = destination / 'lesson-evidence.json'
    transcript_path = destination / 'analysis/transcript.txt'

    def checkpoint():
        manifest['coverage'] = coverage_index(metadata['duration_seconds'], manifest['frames'], manifest['transcript'])
        manifest['processing']['elapsed_seconds'] = round(time.perf_counter() - started, 3)
        manifest['processing']['reused_stages'] = list(cache.reused)
        write_json(output, manifest)

    def timed(name, start):
        manifest['processing']['stage_seconds'][name] = round(time.perf_counter() - start, 3)

    checkpoint()
    try:
        stage_start = time.perf_counter()
        transcript_cache = cache.get('transcript')
        if transcript_cache is not None:
            manifest['transcript'] = validate_transcript(transcript_cache['transcript'], metadata['duration_seconds'])
            manifest['processing']['asr'] = transcript_cache['asr']
            progress('CACHE_HIT transcript：复用已校验的字幕或转写')
        elif imported is not None:
            manifest['transcript'], manifest['processing']['asr'] = imported, imported_info
            progress(f"TRANSCRIPT_IMPORTED status={imported_info['status']} segments={len(imported)}; 跳过 ASR")
        elif not metadata['has_audio']:
            manifest['processing']['asr'] = {'status': 'no_audio', 'device': 'CPU'}
        else:
            required = [model_dir / (stem + suffix) for stem in ('openvino_encoder_model', 'openvino_decoder_model')
                        for suffix in ('.xml', '.bin')]
            if any(not p.is_file() or p.stat().st_size == 0 for p in required):
                raise ValueError('没有可用字幕且本地 ASR 模型不完整；请先准备配置中的本地语音模型。')
            progress('没有通过筛选的字幕：提取完整音轨并本地转写')
            audio_cache = cache.get('audio')
            if audio_cache:
                audio = cache.local_file(audio_cache['path'])
                progress('CACHE_HIT audio')
            else:
                audio = media.extract_audio(source, destination, ffmpeg=tools['ffmpeg'], ffprobe=tools['ffprobe'])
                if audio is None:
                    raise ValueError('源文件含音轨，但提取没有返回音频。')
                audio_relative = audio.relative_to(destination).as_posix()
                cache.put('audio', {'path': audio_relative}, [audio_relative])
            manifest['processing']['asr'] = {'status': 'running', 'device': 'CPU', 'model_dir': str(model_dir)}
            checkpoint()
            with redirect_stdout(sys.stderr):
                manifest['transcript'] = validate_transcript(inference.transcribe(audio, model_dir, 'CPU', progress), metadata['duration_seconds'])
            status = 'transcribed' if any(s['text'].strip() for s in manifest['transcript']) else 'empty_transcript'
            provenance = model_dir / 'model_source.json'
            model_source = json.loads(provenance.read_text(encoding='utf-8')) if provenance.is_file() else {'local_path': str(model_dir)}
            manifest['processing']['asr'].update(status=status, model_source=model_source,
                segment_count=len(manifest['transcript']), full_audio_submitted=True)
        asr_status = manifest['processing']['asr']['status']
        if asr_status in ('no_audio', 'empty_transcript', 'imported_empty'):
            manifest['processing']['warnings'].append('未获得旁白内容；阅读覆盖记录，不将空转写当作完整识别。')
        save_transcript(transcript_path, manifest['transcript'], asr_status)
        if transcript_cache is None:
            cache.put('transcript', {'transcript': manifest['transcript'], 'asr': manifest['processing']['asr']}, ['analysis/transcript.txt'])
        timed('transcript', stage_start)
        manifest['processing']['status'] = 'asr_ready'
        checkpoint()
        progress(f'ASR_COMPLETE status={asr_status} segments={len(manifest["transcript"])} transcript={transcript_path}')

        stage_start = time.perf_counter()
        frames_cache = cache.get('frames')
        if frames_cache is not None:
            manifest['frames'], manifest['contact_sheets'] = frames_cache['frames'], frames_cache['contact_sheets']
            progress('CACHE_HIT frames：复用原图与联系表')
        else:
            cache.invalidate('frames', 'ocr')
            progress(f'提取全片覆盖与场景变化画面，最多 {options.max_frames} 帧')
            manifest['frames'] = media.extract_frames(source, destination, ffmpeg=tools['ffmpeg'], ffprobe=tools['ffprobe'],
                max_frames=options.max_frames, sample_interval=1.0)
            manifest['contact_sheets'] = contact_sheets(manifest['frames'], destination)
            cache.put('frames', {'frames': manifest['frames'], 'contact_sheets': manifest['contact_sheets']},
                [x['image'] for x in manifest['frames'] + manifest['contact_sheets']])
        timed('frames', stage_start)
        manifest['processing']['status'] = 'frames_ready'
        checkpoint()

        stage_start = time.perf_counter()
        if not options.no_ocr:
            ocr_cache = cache.get('ocr')
            if ocr_cache is not None:
                manifest['frames'], manifest['processing']['ocr'] = ocr_cache['frames'], ocr_cache['ocr']
                progress('CACHE_HIT ocr')
            else:
                with redirect_stdout(sys.stderr):
                    ocr = inference.add_ocr(manifest['frames'], destination, ROOT, progress)
                manifest['processing']['ocr'] = {'status': 'completed', **ocr}
                cache.put('ocr', {'frames': manifest['frames'], 'ocr': manifest['processing']['ocr']},
                          [x['image'] for x in manifest['frames']])
        timed('ocr', stage_start)
        checkpoint()

        stage_start = time.perf_counter()
        playback = cache.get('playback')
        if playback is not None:
            progress('CACHE_HIT playback')
        else:
            if resume:
                # A crash after media creation but before checkpoint commit, or
                # corrupted bytes, must not make explicit resume permanently fail.
                # Preserve only the two stage-owned targets in a local quarantine.
                for name in ('playback.mp4', 'source' + source.suffix.lower()):
                    existing = destination / name
                    if existing.exists():
                        if not existing.is_file() or existing.resolve() == source or not existing.resolve().is_relative_to(destination):
                            raise ValueError('播放缓存路径与输入冲突，请使用新输出目录。')
                        quarantine = destination / 'analysis' / 'superseded'
                        quarantine.mkdir(exist_ok=True)
                        existing.replace(quarantine / (uuid.uuid4().hex + '-' + name))
            progress('准备可本地播放的视频副本')
            playback = media.ensure_playback_video(source, destination, ffmpeg=tools['ffmpeg'], ffprobe=tools['ffprobe'], metadata=metadata)
            if playback['metadata']['source_sha256'] != source_hash:
                cache.invalidate(*tuple(cache.data['stages']))
                raise ValueError('播放副本与输入源不一致。')
            artifacts = [value for value in (playback['video'], playback['metadata'].get('source_attachment')) if value]
            cache.put('playback', playback, artifacts)
        manifest['video'] = playback['video']
        manifest['processing']['playback'] = playback['metadata']
        manifest['processing']['warnings'].extend(playback['warnings'])
        timed('playback', stage_start)
        if sha256(source) != source_hash:
            raise ValueError('处理期间源视频发生变化，请使用稳定文件重新生成材料。')
        manifest['processing']['status'] = 'complete'
        checkpoint()
        return {'ok': True, 'material_type': manifest['material_type'], 'output_dir': str(destination),
            'manifest': str(output), 'transcript': str(transcript_path), 'asr_status': asr_status,
            'frame_count': len(manifest['frames']), 'segment_count': len(manifest['transcript']),
            'elapsed_seconds': manifest['processing']['elapsed_seconds'], 'reused_stages': cache.reused,
            'warnings': manifest['processing']['warnings']}
    except Exception as exc:
        try:
            source_unchanged = sha256(source) == source_hash
        except OSError:
            source_unchanged = False
        if not source_unchanged:
            cache.invalidate(*tuple(cache.data['stages']))
        if manifest['processing']['asr']['status'] == 'running':
            manifest['processing']['asr']['status'] = 'failed'
        manifest['processing'].update(status='failed', error=str(exc))
        checkpoint()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="本地教学视频；沿用最多30分钟、2GiB限制")
    parser.add_argument("--output-dir", required=True, help="全新或空的输出目录；已有任务显式加 --resume")
    parser.add_argument("--max-frames", type=int, default=64, help="最多1至96张实际画面，默认64")
    parser.add_argument("--no-ocr", action="store_true")
    transcript = parser.add_mutually_exclusive_group()
    transcript.add_argument("--transcript-json", help="显式导入已有逐段转写，标记 imported 来源")
    transcript.add_argument("--subtitle-file", help="指定 SRT/VTT/JSON 字幕；时间覆盖不足时回退本地 ASR")
    transcript.add_argument("--no-subtitles", action="store_true", help="跳过同名字幕发现，强制本地 ASR")
    parser.add_argument("--resume", action="store_true", help="校验源视频、参数与产物哈希，复用完成的阶段")
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
