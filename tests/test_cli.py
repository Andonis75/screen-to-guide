"""Public entry point behavior with short, explicitly synthetic video fixtures."""
from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("screen_guide_cli", SCRIPTS / "run.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def invoke(arguments):
    output, errors = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = cli.main(arguments + ["--format", "json"])
    return code, json.loads(output.getvalue()), errors.getvalue()


class CLIUnitTests(unittest.TestCase):
    def test_missing_model_fails_before_extracting_or_creating_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "must-not-exist"
            with mock.patch("screen_guide.media.extract_frames") as extract:
                code, result, _ = invoke(["missing.mp4", "--model-dir", str(Path(temporary) / "no-model"),
                                         "--output-dir", str(destination)])
                self.assertEqual(code, 1)
                self.assertFalse(result["ok"])
                self.assertIn("--prepare-model", result["error"])
                extract.assert_not_called()
            self.assertFalse(destination.exists())

    def test_prepare_model_keeps_progress_off_json_stdout(self):
        def prepare(kind, model_id):
            print("download progress")
            return {"repo_id": "synthetic/test", "revision": "fixture"}
        with mock.patch("prepare_models.prepare", side_effect=prepare):
            code, result, errors = invoke(["--prepare-model", "--kind", "asr"])
        self.assertEqual(code, 0)
        self.assertEqual(result["kind"], "asr")
        self.assertIn("download progress", errors)

    def test_malformed_review_json_reports_structured_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "reviewed.json"
            malformed = [[], None, "text", {"schema_version": 1, "frames": "text"},
                         {"schema_version": 1, "frames": [None]}, {"schema_version": 1, "steps": {}},
                         {"schema_version": 1, "video": []}, {"schema_version": 1, "source": []},
                         {"schema_version": 1, "transcript": ["text"]}]
            for value in malformed:
                with self.subTest(value=value):
                    source.write_text(json.dumps(value), encoding="utf-8")
                    code, result, _ = invoke(["--render-manifest", str(source)])
                    self.assertEqual(code, 1)
                    self.assertFalse(result["ok"])
                    self.assertFalse((Path(temporary) / "guide.html").exists())
            source.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")
            code, result, _ = invoke(["--render-manifest", str(source)])
            self.assertEqual(code, 1)
            self.assertIn("嵌套过深", result["error"])

    def test_render_does_not_mask_internal_type_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "reviewed.json"
            source.write_text('{"schema_version":1}', encoding="utf-8")
            with mock.patch("screen_guide.render._validate", side_effect=TypeError("internal bug")):
                with self.assertRaisesRegex(TypeError, "internal bug"):
                    invoke(["--render-manifest", str(source)])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Local FFmpeg is required")
class CLIIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="screen-guide-cli-")
        cls.root = Path(cls.temporary.name)
        cls.source = cls.root / "synthetic-fixture.mp4"
        subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi",
                        "-i", "testsrc2=size=320x180:rate=5:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        str(cls.source)], check=True, capture_output=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_evidence_export_review_and_overwrite_guard(self):
        destination = self.root / "guide"
        code, result, errors = invoke([str(self.source), "--mode", "evidence", "--no-ocr", "--max-frames", "3",
                                       "--sample-interval", ".5", "--output-dir", str(destination)])
        self.assertEqual(code, 0, errors)
        self.assertEqual(result["frame_count"], 3)
        manifest = json.loads((destination / "steps.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["processing"]["local_only"])
        self.assertEqual(manifest["processing"]["mode"], "evidence")
        self.assertEqual(manifest["video"], "source.mp4")
        self.assertEqual(manifest["processing"]["playback"]["status"], "source_copy")
        self.assertFalse(manifest["processing"]["playback"]["derived_copy"])
        self.assertTrue(all(step["review_status"] == "pending" for step in manifest["steps"]))
        self.assertEqual(cli.sha256(self.source), cli.sha256(destination / "source.mp4"))
        self.assertIn("原始时间戳", errors)
        manifest["steps"][0]["review_status"] = "confirmed"
        manifest["steps"][0]["title"] = "人工复核后的标题"
        reviewed = destination / "steps-reviewed.json"
        reviewed.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        code, result, _ = invoke(["--render-manifest", str(reviewed)])
        self.assertEqual(code, 1)
        self.assertIn("--overwrite-guide", result["error"])
        code, result, errors = invoke(["--render-manifest", str(reviewed), "--overwrite-guide"])
        self.assertEqual(code, 0, errors)
        rebuilt = json.loads((destination / "steps.json").read_text(encoding="utf-8"))
        self.assertEqual(rebuilt["steps"][0]["review_status"], "confirmed")
        self.assertEqual(rebuilt["steps"][0]["title"], "人工复核后的标题")
        code, result, _ = invoke([str(self.source), "--mode", "evidence", "--no-ocr", "--output-dir", str(destination)])
        self.assertEqual(code, 1)
        self.assertIn("空目录", result["error"])

    def test_no_copy_no_audio_and_portable_json_stdout(self):
        from screen_guide import media
        destination = self.root / "no-copy"
        with mock.patch("screen_guide.media.extract_audio", wraps=media.extract_audio) as audio:
            code, result, errors = invoke([str(self.source), "--mode", "evidence", "--no-ocr", "--transcribe", "--no-copy-video",
                                           "--max-frames", "1", "--output-dir", str(destination), "--ffprobe", shutil.which("ffprobe")])
        self.assertEqual(audio.call_args.kwargs["ffprobe"], str(Path(shutil.which("ffprobe")).resolve()))
        self.assertEqual(code, 0, errors)
        manifest = json.loads((destination / "steps.json").read_text(encoding="utf-8"))
        self.assertIsNone(manifest["video"])
        self.assertIsNone(manifest["processing"]["playback"])
        self.assertEqual(manifest["transcript"], [])
        self.assertTrue(any("没有音轨" in warning for warning in result["warnings"]))
        self.assertFalse((destination / "source.mp4").exists())


if __name__ == "__main__":
    unittest.main()
