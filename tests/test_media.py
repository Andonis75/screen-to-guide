"""Engineering fixtures only; these tests do not evaluate video understanding."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from screen_guide import media


class MediaUnitTests(unittest.TestCase):
    def test_remote_and_missing_input(self):
        for source in ("https://example.org/video.mp4", "file:///video.mp4", "missing-video.mp4"):
            with self.subTest(source=source), self.assertRaises(media.MediaError):
                media.probe_media(source, "ffprobe")

    def test_exact_pts_uses_rational_timebase(self):
        log = (b"config in time_base: 1/90000, frame_rate: 30000/1001\n"
               b"n: 0 pts: 3003 pts_time:0.0333667\n"
               b"n: 1 pts: 12012 pts_time:0.133467\n")
        self.assertEqual(media._timestamps(log), [(3003, 3003 / 90000), (12012, 12012 / 90000)])
        with self.assertRaises(media.MediaError):
            media._timestamps(log + b"n: 2 pts: 3003 pts_time:0.0333667\n")

    def test_selection_keeps_coverage_and_strongest_change(self):
        points = [(i, float(i)) for i in range(10)]
        result = media._select(points, [0, 0, .8, 0, 0, 0, .4, 0, 0, 0], 4, .08)
        self.assertEqual(result, {0: "start", 9: "end", 4: "interval", 2: "change"})
        static = media._select(points, [0] * 10, 8, .08)
        self.assertEqual(set(static), {0, 3, 6, 9})
        self.assertEqual(set(media._select(points, [0] * 10, 3, .08)), {0, 4, 9})

    def test_resource_bounds(self):
        for maximum in (0, 97, True, 1.5):
            with self.subTest(maximum=maximum), self.assertRaises(media.MediaError):
                media.extract_frames("missing.mp4", ".", ffmpeg="ffmpeg", ffprobe="ffprobe", max_frames=maximum)
        for interval in (0, -1, float("nan"), float("inf"), 1801):
            with self.subTest(interval=interval), self.assertRaises(media.MediaError):
                media.extract_frames("missing.mp4", ".", ffmpeg="ffmpeg", ffprobe="ffprobe", sample_interval=interval)

    def test_metadata_duration_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.mp4"
            path.write_bytes(b"fixture")
            for duration in (0, 1801, float("nan")):
                payload = {"format": {"duration": str(duration)}, "streams": [
                    {"codec_type": "video", "width": 640, "height": 360}]}
                with mock.patch.object(media, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload).encode(), b"")):
                    with self.assertRaises(media.MediaError):
                        media.probe_media(path, "ffprobe")

    def test_timeout_and_tool_failure(self):
        with mock.patch.object(media.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffmpeg", 1)):
            with self.assertRaisesRegex(media.MediaError, "exceeded"):
                media._run(["ffmpeg"], timeout=1)
        with mock.patch.object(media.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"decode error")):
            with self.assertRaisesRegex(media.MediaError, "decode error"):
                media._run(["ffmpeg"])

    def test_audio_honors_separately_configured_ffprobe(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "synthetic.mp4"
            source.write_bytes(b"fixture")
            with mock.patch.object(media, "probe_media", return_value={"has_audio": False}) as probe:
                self.assertIsNone(media.extract_audio(source, Path(temporary), ffmpeg="elsewhere/ffmpeg",
                                                       ffprobe="custom/tools/ffprobe"))
            probe.assert_called_once_with(source.resolve(), "custom/tools/ffprobe")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Local FFmpeg is required for integration fixtures")
class MediaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="screen-guide-fixture-")
        cls.root = Path(cls.temporary.name)
        cls.ffmpeg, cls.ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        cls.vfr = cls.root / "合成工程测试 VFR.mkv"
        cls.av = cls.root / "synthetic-audio.mkv"
        cls.ui = cls.root / "three-small-ui-changes.mkv"
        cls.shifted = cls.root / "shifted-vfr.mkv"
        cls.hevc = cls.root / "synthetic-hlg-hevc.mp4"
        cls._ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10:duration=4", "-vf",
                     "select='eq(n,0)+eq(n,3)+eq(n,10)+eq(n,12)+eq(n,27)+eq(n,34)'",
                     "-fps_mode", "vfr", "-c:v", "ffv1", str(cls.vfr)])
        cls._ffmpeg(["-itsoffset", "5", "-i", str(cls.vfr), "-c", "copy", str(cls.shifted)])
        cls._ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=5:duration=1",
                     "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest",
                     "-c:v", "ffv1", "-c:a", "pcm_s16le", str(cls.av)])
        cls._ffmpeg(["-f", "lavfi", "-i", "color=white:size=480x270:rate=2:duration=3", "-vf",
                     "drawbox=x=20:y=20:w=160:h=60:color=black:t=fill:enable='between(t,1,1.9)',"
                     "drawbox=x=20:y=20:w=160:h=60:color=yellow:t=fill:enable='gte(t,2)'", "-c:v", "ffv1", str(cls.ui)])
        cls._ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=5:duration=1", "-f", "lavfi", "-i",
                     "sine=frequency=440:duration=1", "-shortest", "-c:v", "libx265", "-x265-params",
                     "pools=2:frame-threads=2:log-level=error:colorprim=9:transfer=18:colormatrix=9", "-pix_fmt", "yuv420p10le", "-color_primaries", "bt2020",
                     "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc", "-c:a", "aac", str(cls.hevc)])

    @classmethod
    def _ffmpeg(cls, arguments):
        subprocess.run([cls.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"] + arguments,
                       capture_output=True, check=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_vfr_timestamps_exact_and_source_unchanged(self):
        before = hashlib.sha256(self.vfr.read_bytes()).hexdigest()
        destination = self.root / "vfr-evidence"
        frames = media.extract_frames(self.vfr, destination, ffmpeg=self.ffmpeg, ffprobe=self.ffprobe,
                                      max_frames=6, sample_interval=.15, scene_threshold=0)
        self.assertEqual([f["timestamp_seconds"] for f in frames], [0, .3, 1, 1.2, 2.7, 3.4])
        self.assertEqual([f["id"] for f in frames], [f"frame-{i:04d}" for i in range(1, 7)])
        self.assertEqual(frames[0]["reason"], "start")
        self.assertEqual(frames[-1]["reason"], "end")
        for frame in frames:
            self.assertGreater((destination / frame["image"]).stat().st_size, 1000)
        self.assertEqual(hashlib.sha256(self.vfr.read_bytes()).hexdigest(), before)

    def test_hard_output_count_and_full_resolution(self):
        destination = self.root / "bounded"
        frames = media.extract_frames(self.vfr, destination, ffmpeg=self.ffmpeg, ffprobe=self.ffprobe,
                                      max_frames=3, sample_interval=.15, scene_threshold=0)
        self.assertEqual(len(frames), 3)
        info = subprocess.run([self.ffprobe, "-v", "error", "-show_entries", "stream=width,height", "-of", "json",
                               str(destination / frames[0]["image"])], capture_output=True, check=True, timeout=10)
        self.assertEqual(json.loads(info.stdout)["streams"][0], {"width": 640, "height": 360})

    def test_probe_and_optional_audio(self):
        info = media.probe_media(self.vfr, self.ffprobe)
        self.assertEqual((info["width"], info["height"], info["has_audio"]), (640, 360, False))
        self.assertIsNone(media.extract_audio(self.vfr, self.root / "silent", ffmpeg=self.ffmpeg))
        output = media.extract_audio(self.av, self.root / "sound", ffmpeg=self.ffmpeg)
        with wave.open(str(output), "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getframerate(), wav.getsampwidth()), (1, 16000, 2))
            self.assertGreater(wav.getnframes(), 15000)

    def test_three_white_ui_phases_fit_budget_and_small_change_survives(self):
        frames = media.extract_frames(self.ui, self.root / "small-ui", ffmpeg=self.ffmpeg, ffprobe=self.ffprobe,
                                      max_frames=6, sample_interval=.5)
        self.assertIn(2.0, [frame["timestamp_seconds"] for frame in frames])
        transition = next(frame for frame in frames if frame["timestamp_seconds"] == 2)
        self.assertEqual(transition["reason"], "change")
        self.assertLess(transition["change_score"], .08)
        bounded = media.extract_frames(self.ui, self.root / "small-ui-three", ffmpeg=self.ffmpeg, ffprobe=self.ffprobe,
                                       max_frames=3, sample_interval=.5)
        self.assertEqual([frame["timestamp_seconds"] for frame in bounded], [0, 1, 2.5])

    def test_playback_preserves_vfr_pts_and_normalizes_input_origin(self):
        for name, source in (("vfr", self.vfr), ("offset", self.shifted)):
            with self.subTest(name=name):
                destination = self.root / (name + "-playback")
                result = media.ensure_playback_video(source, destination, ffmpeg=self.ffmpeg, ffprobe=self.ffprobe)
                self.assertEqual(result["metadata"]["status"], "transcoded", result["warnings"])
                self.assertTrue(result["metadata"]["derived_copy"])
                self.assertEqual(result["metadata"]["first_video_pts_delta_seconds"], 0)
                data = subprocess.run([self.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
                                       "-show_entries", "frame=best_effort_timestamp_time", "-of", "json",
                                       str(destination / result["video"])], capture_output=True, check=True, timeout=10)
                points = [float(frame["best_effort_timestamp_time"]) for frame in json.loads(data.stdout)["frames"]]
                self.assertEqual(points, [0, .3, 1, 1.2, 2.7, 3.4])

    def test_hevc_hdr_becomes_h264_sdr_aac_without_changing_source(self):
        before = hashlib.sha256(self.hevc.read_bytes()).hexdigest()
        destination = self.root / "hevc-playback"
        result = media.ensure_playback_video(self.hevc, destination, ffmpeg=self.ffmpeg, ffprobe=self.ffprobe)
        self.assertEqual(result["video"], "playback.mp4", result["warnings"])
        self.assertTrue(result["metadata"]["hdr_to_sdr"])
        self.assertTrue(result["metadata"]["lossy"])
        info = media.probe_media(destination / result["video"], self.ffprobe)
        self.assertEqual((info["video_codec"], info["audio_codec"], info["pixel_format"], info["color_transfer"]),
                         ("h264", "aac", "yuv420p", "bt709"))
        self.assertLessEqual(result["metadata"]["duration_delta_seconds"], .5)
        self.assertEqual(hashlib.sha256(self.hevc.read_bytes()).hexdigest(), before)
        payload = (destination / result["video"]).read_bytes()
        self.assertLess(payload.index(b"moov"), payload.index(b"mdat"))

    def test_failed_transcode_attaches_source_and_reports_fallback(self):
        metadata = media.probe_media(self.av, self.ffprobe)
        destination = self.root / "fallback-playback"
        with mock.patch.object(media, "_run", side_effect=media.MediaError("encoder unavailable")):
            result = media.ensure_playback_video(self.av, destination, ffmpeg=self.ffmpeg, ffprobe=self.ffprobe, metadata=metadata)
        self.assertIsNone(result["video"])
        self.assertEqual(result["metadata"]["status"], "source_fallback")
        self.assertFalse(result["metadata"]["derived_copy"])
        self.assertIn("encoder unavailable", result["warnings"][0])
        self.assertEqual((destination / result["metadata"]["source_attachment"]).read_bytes(), self.av.read_bytes())
        self.assertFalse((destination / "playback.mp4").exists())


if __name__ == "__main__":
    unittest.main()
