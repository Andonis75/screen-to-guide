"""Source-bound, checked stage checkpoints for explicit lesson resumption."""
from __future__ import annotations
import copy
import hashlib
import json
import tempfile
from pathlib import Path


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def atomic_text(path, text):
    # A fresh temporary file avoids following a stale/malicious .tmp symlink.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.material-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n')


class MaterialCache:
    def __init__(self, root, identity, *, resume=False):
        self.root = Path(root).resolve()
        self.path = self.root / 'analysis/material-cache.json'
        self.reused = []
        self.identity = identity
        self.data = {'version': 1, 'identity': identity, 'stages': {}}
        if resume:
            if not self.path.is_file() or self.path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError('此目录没有可恢复的处理记录；请使用新的输出目录。')
            try:
                value = json.loads(self.path.read_text(encoding='utf-8'))
            except (ValueError, OSError) as exc:
                raise ValueError('处理记录损坏；请使用新的输出目录。') from exc
            if value.get('version') != 1 or value.get('identity') != identity:
                raise ValueError('源视频、字幕或处理参数已改变；请使用新的输出目录，避免混用旧结果。')
            if not isinstance(value.get('stages'), dict):
                raise ValueError('处理记录的 stages 无效。')
            self.data = value
        else:
            self.save()

    def local_file(self, relative):
        if not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative:
            raise ValueError('缓存产物必须是目录内的相对路径。')
        path = Path(relative)
        resolved = (self.root / path).resolve()
        if path.is_absolute() or '..' in path.parts or not resolved.is_relative_to(self.root):
            raise ValueError('缓存产物路径越出输出目录。')
        if (self.root / path).is_symlink():
            raise ValueError('缓存产物不接受符号链接。')
        return resolved

    def save(self):
        atomic_json(self.path, self.data)

    def invalidate(self, *names):
        for name in names:
            self.data['stages'].pop(name, None)
        self.save()

    def get(self, name):
        entry = self.data['stages'].get(name)
        if not isinstance(entry, dict):
            return None
        try:
            serialized = json.dumps(entry['data'], ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')
            if hashlib.sha256(serialized).hexdigest() != entry['data_sha256']:
                return None
            paths = {item['path'] for item in entry['artifacts']}
            if name in {'frames', 'ocr'}:
                required = [frame['image'] for frame in entry['data']['frames']]
                if name == 'frames':
                    required += [sheet['image'] for sheet in entry['data']['contact_sheets']]
                if any(path not in paths for path in required):
                    return None
            if name == 'audio' and entry['data']['path'] not in paths:
                return None
            if name == 'playback' and entry['data'].get('video') and entry['data']['video'] not in paths:
                return None
            if name == 'playback':
                attachment = entry['data'].get('metadata', {}).get('source_attachment')
                if attachment and attachment not in paths:
                    return None
            for artifact in entry['artifacts']:
                path = self.local_file(artifact['path'])
                if not path.is_file() or path.stat().st_size != artifact['bytes'] or digest(path) != artifact['sha256']:
                    return None
        except (ValueError, KeyError, TypeError, OSError):
            return None
        self.reused.append(name)
        return copy.deepcopy(entry['data'])

    def put(self, name, data, artifacts=()):
        files = []
        for relative in dict.fromkeys(artifacts):
            path = self.local_file(relative)
            if not path.is_file():
                raise ValueError(f'处理产物未写入：{relative}')
            files.append({'path': relative, 'bytes': path.stat().st_size, 'sha256': digest(path)})
        serialized = json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')
        self.data['stages'][name] = {'data': copy.deepcopy(data), 'data_sha256': hashlib.sha256(serialized).hexdigest(), 'artifacts': files}
        self.save()
