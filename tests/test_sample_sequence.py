"""Verify original PTS bindings and bounded non-overwriting sequence exports."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sample_sequence
from screen_guide.media import MediaError


class SequenceBoundsTests(unittest.TestCase):
    def test_interval_and_density_bounds_precede_decoding(self):
        for start, end, fps in ((-1, 1, 8), (1, 1, 8), (0, 31, 8), (0, 1, 0),
                                (0, 1, 31), (0, 30, 30), (False, 1, 8), (0, float('nan'), 8)):
            with self.subTest(start=start, end=end, fps=fps), self.assertRaises(MediaError):
                sample_sequence.sample_sequence(Path('missing.mp4'), Path('unused-output'),
                                                 start=start, end=end, fps=fps)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required')
class SequenceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        runtime = Path(__file__).resolve().parents[1] / '.runtime' / 'tests'
        runtime.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix='sequence-sampler-', dir=runtime)
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / 'shifted noninteger VFR.mkv'
        cls.ffmpeg, cls.ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        subprocess.run([cls.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                        '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30000/1001:duration=2.5',
                        '-vf', "select='eq(n,0)+eq(n,2)+eq(n,5)+eq(n,9)+eq(n,12)+eq(n,20)+eq(n,38)+eq(n,49)+eq(n,60)+eq(n,69)',setpts=PTS+5/TB",
                        '-fps_mode', 'vfr', '-c:v', 'ffv1', str(cls.source)],
                       check=True, capture_output=True, timeout=30)
        probe = subprocess.run([cls.ffprobe, '-v', 'error', '-select_streams', 'v:0',
                                '-show_frames', '-show_entries', 'frame=pts:stream=time_base:format=start_time',
                                '-of', 'json', str(cls.source)], check=True, capture_output=True, timeout=10)
        cls.probe = json.loads(probe.stdout)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_noninteger_source_pts_shift_and_original_dimensions(self):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        output = self.root / 'sample'
        result = sample_sequence.sample_sequence(self.source, output, start=.1, end=2.1, fps=8,
                                                 ffmpeg=self.ffmpeg, ffprobe=self.ffprobe)
        native_pts = {frame['pts'] for frame in self.probe['frames']}
        origin = float(self.probe['format']['start_time'])
        self.assertGreater(origin, 4)
        self.assertEqual(result['source']['source_start_time_seconds'], origin)
        self.assertGreater(len(result['frames']), 3)
        self.assertTrue(any(frame['timestamp_seconds'] % 1 for frame in result['frames']))
        for frame in result['frames']:
            with self.subTest(frame=frame['id']):
                self.assertIn(frame['source_pts'], native_pts)
                self.assertEqual(frame['source_time_base'], self.probe['streams'][0]['time_base'])
                num, den = map(int, frame['source_time_base'].split('/'))
                self.assertAlmostEqual(frame['timestamp_seconds'], frame['source_pts'] * num / den - origin)
                self.assertTrue(.1 <= frame['timestamp_seconds'] <= 2.1)
                self.assertEqual(hashlib.sha256((output / frame['image']).read_bytes()).hexdigest(), frame['sha256'])
        from PIL import Image
        with Image.open(output / result['frames'][0]['image']) as image:
            self.assertEqual(image.size, (320, 180))
        self.assertTrue((output / 'contact-001.jpg').is_file())
        self.assertEqual(json.loads((output / 'sequence.json').read_text(encoding='utf-8')), result)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)

    def test_existing_evidence_and_invalid_end_are_preserved(self):
        output = self.root / 'existing'
        output.mkdir()
        sentinel = output / 'keep.txt'
        sentinel.write_text('preserve')
        with self.assertRaisesRegex(MediaError, 'new or empty'):
            sample_sequence.sample_sequence(self.source, output, start=0, end=1, ffmpeg=self.ffmpeg, ffprobe=self.ffprobe)
        self.assertEqual(sentinel.read_text(), 'preserve')
        with self.assertRaisesRegex(MediaError, 'exceeds'):
            sample_sequence.sample_sequence(self.source, self.root / 'too-long', start=0, end=10,
                                             ffmpeg=self.ffmpeg, ffprobe=self.ffprobe)
        self.assertFalse((self.root / 'too-long').exists())

    def test_cli_writes_manifest_with_exact_frame_records(self):
        output = self.root / 'cli'
        result = subprocess.run([sys.executable, str(Path(sample_sequence.__file__)), str(self.source),
                                 '--start', '.2', '--end', '1', '--fps', '4', '--output-dir', str(output),
                                 '--ffmpeg', self.ffmpeg, '--ffprobe', self.ffprobe],
                                capture_output=True, timeout=30, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        manifest = json.loads((output / 'sequence.json').read_text(encoding='utf-8'))
        self.assertTrue(report['ok'])
        self.assertEqual(report['frame_count'], len(manifest['frames']))
        self.assertTrue(manifest['frames'][0]['source_pts'] > 5000)


if __name__ == '__main__':
    unittest.main()
