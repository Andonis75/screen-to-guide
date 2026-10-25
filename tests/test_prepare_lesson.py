"""Small boundary checks for the teaching-material preparation entry point."""
import math
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import prepare_lesson as lesson


class LessonMaterialTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / ".runtime" / "cache" / "tmp"
        cache.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="lesson-unit-", dir=cache)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"fixture")

    def test_new_output_never_overwrites_source_or_existing_material(self):
        with self.assertRaises(ValueError):
            lesson.new_output(self.source, self.source)
        existing = self.root / "existing"
        existing.mkdir()
        note = existing / "keep.txt"
        note.write_text("keep", encoding="utf-8")
        with self.assertRaises(ValueError):
            lesson.new_output(existing, self.source)
        self.assertEqual(note.read_text(), "keep")
        created = lesson.new_output(self.root / "fresh", self.source)
        self.assertTrue((created / "analysis").is_dir())

    def test_coverage_indexes_actual_windows_and_exposes_gaps(self):
        frames = [{"id": "first", "timestamp_seconds": 0}, {"id": "last", "timestamp_seconds": 94}]
        transcript = [{"start_seconds": 0, "end_seconds": 30, "text": "first"},
                      {"start_seconds": 30, "end_seconds": 61, "text": "second"}]
        rows = lesson.coverage_index(95, frames, transcript)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["transcript_indices"], [0])
        self.assertEqual(rows[1]["transcript_indices"], [1])
        self.assertFalse(rows[1]["has_frame_in_window"])
        self.assertEqual(rows[1]["frame_ids"], [])
        self.assertEqual(rows[1]["nearest_frames_outside_window"][0]["frame_id"], "first")
        self.assertEqual(rows[-1]["end_seconds"], 95)
        self.assertEqual(rows[-1]["frame_ids"], ["last"])

    def test_transcript_import_rejects_invalid_or_out_of_range_times(self):
        valid = {"start_seconds": 0, "end_seconds": 10, "text": "all text"}
        self.assertEqual(lesson.validate_transcript({"transcript": [valid]}, 10), [valid])
        for start, end in [(-1, 1), (0, 10.1), (True, 2), (0, math.nan), (5, 2)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                lesson.validate_transcript([dict(valid, start_seconds=start, end_seconds=end)], 10)
        with self.assertRaises(ValueError):
            lesson.validate_transcript([dict(valid, start_seconds=5), valid], 10)

    def test_empty_transcript_is_explicit_and_keeps_all_nonempty_segments(self):
        path = self.root / "transcript.txt"
        lesson.save_transcript(path, [], "no_audio")
        text = path.read_text(encoding="utf-8")
        self.assertIn("no_audio", text)
        self.assertIn("未获得语音转写内容", text)
        lesson.save_transcript(path, [{"start_seconds": 0, "end_seconds": 2.25, "text": "完整文字"}], "transcribed")
        self.assertIn("00:00.000 --> 00:02.250", path.read_text(encoding="utf-8"))
        self.assertIn("完整文字", path.read_text(encoding="utf-8"))

    def test_imported_transcript_skips_asr_and_records_its_source(self):
        imported = self.root / "import.json"
        imported.write_text(json.dumps([{"start_seconds": 0, "end_seconds": 10, "text": "complete"}]), encoding="utf-8")
        output = self.root / "import-output"
        options = SimpleNamespace(input=str(self.source), output_dir=str(output), max_frames=1,
                                  no_ocr=True, transcript_json=str(imported))
        source_hash = lesson.sha256(self.source)
        with patch.object(lesson.shutil, "which", return_value="local-tool"), \
             patch.object(lesson.media, "probe_media", return_value={"has_audio": True, "duration_seconds": 10}), \
             patch.object(lesson.media, "extract_audio") as audio, \
             patch.object(lesson.inference, "transcribe") as asr, \
             patch.object(lesson.media, "extract_frames", return_value=[]), \
             patch.object(lesson.media, "ensure_playback_video", return_value={"video": None, "metadata": {"source_sha256": source_hash}, "warnings": []}):
            result = lesson.prepare(options)
        audio.assert_not_called()
        asr.assert_not_called()
        self.assertEqual(result["asr_status"], "imported")
        manifest = json.loads((output / "lesson-evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["processing"]["asr"]["source_sha256"], lesson.sha256(imported))
        self.assertFalse(manifest["processing"]["asr"]["full_audio_submitted"])


if __name__ == "__main__":
    unittest.main()
