"""Acquisition behavior checks; no test makes network requests."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import acquire_video as acquisition


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / '.runtime/cache/tmp'
        cache.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='acquisition-test-', dir=cache)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.options = SimpleNamespace(url='https://example.com/watch?id=sample#player', output_dir=str(self.root / 'download'), resume=False, subtitle_langs='zh,en')
        self.info = {'_type': 'video', 'id': 'sample', 'title': 'Example', 'uploader': 'Author', 'duration': 12,
                     'height': 1080, 'width': 1920, 'ext': 'mp4', 'vcodec': 'h264', 'acodec': 'aac',
                     'subtitles': {'zh-Hans': [{'ext': 'vtt', 'url': 'https://cdn.invalid/sub?token=private'}]},
                     'automatic_captions': {'zh': [{'ext': 'json3'}]}}
        self.probe = {'duration_seconds': 12, 'width': 1920, 'height': 1080, 'has_audio': True}
        self.calls = []
        self.subtitle_failure = False
        self.subtitle_failure_after_write = False
        self.subtitle_text = 'WEBVTT\n\n00:00.000 --> 00:12.000\n示例文字\n'
        self.video_failure = False
        self.video_writes = 0

    def factory(self, options):
        parent = self
        parent.calls.append(options)

        class FakeYDL:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                parent.assertFalse(download)
                return copy.deepcopy(parent.info)

            def process_ie_result(self, info, download=True):
                destination = Path(options['paths']['home'])
                if options.get('skip_download'):
                    if parent.subtitle_failure:
                        raise RuntimeError('403 https://cdn.invalid/sub?token=secret')
                    subtitle = destination / 'media.zh-Hans.vtt'
                    # yt-dlp's existing_file behavior must be represented here:
                    # an existing completed filename is reused without redownload.
                    if not subtitle.exists():
                        subtitle.write_text(parent.subtitle_text, encoding='utf-8')
                    if parent.subtitle_failure_after_write:
                        raise RuntimeError('subtitle stage interrupted after writing')
                    info['requested_subtitles'] = {'zh-Hans': {'ext': 'vtt', 'filepath': str(subtitle)}}
                    return info
                if parent.video_failure:
                    (destination / 'media.mp4.part').write_bytes(b'partial')
                    raise RuntimeError('network connection interrupted')
                video = destination / 'media.mp4'
                if not video.exists():
                    video.write_bytes(b'complete-video-fixture')
                    parent.video_writes += 1
                info['requested_downloads'] = [{'filepath': str(video)}]
                return info
        return FakeYDL()

    def run_acquire(self, factory=None):
        with patch.object(acquisition.shutil, 'which', side_effect=lambda name: name), \
             patch.object(acquisition.media, 'probe_media', return_value=self.probe), \
             patch.object(acquisition.media, '_run'):
            return acquisition.acquire(self.options, factory or self.factory)

    def test_public_url_and_language_validation(self):
        self.assertEqual(acquisition.public_url(self.options.url), 'https://example.com/watch?id=sample')
        for url in ('file:///private.mp4', 'https://user:password@example.com', 'http://127.0.0.1/video', 'http://localhost/video', 'https://example.com\n/header'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                acquisition.public_url(url)
        self.assertEqual(acquisition.languages('zh,en,zh'), ['zh', 'en'])
        for value in ('', 'all', 'zh,.*', '../zh', 'zh|en'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                acquisition.languages(value)

    def test_subtitle_preference_uses_chinese_human_then_chinese_auto_then_english(self):
        self.assertEqual(acquisition.choose_subtitle(self.info, ['zh', 'en']), {'language': 'zh-Hans', 'kind': 'human'})
        self.info['subtitles'] = {'en': [{'ext': 'srt'}]}
        self.assertEqual(acquisition.choose_subtitle(self.info, ['zh', 'en']), {'language': 'zh', 'kind': 'automatic'})
        self.info['automatic_captions'] = {}
        self.assertEqual(acquisition.choose_subtitle(self.info, ['zh', 'en']), {'language': 'en', 'kind': 'human'})
        self.info['subtitles'] = {'zh': [{'ext': 'ass'}]}
        self.assertIsNone(acquisition.choose_subtitle(self.info, ['zh', 'en']))

    def test_metadata_rejects_playlist_live_unbounded_drm_and_oversized(self):
        for changes in ({'_type': 'playlist'}, {'entries': []}, {'is_live': True}, {'has_drm': True},
                        {'duration': None}, {'duration': float('nan')}, {'duration': 1801},
                        {'filesize': acquisition.media.MAX_FILE_BYTES + 1}, {'height': 2160}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                acquisition.validate_info(dict(self.info, **changes))

    def test_video_and_subtitles_have_bound_provenance_without_asset_urls(self):
        result = self.run_acquire()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['subtitle']['kind'], 'human')
        self.assertEqual(result['subtitle']['path'], 'media.zh-Hans.vtt')
        self.assertEqual(result['video']['height'], 1080)
        self.assertEqual(result['video']['sha256'], acquisition.sha256(Path(self.options.output_dir) / 'media.mp4'))
        serialized = (Path(self.options.output_dir) / 'acquisition-status.json').read_text(encoding='utf-8')
        self.assertNotIn('cdn.invalid', serialized)
        self.assertNotIn('private', serialized)
        self.assertFalse(self.calls[0]['writesubtitles'])
        self.assertTrue(self.calls[1]['skip_download'])
        self.assertNotIn('cookiesfrombrowser', self.calls[0])
        self.assertFalse(self.calls[0]['skip_unavailable_fragments'])

    def test_subtitle_failure_keeps_verified_video_and_redacts_remote_url(self):
        self.subtitle_failure = True
        result = self.run_acquire()
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(result['video']['decode_passed'])
        self.assertEqual(result['subtitle']['fallback'], 'local_asr')
        self.assertNotIn('token=secret', result['subtitle']['reason'])

    def test_real_ytdlp_promotes_subtitle_after_postprocessor_moves_it(self):
        try:
            from yt_dlp import YoutubeDL
        except ImportError:
            self.skipTest('Optional requirements-download.txt dependency is not installed')
        self.info.update(url='https://fixture.invalid/video.mp4', extractor='fixture', extractor_key='Fixture',
                         subtitles={'zh-Hans': [{'ext': 'vtt', 'data': self.subtitle_text}]})
        observed = []

        class OfflineYDL(YoutubeDL):
            def urlopen(self, *args, **kwargs):
                raise AssertionError('This regression must not access the network')

            def process_ie_result(self, *args, **kwargs):
                result = super().process_ie_result(*args, **kwargs)
                stale = Path(result['requested_subtitles']['zh-Hans']['filepath'])
                final = Path(self.params['paths']['home']) / 'media.zh-Hans.vtt'
                observed.append((stale.parent.name, stale.exists(), final.is_file()))
                return result

        def factory(options):
            return OfflineYDL(options) if options.get('skip_download') else self.factory(options)

        result = self.run_acquire(factory)
        self.assertEqual(observed, [('temp', False, True)])
        self.assertEqual(result['subtitle']['status'], 'downloaded')
        path = Path(self.options.output_dir) / result['subtitle']['path']
        self.assertEqual(path.read_text(encoding='utf-8'), self.subtitle_text)
        self.assertEqual(acquisition.subtitles.find_subtitles(path.parent / 'media.mp4'), [path])

    def test_missing_subtitles_still_produces_complete_video(self):
        self.info['subtitles'] = self.info['automatic_captions'] = {}
        result = self.run_acquire()
        self.assertEqual(result['subtitle']['status'], 'unavailable')
        self.assertEqual(len(self.calls), 1)

    def test_explicit_resume_of_completed_output_skips_network_and_detects_changed_options(self):
        first = self.run_acquire()
        self.options.resume = True
        count = len(self.calls)
        resumed = self.run_acquire()
        self.assertTrue(resumed['reused'])
        self.assertEqual(resumed['video']['sha256'], first['video']['sha256'])
        self.assertEqual(len(self.calls), count)
        self.options.subtitle_langs = 'en'
        with self.assertRaisesRegex(ValueError, '选项不同'):
            self.run_acquire()

    def test_existing_material_is_never_silently_reused(self):
        self.run_acquire()
        with self.assertRaisesRegex(ValueError, '--resume'):
            self.run_acquire()
        unknown = self.root / 'unknown'
        unknown.mkdir()
        (unknown / 'keep.txt').write_text('keep')
        self.options.output_dir = str(unknown)
        self.options.resume = True
        with self.assertRaisesRegex(ValueError, '缺少有效下载记录'):
            self.run_acquire()
        self.assertEqual((unknown / 'keep.txt').read_text(), 'keep')

    def test_interrupted_download_preserves_partial_and_resumes_same_task(self):
        self.video_failure = True
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            self.run_acquire()
        destination = Path(self.options.output_dir)
        state = json.loads((destination / 'acquisition-status.json').read_text(encoding='utf-8'))
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['phase'], 'video_download')
        self.assertTrue((destination / 'media.mp4.part').is_file())
        self.video_failure = False
        self.options.resume = True
        self.assertEqual(self.run_acquire()['status'], 'complete')

    def test_duration_mismatch_or_missing_audio_never_claims_complete(self):
        for probe in (dict(self.probe, duration_seconds=4), dict(self.probe, has_audio=False)):
            with self.subTest(probe=probe):
                self.options.output_dir = str(self.root / ('case-' + str(len(self.calls))))
                self.probe = probe
                with self.assertRaises(ValueError):
                    self.run_acquire()
                state = json.loads((Path(self.options.output_dir) / 'acquisition-status.json').read_text(encoding='utf-8'))
                self.assertEqual(state['status'], 'failed')
                self.assertEqual(state['phase'], 'verification')

    def test_decode_failure_preserves_file_but_never_marks_success(self):
        with patch.object(acquisition.shutil, 'which', side_effect=lambda name: name), \
             patch.object(acquisition.media, 'probe_media', return_value=self.probe), \
             patch.object(acquisition.media, '_run', side_effect=acquisition.media.MediaError('decode error')):
            with self.assertRaisesRegex(ValueError, 'decode error'):
                acquisition.acquire(self.options, self.factory)
        directory = Path(self.options.output_dir)
        state = json.loads((directory / 'acquisition-status.json').read_text(encoding='utf-8'))
        self.assertEqual(state['status'], 'failed')
        self.assertNotIn('video', state)
        self.assertTrue((directory / 'media.mp4').is_file())

    def test_progress_bound_counts_separate_audio_and_video_files(self):
        self.run_acquire()
        callback = self.calls[0]['progress_hooks'][0]
        callback({'filename': 'video.mp4', 'downloaded_bytes': acquisition.media.MAX_FILE_BYTES - 10})
        with self.assertRaisesRegex(ValueError, '2GiB'):
            callback({'filename': 'audio.m4a', 'downloaded_bytes': 20})
        subtitle_callback = self.calls[1]['progress_hooks'][0]
        with self.assertRaisesRegex(ValueError, '16MiB'):
            subtitle_callback({'downloaded_bytes': acquisition.MAX_SUBTITLE_BYTES + 1})

    def test_hash_changed_completed_media_is_preserved_then_refetched(self):
        first = self.run_acquire()
        directory = Path(self.options.output_dir)
        changed = b'changed-valid-media-same-duration'
        (directory / 'media.mp4').write_bytes(changed)
        self.options.resume = True
        second = self.run_acquire()
        self.assertEqual(self.video_writes, 2)
        self.assertEqual(second['video']['sha256'], first['video']['sha256'])
        self.assertFalse(second['video']['reused_verified_video'])
        preserved = [item for item in second['quarantined_files'] if item['original_path'] == 'media.mp4']
        self.assertEqual(len(preserved), 1)
        self.assertEqual((directory / preserved[0]['preserved_path']).read_bytes(), changed)

    def test_orphan_completed_video_is_not_accepted_but_partial_fragments_stay(self):
        self.video_failure = True
        with self.assertRaises(RuntimeError):
            self.run_acquire()
        directory = Path(self.options.output_dir)
        (directory / 'media.mp4').write_bytes(b'orphan completed file')
        self.video_failure = False
        self.options.resume = True
        result = self.run_acquire()
        self.assertEqual((directory / 'media.mp4').read_bytes(), b'complete-video-fixture')
        self.assertEqual((directory / 'media.mp4.part').read_bytes(), b'partial')
        self.assertTrue(any(item['original_path'] == 'media.mp4' for item in result['quarantined_files']))

    def test_failed_subtitle_output_stays_in_staging_and_is_not_discovered(self):
        self.subtitle_failure_after_write = True
        result = self.run_acquire()
        directory = Path(self.options.output_dir)
        self.assertEqual(result['subtitle']['status'], 'failed')
        self.assertEqual(acquisition.subtitles.find_subtitles(directory / 'media.mp4'), [])
        self.assertEqual(len(list((directory / 'analysis/subtitle-staging').rglob('media.zh-Hans.vtt'))), 1)

    def test_partial_subtitle_is_not_promoted_after_successful_transfer(self):
        self.subtitle_text = 'WEBVTT\n\n00:00.000 --> 00:01.000\n开头\n'
        result = self.run_acquire()
        directory = Path(self.options.output_dir)
        self.assertEqual(result['subtitle']['status'], 'failed')
        self.assertEqual(result['subtitle']['fallback'], 'local_asr')
        self.assertEqual(acquisition.subtitles.find_subtitles(directory / 'media.mp4'), [])

    def test_tampered_cached_subtitle_is_quarantined_even_if_refetch_fails(self):
        first = self.run_acquire()
        directory = Path(self.options.output_dir)
        changed = 'WEBVTT\n\n00:00.000 --> 00:12.000\n替换后的文字\n'
        (directory / 'media.zh-Hans.vtt').write_text(changed, encoding='utf-8')
        self.options.resume = True
        self.subtitle_failure = True
        second = self.run_acquire()
        self.assertEqual(self.video_writes, 1)
        self.assertEqual(second['video']['sha256'], first['video']['sha256'])
        self.assertTrue(second['video']['reused_verified_video'])
        self.assertEqual(second['subtitle']['fallback'], 'local_asr')
        self.assertEqual(acquisition.subtitles.find_subtitles(directory / 'media.mp4'), [])
        saved = next(item for item in second['quarantined_files'] if item['original_path'] == 'media.zh-Hans.vtt')
        self.assertEqual((directory / saved['preserved_path']).read_text(encoding='utf-8'), changed)

    def test_legacy_failed_sidecar_invalidates_fast_cache_and_is_removed_from_discovery(self):
        self.subtitle_failure = True
        first = self.run_acquire()
        self.assertEqual(first['status'], 'complete')
        directory = Path(self.options.output_dir)
        (directory / 'media.zh.vtt').write_text(self.subtitle_text, encoding='utf-8')
        self.options.resume = True
        second = self.run_acquire()
        self.assertFalse(second['reused'])
        self.assertEqual(acquisition.subtitles.find_subtitles(directory / 'media.mp4'), [])
        self.assertTrue(any(item['original_path'] == 'media.zh.vtt' for item in second['quarantined_files']))


if __name__ == '__main__':
    unittest.main()
