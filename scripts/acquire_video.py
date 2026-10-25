"""Download one complete public video and an available subtitle sidecar.

This optional network step is separate from the offline analysis pipeline.
Install requirements-download.txt explicitly; this script never installs tools,
reads browser cookies, logs in, or treats an incomplete download as a video.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

from screen_guide import media, subtitles

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / '.runtime' / 'venv'
VENV_PYTHON = VENV / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
FORMAT = 'bv*[height<=1080]+ba/b[height<=1080]'
SUBTITLE_FORMATS = {'vtt', 'srt', 'json3'}
VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.webm', '.mov', '.m4v'}
MAX_SUBTITLE_BYTES = 16 * 1024 * 1024


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                         prefix='.' + path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def public_url(value):
    if not isinstance(value, str) or re.search(r'[\s\x00-\x1f\x7f]', value):
        raise ValueError('请输入完整的公开 HTTP(S) 视频页面链接。')
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        parsed.port
        if parsed.scheme.lower() not in {'http', 'https'} or not host or parsed.username is not None or parsed.password is not None:
            raise ValueError()
        if host.lower() in {'localhost', 'localhost.localdomain'} or host.lower().endswith('.local'):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
    except ValueError as exc:
        raise ValueError('下载入口只接受不带账号密码的公开 HTTP(S) 页面链接。') from exc
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or '/', parsed.query, ''))


def languages(value):
    items = [item.strip().lower() for item in value.split(',') if item.strip()]
    if not items or len(items) > 8 or any(item == 'all' or not re.fullmatch(r'[a-z]{2,3}(?:-[a-z0-9]{2,8})*', item) for item in items):
        raise ValueError('--subtitle-langs 使用语言代码，例如 zh,en 或 zh-cn,en。')
    return list(dict.fromkeys(items))


def safe_error(exc):
    # Asset URLs can contain signed credentials; keep only a bounded diagnosis.
    return re.sub(r'https?://[^\s\]<>"\']+', '[远程资源地址]', str(exc))[-1200:]


class Logger:
    def debug(self, message):
        pass

    def info(self, message):
        pass

    def warning(self, message):
        print('下载提示：' + safe_error(message), file=sys.stderr, flush=True)

    def error(self, message):
        print('下载错误：' + safe_error(message), file=sys.stderr, flush=True)


def validate_info(info):
    if not isinstance(info, dict) or info.get('_type', 'video') != 'video' or 'entries' in info:
        raise ValueError('请选择单条视频页面；当前入口不批量下载播放列表或多集内容。')
    if info.get('is_live') or info.get('live_status') in {'is_live', 'is_upcoming'}:
        raise ValueError('请使用已经结束且有完整时长的视频，直播不在此入口范围。')
    if info.get('has_drm'):
        raise ValueError('该媒体标记为 DRM 内容，请提供可正常读取的本地视频。')
    duration = info.get('duration')
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 < duration <= media.MAX_DURATION_SECONDS:
        raise ValueError('视频需有明确时长且不超过30分钟；较长课程请先分段。')
    selected = info.get('requested_formats') or [info]
    sizes = [row.get('filesize') or row.get('filesize_approx') or 0 for row in selected]
    if sum(value for value in sizes if isinstance(value, (int, float)) and math.isfinite(value)) > media.MAX_FILE_BYTES:
        raise ValueError('所选视频预计超过2GiB，请提供较小版本。')
    for row in selected:
        if row.get('has_drm'):
            raise ValueError('所选格式标记为 DRM 内容。')
        if isinstance(row.get('height'), (int, float)) and row['height'] > 1080:
            raise ValueError('所选格式超过此入口1080p上限。')
    return {'id': str(info.get('id', '')), 'title': str(info.get('title', '')),
            'author': str(info.get('uploader') or info.get('channel') or ''),
            'declared_duration_seconds': duration,
            'selected_formats': [{key: row.get(key) for key in ('format_id', 'ext', 'width', 'height', 'fps', 'vcodec', 'acodec')} for row in selected]}


def choose_subtitle(info, preferred):
    """Language priority first; human captions beat automatic in that language."""
    for language in preferred:
        for kind, key in (('human', 'subtitles'), ('automatic', 'automatic_captions')):
            available = info.get(key) or {}
            for name in sorted(available, key=lambda name: (name.lower() != language, name.lower())):
                normalized = name.lower().replace('_', '-')
                if not (normalized == language or normalized.startswith(language + '-')):
                    continue
                if not re.fullmatch(r'[A-Za-z0-9_-]{2,40}', name):
                    continue
                if any(item.get('ext') in SUBTITLE_FORMATS for item in available[name] if isinstance(item, dict)):
                    return {'language': name, 'kind': kind}
    return None


def is_reparse(path):
    """Reject symlinks and Windows junction/reparse points before any writer."""
    try:
        details = Path(path).lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(details.st_mode) or bool(getattr(details, 'st_file_attributes', 0) & 0x400)


def contained_path(root, value):
    path = Path(value)
    path = path if path.is_absolute() else root / path
    if '..' in path.parts:
        raise ValueError('下载文件路径包含上级目录。')
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError('下载文件不在本次输出目录内。') from exc
    current = root
    for part in ('', *relative.parts):
        current = current / part
        if is_reparse(current):
            raise ValueError('下载文件及其父目录不能使用符号链接或重解析点。')
    if not path.resolve().is_relative_to(root):
        raise ValueError('下载路径越过本次输出目录。')
    return path


def ensure_directory(root, relative):
    path = contained_path(root, relative)
    path.mkdir(parents=True, exist_ok=True)
    return path


def check_writer_tree(root, relative):
    """Existing download/cache children must not redirect subsequent writers."""
    directory = ensure_directory(root, relative)
    count = 0
    for current, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs + files:
            count += 1
            if count > 10000:
                raise ValueError('下载临时目录内容过多，请换新目录。')
            contained_path(root, Path(current) / name)
    return directory


def managed_files(root, extensions):
    return [contained_path(root, path) for path in root.glob('media.*')
            if path.suffix.lower() in extensions and path.is_file()]


def verified_previous_video(root, previous):
    try:
        item = previous['video']
        if not item.get('decode_passed'):
            return None
        path = local_file(root, item['path'])
        if path.parent != root or path.suffix.lower() not in VIDEO_EXTENSIONS:
            return None
        if not 0 < path.stat().st_size <= media.MAX_FILE_BYTES or path.stat().st_size != item['bytes'] or sha256(path) != item['sha256']:
            return None
        return path
    except (OSError, ValueError, KeyError, TypeError):
        return None


def quarantine(root, path, reason):
    """Preserve changed/orphan completed artifacts; never touch .part fragments."""
    source = local_file(root, path)
    directory = ensure_directory(root, 'analysis/superseded')
    target = contained_path(root, directory / (uuid.uuid4().hex[:12] + '-' + source.name))
    source.replace(target)
    return {'original_path': source.name, 'preserved_path': target.relative_to(root).as_posix(), 'reason': reason}


def output_directory(path, signature, resume):
    requested = Path(path).expanduser()
    if is_reparse(requested):
        raise ValueError('下载目录不能是符号链接。')
    root = requested.resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        state_file = root / 'acquisition-status.json'
        if not resume:
            raise ValueError('输出目录已有内容。继续同一任务请显式加 --resume，或换新目录。')
        if not state_file.is_file() or is_reparse(state_file) or state_file.stat().st_size > 1024 * 1024:
            raise ValueError('已有目录缺少有效下载记录；请使用新目录。')
        state = json.loads(state_file.read_text(encoding='utf-8'))
        if state.get('signature') != signature:
            raise ValueError('已有记录的视频链接或下载选项不同；请使用新目录。')
        return root, state
    root.mkdir(parents=True, exist_ok=True)
    return root, None


def local_file(root, value):
    path = contained_path(root, value)
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError('下载器返回的文件不在本次输出目录内，或文件尚未完成。')
    return resolved


def cached_complete(root, previous):
    if not previous or previous.get('status') != 'complete' or not previous.get('video', {}).get('decode_passed'):
        return False
    try:
        path = verified_previous_video(root, previous)
        if path is None or set(managed_files(root, VIDEO_EXTENSIONS)) != {path}:
            return False
        caption = previous.get('subtitle', {})
        expected_subtitles = set()
        if caption.get('status') == 'downloaded':
            subtitle = local_file(root, caption['path'])
            if subtitle.parent != root or not 0 < subtitle.stat().st_size <= MAX_SUBTITLE_BYTES or sha256(subtitle) != caption['sha256']:
                return False
            checked = subtitles.load_subtitles(subtitle, previous['video']['duration_seconds'], previous['video']['sha256'])
            if not checked['metadata']['usable']:
                return False
            expected_subtitles.add(subtitle)
        if set(managed_files(root, {'.' + extension for extension in SUBTITLE_FORMATS})) != expected_subtitles:
            return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def acquire(options, ydl_factory=None):
    started = time.monotonic()
    url = public_url(options.url)
    preferred = languages(getattr(options, 'subtitle_langs', 'zh,en'))
    signature = {'schema': 1, 'url': url, 'format': FORMAT, 'subtitle_languages': preferred}
    root, previous = output_directory(options.output_dir, signature, bool(getattr(options, 'resume', False)))
    if any(is_reparse(path) for path in root.glob('media.*')):
        raise ValueError('本次下载文件不能是符号链接；请换新目录。')
    state_file = root / 'acquisition-status.json'
    if cached_complete(root, previous):
        result = dict(previous, reused=True, last_resume_at=datetime.now(timezone.utc).isoformat())
        write_json(state_file, result)
        return result
    state = {'schema_version': 1, 'signature': signature, 'source_url': url, 'status': 'running',
             'phase': 'setup', 'started_at': datetime.now(timezone.utc).isoformat(),
             'reused': False, 'warnings': [], 'subtitle': {'status': 'pending'}}
    write_json(state_file, state)
    old_temporary = {key: os.environ.get(key) for key in ('TEMP', 'TMP')}
    old_tempdir = tempfile.tempdir

    def checkpoint(phase):
        state['phase'] = phase
        state['elapsed_seconds'] = round(time.monotonic() - started, 3)
        write_json(state_file, state)

    try:
        ffmpeg = shutil.which('ffmpeg')
        ffprobe = shutil.which('ffprobe')
        if not ffmpeg or not ffprobe:
            raise ValueError('请先把本地 FFmpeg 与 ffprobe 加入 PATH。')
        if ydl_factory is None:
            try:
                from yt_dlp import YoutubeDL
            except ImportError as exc:
                raise ValueError('下载依赖尚未准备，请显式安装 requirements-download.txt；本地视频可直接进入离线处理。') from exc
            ydl_factory = YoutubeDL
        for name in ('temp', 'cache'):
            check_writer_tree(root, name)
        os.environ['TEMP'] = os.environ['TMP'] = str(root / 'temp')
        tempfile.tempdir = str(root / 'temp')
        # yt-dlp otherwise skips any existing completed filename, even if our
        # recorded checksum failed. Only an independently verified prior video
        # may stay in place; orphan/tampered outputs are preserved elsewhere.
        verified_video = verified_previous_video(root, previous)
        state['quarantined_files'] = []
        for path in managed_files(root, VIDEO_EXTENSIONS):
            if path != verified_video:
                state['quarantined_files'].append(quarantine(root, path, 'completed video has no matching verified checksum'))
        # A rerun's captions must come from its successful subtitle stage.
        # This also removes legacy failed sidecars from automatic discovery.
        for path in managed_files(root, {'.' + extension for extension in SUBTITLE_FORMATS}):
            state['quarantined_files'].append(quarantine(root, path, 'subtitle requires a fresh validated download'))
        checkpoint('setup')
        totals = {}

        def bound_progress(event):
            size = event.get('downloaded_bytes', 0) or 0
            name = str(event.get('filename') or event.get('tmpfilename') or 'media')
            totals[name] = max(totals.get(name, 0), size)
            if sum(totals.values()) > media.MAX_FILE_BYTES:
                raise ValueError('累计媒体下载超过2GiB，保留已有部分文件。')
            if time.monotonic() - started > 3600:
                raise ValueError('本次下载已超过一小时，保留部分文件供下次继续。')

        common = {'paths': {'home': str(root), 'temp': str(root / 'temp')},
                  'outtmpl': 'media.%(ext)s', 'noplaylist': True, 'format': FORMAT,
                  'merge_output_format': 'mkv', 'continuedl': True, 'nopart': False,
                  'retries': 2, 'fragment_retries': 2, 'extractor_retries': 1,
                  'socket_timeout': 15, 'skip_unavailable_fragments': False,
                  'max_filesize': media.MAX_FILE_BYTES, 'cachedir': str(root / 'cache'),
                  'ffmpeg_location': str(Path(ffmpeg).parent),
                  'quiet': True, 'noprogress': True, 'logger': Logger(),
                  'writesubtitles': False, 'writeautomaticsub': False,
                  'progress_hooks': [bound_progress]}
        checkpoint('metadata')
        with ydl_factory(common) as ydl:
            info = ydl.extract_info(url, download=False)
            state['source'] = validate_info(info)
            caption_choice = choose_subtitle(info, preferred)
            checkpoint('video_download')
            downloaded = ydl.process_ie_result(info, download=True)
            candidates = [entry.get('filepath') for entry in downloaded.get('requested_downloads', []) if isinstance(entry, dict)]
            candidates.append(downloaded.get('filepath'))
            # The fixed output stem also covers extractor/postprocessor variants.
            candidates.extend(str(path) for path in root.glob('media.*') if path.suffix.lower() in VIDEO_EXTENSIONS)
            videos = {}
            for name in candidates:
                if name and Path(name).suffix.lower() in VIDEO_EXTENSIONS:
                    try:
                        path = local_file(root, name)
                    except ValueError:
                        continue
                    videos[str(path)] = path
            if len(videos) != 1:
                raise ValueError('未得到唯一已完成的合并视频；保留现有文件供检查。')
            source = next(iter(videos.values()))
        checkpoint('verification')
        probe = media.probe_media(source, ffprobe)
        expected = state['source']['declared_duration_seconds']
        if abs(probe['duration_seconds'] - expected) > max(3.0, expected * .01):
            raise ValueError(f'完整性检查失败：页面时长 {expected:g} 秒，文件时长 {probe["duration_seconds"]:g} 秒。')
        if probe['height'] > 1080:
            raise ValueError('下载视频超过1080p上限。')
        expected_audio = any(row.get('acodec') not in {None, 'none'} for row in state['source']['selected_formats'])
        if expected_audio and not probe['has_audio']:
            raise ValueError('页面所选格式有音轨，但下载文件没有音轨。')
        media._run([ffmpeg, '-hide_banner', '-nostdin', '-v', 'error', '-xerror', '-protocol_whitelist', 'file,pipe',
                    '-i', str(source), '-map', '0:V:0', '-map', '0:a?', '-f', 'null', '-'],
                   timeout=min(1800, max(60, expected * 1.5)))
        state['video'] = {'path': source.name, 'sha256': sha256(source), 'bytes': source.stat().st_size,
                          'reused_verified_video': source == verified_video,
                          'decode_passed': True, **probe}
        checkpoint('subtitles')
        state['subtitle'] = {'status': 'unavailable', 'fallback': 'local_asr',
                             'reason': '当前页面未返回匹配语言的可下载字幕；画面内烧录字幕不等于字幕文件。'}
        if caption_choice:
            try:
                staging = ensure_directory(root, 'analysis/subtitle-staging/' + uuid.uuid4().hex[:12])
                ensure_directory(root, staging.relative_to(root) / 'temp')
                def bound_subtitle(event):
                    if (event.get('downloaded_bytes', 0) or 0) > MAX_SUBTITLE_BYTES:
                        raise ValueError('字幕超过16MiB上限。')
                subtitle_key = 'subtitles' if caption_choice['kind'] == 'human' else 'automatic_captions'
                for entry in (info.get(subtitle_key) or {}).get(caption_choice['language'], []):
                    if entry.get('ext') in SUBTITLE_FORMATS and isinstance(entry.get('data'), str) and len(entry['data'].encode('utf-8')) > MAX_SUBTITLE_BYTES:
                        raise ValueError('字幕超过16MiB上限。')
                subtitle_options = dict(common, skip_download=True, progress_hooks=[bound_subtitle], max_filesize=MAX_SUBTITLE_BYTES,
                                        paths={'home': str(staging), 'temp': str(staging / 'temp')},
                                        writesubtitles=caption_choice['kind'] == 'human',
                                        writeautomaticsub=caption_choice['kind'] == 'automatic',
                                        subtitleslangs=[caption_choice['language']], subtitlesformat='vtt/srt/json3')
                with ydl_factory(subtitle_options) as ydl:
                    subtitle_result = ydl.process_ie_result(copy.deepcopy(info), download=True)
                requested = subtitle_result.get('requested_subtitles') or {}
                selected = requested.get(caption_choice['language']) or {}
                ext = selected.get('ext')
                if ext not in SUBTITLE_FORMATS:
                    raise ValueError('没有取得受支持的字幕格式。')
                expected_name = f'media.{caption_choice["language"]}.{ext}'
                # yt-dlp can move a sidecar from temp to home without updating
                # requested_subtitles.filepath. Resolve only our two fixed paths.
                candidates = [contained_path(staging, expected_name),
                              contained_path(staging, Path('temp') / expected_name)]
                completed = [path for path in candidates if path.is_file()]
                if len(completed) != 1:
                    raise ValueError('未得到唯一已完成的预期字幕文件。')
                caption = local_file(staging, completed[0])
                if caption.name != expected_name or not 0 < caption.stat().st_size <= MAX_SUBTITLE_BYTES:
                    raise ValueError('字幕文件名或大小不符合单视频字幕要求。')
                checked = subtitles.load_subtitles(caption, probe['duration_seconds'], state['video']['sha256'])
                if not checked['metadata']['usable']:
                    raise ValueError(checked['metadata']['reason'])
                caption_hash = checked['metadata']['sha256']
                caption_bytes = caption.stat().st_size
                final_caption = contained_path(root, expected_name)
                if final_caption.exists():
                    raise ValueError('字幕目标文件已经存在，保留暂存结果，不覆盖该文件。')
                caption.replace(final_caption)
                state['subtitle'] = {'status': 'downloaded', **caption_choice, 'path': final_caption.name,
                                     'sha256': caption_hash, 'bytes': caption_bytes,
                                     'format': ext, 'coverage': checked['metadata']['coverage'],
                                     'completeness': '通过解析与时间覆盖基础检查；不代表字幕文字已全部核实。'}
            except Exception as exc:
                state['subtitle'] = {'status': 'failed', **caption_choice, 'fallback': 'local_asr', 'reason': safe_error(exc)}
                state['warnings'].append('视频已完整取得，但字幕获取失败；后续使用本地语音转写。')
        state.update(status='complete', completed_at=datetime.now(timezone.utc).isoformat())
        checkpoint('complete')
        return state
    except Exception as exc:
        state.update(status='failed', error=safe_error(exc))
        checkpoint(state['phase'])
        raise
    finally:
        tempfile.tempdir = old_tempdir
        for key, value in old_temporary.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url', help='公开单视频页面 HTTP(S) 链接')
    parser.add_argument('--output-dir', required=True, help='全新或空目录；下载和缓存保留在这里')
    parser.add_argument('--resume', action='store_true', help='继续同一链接、同一选项的已记录下载')
    parser.add_argument('--subtitle-langs', default='zh,en', help='字幕语言优先级，默认 zh,en')
    options = parser.parse_args(argv)
    try:
        result = acquire(options)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': safe_error(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    if VENV_PYTHON.is_file() and Path(sys.prefix).resolve() != VENV.resolve():
        raise SystemExit(subprocess.call([str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]]))
    raise SystemExit(main())
