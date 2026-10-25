"""Observable reuse/recovery behavior, with real output files and bounded mocked engines."""
import json
import os
from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import prepare_lesson as lesson
from screen_guide.lesson_cache import MaterialCache


class ResumeTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / '.runtime/cache/tmp'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'clip.mp4'
        self.source.write_bytes(b'original video')
        self.subtitle = self.root / 'clip.zh.srt'
        self.subtitle.write_text('1\n00:00:00,000 --> 00:00:10,000\n教学字幕\n', encoding='utf-8')
        self.output = self.root / 'out'
        self.options = SimpleNamespace(input=str(self.source), output_dir=str(self.output), max_frames=2,
                                      no_ocr=False, transcript_json=None, resume=False)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(lesson.shutil, 'which', return_value='tool'))
        self.stack.enter_context(patch.object(lesson.media, 'probe_media', return_value={'has_audio': True, 'duration_seconds': 10}))
        self.audio = self.stack.enter_context(patch.object(lesson.media, 'extract_audio'))
        self.asr = self.stack.enter_context(patch.object(lesson.inference, 'transcribe'))
        self.frames = self.stack.enter_context(patch.object(lesson.media, 'extract_frames', side_effect=self.make_frames))
        self.stack.enter_context(patch.object(lesson, 'contact_sheets', return_value=[]))
        self.ocr = self.stack.enter_context(patch.object(lesson.inference, 'add_ocr', return_value={'engine': 'fixture'}))
        self.playback = self.stack.enter_context(patch.object(lesson.media, 'ensure_playback_video', side_effect=self.make_playback))

    def make_frames(self, *args, **kwargs):
        (self.output / 'frames').mkdir(exist_ok=True)
        (self.output / 'frames/frame-0001.jpg').write_bytes(b'frame bytes')
        return [{'id': 'frame-0001', 'image': 'frames/frame-0001.jpg', 'timestamp_seconds': 0}]

    def make_playback(self, *args, **kwargs):
        if (self.output / 'playback.mp4').exists():
            raise ValueError('playback.mp4 already exists; use a fresh output directory.')
        (self.output / 'playback.mp4').write_bytes(b'playback bytes')
        return {'video': 'playback.mp4', 'metadata': {'source_sha256': lesson.sha256(self.source)}, 'warnings': []}

    def reset_engines(self):
        for mock in (self.audio, self.asr, self.frames, self.ocr, self.playback):
            mock.reset_mock()

    def test_subtitles_skip_audio_and_asr_and_resume_reuses_all_completed_stages(self):
        first = lesson.prepare(self.options)
        self.assertEqual(first['asr_status'], 'subtitles')
        self.audio.assert_not_called()
        self.asr.assert_not_called()
        sentinel = self.output / 'guide.md'
        sentinel.write_text('existing authored document')
        self.options.resume = True
        self.reset_engines()
        second = lesson.prepare(self.options)
        self.assertEqual(second['reused_stages'], ['transcript', 'frames', 'ocr', 'playback'])
        for mock in (self.audio, self.asr, self.frames, self.ocr, self.playback):
            mock.assert_not_called()
        self.assertEqual(sentinel.read_text(), 'existing authored document')

    def test_failed_ocr_resumes_after_frames_without_repeating_transcription(self):
        self.ocr.side_effect = RuntimeError('temporary OCR failure')
        with self.assertRaisesRegex(RuntimeError, 'temporary OCR'):
            lesson.prepare(self.options)
        self.assertEqual(json.loads((self.output / 'lesson-evidence.json').read_text('utf-8'))['processing']['status'], 'failed')
        self.options.resume = True
        self.reset_engines()
        self.ocr.side_effect = None
        result = lesson.prepare(self.options)
        self.assertEqual(result['reused_stages'], ['transcript', 'frames'])
        self.frames.assert_not_called()
        self.asr.assert_not_called()
        self.ocr.assert_called_once()
        self.playback.assert_called_once()

    def test_corrupt_frame_invalidates_frame_and_ocr_only(self):
        lesson.prepare(self.options)
        (self.output / 'frames/frame-0001.jpg').write_bytes(b'corrupted')
        self.options.resume = True
        self.reset_engines()
        result = lesson.prepare(self.options)
        self.assertEqual(result['reused_stages'], ['transcript', 'playback'])
        self.frames.assert_called_once()
        self.ocr.assert_called_once()
        self.playback.assert_not_called()

    def test_corrupt_playback_is_quarantined_before_non_overwriting_media_writer(self):
        lesson.prepare(self.options)
        (self.output / 'playback.mp4').write_bytes(b'corrupt video')
        self.options.resume = True
        self.reset_engines()
        result = lesson.prepare(self.options)
        self.assertEqual(result['reused_stages'], ['transcript', 'frames', 'ocr'])
        self.playback.assert_called_once()
        self.assertEqual((self.output / 'playback.mp4').read_bytes(), b'playback bytes')
        preserved = list((self.output / 'analysis/superseded').glob('*-playback.mp4'))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_bytes(), b'corrupt video')

    def test_source_change_during_processing_invalidates_intermediate_results(self):
        def changed_frames(*args, **kwargs):
            self.source.write_bytes(b'changed during extraction')
            return self.make_frames()
        self.frames.side_effect = changed_frames
        with self.assertRaisesRegex(ValueError, '输入源不一致'):
            lesson.prepare(self.options)
        state = json.loads((self.output / 'analysis/material-cache.json').read_text('utf-8'))
        self.assertEqual(state['stages'], {})
        self.source.write_bytes(b'original video')
        self.frames.side_effect = self.make_frames
        self.options.resume = True
        self.reset_engines()
        self.assertEqual(lesson.prepare(self.options)['reused_stages'], [])
        self.frames.assert_called_once()
        self.ocr.assert_called_once()

    def test_non_browser_source_attachment_is_checked_and_repaired(self):
        def fallback(*args, **kwargs):
            path = self.output / 'source.mp4'
            if path.exists():
                raise ValueError('Unrelated source playback file already exists')
            path.write_bytes(b'fallback source')
            return {'video': None, 'metadata': {'source_sha256': lesson.sha256(self.source),
                    'source_attachment': 'source.mp4'}, 'warnings': ['fallback']}
        self.playback.side_effect = fallback
        lesson.prepare(self.options)
        (self.output / 'source.mp4').unlink()
        self.options.resume = True
        self.reset_engines()
        result = lesson.prepare(self.options)
        self.assertNotIn('playback', result['reused_stages'])
        self.playback.assert_called_once()
        self.assertEqual((self.output / 'source.mp4').read_bytes(), b'fallback source')

    def test_transcript_repair_does_not_mutate_a_linked_unrelated_file(self):
        lesson.prepare(self.options)
        target = self.root / 'keep.txt'
        target.write_text('keep this file')
        transcript = self.output / 'analysis/transcript.txt'
        transcript.unlink()
        os.link(target, transcript)
        self.options.resume = True
        lesson.prepare(self.options)
        self.assertEqual(target.read_text(), 'keep this file')
        self.assertIn('教学字幕', transcript.read_text('utf-8'))

    def test_source_subtitle_or_options_change_never_reuses_old_task(self):
        lesson.prepare(self.options)
        self.options.resume = True
        for change, restore in (
            (lambda: self.source.write_bytes(b'new input'), lambda: self.source.write_bytes(b'original video')),
            (lambda: self.subtitle.write_text('1\n00:00:00,000 --> 00:00:10,000\nchanged\n', encoding='utf-8'),
             lambda: self.subtitle.write_text('1\n00:00:00,000 --> 00:00:10,000\n教学字幕\n', encoding='utf-8')),
            (lambda: setattr(self.options, 'max_frames', 3), lambda: setattr(self.options, 'max_frames', 2)),
        ):
            change()
            with self.assertRaisesRegex(ValueError, '已改变'):
                lesson.prepare(self.options)
            restore()

    def test_cache_rejects_artifacts_outside_output(self):
        self.output.mkdir()
        (self.output / 'analysis').mkdir()
        cache = MaterialCache(self.output, {'source': 'test'})
        for path in ('../clip.mp4', str(self.source), 'frames/../../clip.mp4'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                cache.put('frames', {}, [path])

    def test_partial_subtitles_fall_back_to_asr_and_can_resume_audio_after_failure(self):
        self.subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\n片头字幕\n', encoding='utf-8')
        model_root = self.root / 'skill'
        model = model_root / 'model'
        model.mkdir(parents=True)
        config = {'version': 'test', 'asr_model_dir': 'model', 'asr_model_id': 'test', 'asr_model_revision': '1'}
        (model_root / 'config.json').write_text(json.dumps(config), encoding='utf-8')
        for stem in ('openvino_encoder_model', 'openvino_decoder_model'):
            for suffix in ('.xml', '.bin'):
                (model / (stem + suffix)).write_bytes(b'fixture')
        def audio(*args, **kwargs):
            path = self.output / 'audio.wav'
            path.write_bytes(b'extracted audio')
            return path
        self.audio.side_effect = audio
        self.asr.side_effect = RuntimeError('transcriber stopped')
        with patch.object(lesson, 'ROOT', model_root):
            with self.assertRaisesRegex(RuntimeError, 'transcriber stopped'):
                lesson.prepare(self.options)
            self.options.resume = True
            self.audio.reset_mock()
            self.asr.side_effect = None
            self.asr.return_value = [{'start_seconds': 0, 'end_seconds': 10, 'text': '本地转写结果'}]
            result = lesson.prepare(self.options)
        self.audio.assert_not_called()
        self.assertEqual(result['asr_status'], 'transcribed')
        self.assertIn('audio', result['reused_stages'])
        evidence = json.loads((self.output / 'lesson-evidence.json').read_text('utf-8'))
        self.assertEqual(evidence['transcript'][0]['text'], '本地转写结果')
        self.assertTrue(evidence['processing']['asr']['full_audio_submitted'])
        self.assertTrue(any('字幕' in warning for warning in result['warnings']))


if __name__ == '__main__':
    unittest.main()
