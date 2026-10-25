"""Caption parsing and reuse gates without model/network dependencies."""
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from screen_guide import subtitles


class SubtitleTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / ".runtime/cache/tmp"
        cache.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="subtitles-", dir=cache)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, content):
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return path

    def load_json(self, value, duration=10, **kwargs):
        path = self.write("captions.json", json.dumps(value, ensure_ascii=False))
        return subtitles.load_subtitles(path, duration, **kwargs)

    def test_srt_text_timing_and_provenance(self):
        path = self.write("lesson.srt", "\ufeff1\n00:00:00,000 --> 00:00:04,200\n<b>先打开</b> &amp; <TOKEN>\n第二行\n\n2\n00:00:04,200 --> 00:00:10,000\n完成\n")
        result = subtitles.load_subtitles(path, 10)
        self.assertEqual(result["segments"][0], {"start_seconds": 0, "end_seconds": 4.2, "text": "先打开 & <TOKEN> 第二行"})
        self.assertEqual(result["metadata"]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertTrue(result["metadata"]["usable"])

    def test_vtt_header_settings_tags_and_non_caption_blocks(self):
        path = self.write("lesson.zh.vtt", "WEBVTT\nKind: captions\nLanguage: zh\n\nNOTE evidence only\nignored\n\nSTYLE\n::cue { color: red; }\n\ncue-1\n00:00.000 --> 00:05.000 align:start position:0%\n<v Speaker><c.green>hello</c> <00:01.000>world\n\n00:05.000 --> 00:10.000\n再见\n")
        result = subtitles.load_subtitles(path, 10)
        self.assertEqual([s["text"] for s in result["segments"]], ["hello world", "再见"])
        self.assertTrue(result["metadata"]["usable"])

    def test_json_formats(self):
        expected = [{"start_seconds": 0.0, "end_seconds": 10.0, "text": "hello world"}]
        variants = [expected, {"transcript": expected}, {"body": [{"from": 0, "to": 10, "content": "hello world"}]},
                    {"events": [{"id": 1}, {"tStartMs": 0, "dDurationMs": 10000, "segs": [{"utf8": "hello "}, {"utf8": "world", "tOffsetMs": 100}]}]}]
        for value in variants:
            with self.subTest(value=value):
                result = self.load_json(value)
                self.assertEqual(result["segments"], expected)
                self.assertTrue(result["metadata"]["usable"])

    def test_inline_formatting_does_not_insert_words_or_break_placeholders(self):
        result = self.load_json([{"start_seconds": 0, "end_seconds": 10, "text": "你<b>好</b><br><a href='https://example.com'>打开</a> <TOKEN>"}])
        self.assertEqual(result["segments"][0]["text"], "你好 打开 <TOKEN>")

    def test_rolling_exact_duplicates_merge_but_different_words_survive(self):
        result = self.load_json([
            {"start_seconds": 0, "end_seconds": 3, "text": "click"},
            {"start_seconds": 1, "end_seconds": 4, "text": "click"},
            {"start_seconds": 2, "end_seconds": 5, "text": "click Save"},
            {"start_seconds": 7, "end_seconds": 10, "text": "click"},
        ])
        self.assertEqual(result["segments"], [
            {"start_seconds": 0, "end_seconds": 4, "text": "click"},
            {"start_seconds": 2, "end_seconds": 5, "text": "click Save"},
            {"start_seconds": 7, "end_seconds": 10, "text": "click"},
        ])
        self.assertEqual(result["metadata"]["coverage"]["caption_seconds"], 8)

    def test_repeated_line_after_gap_is_not_removed(self):
        result = self.load_json([{"start_seconds": 0, "end_seconds": 2, "text": "再次确认"},
                                 {"start_seconds": 8, "end_seconds": 10, "text": "再次确认"}])
        self.assertEqual(len(result["segments"]), 2)

    def test_partial_empty_and_extremely_sparse_tracks_not_usable(self):
        cases = [[], [{"start_seconds": 0, "end_seconds": 30, "text": "only opening"}],
                 [{"start_seconds": 30, "end_seconds": 100, "text": "missing opening"}],
                 [{"start_seconds": 0, "end_seconds": .1, "text": "hi"}, {"start_seconds": 99.9, "end_seconds": 100, "text": "bye"}]]
        for value in cases:
            with self.subTest(value=value):
                self.assertFalse(self.load_json(value, duration=100)["metadata"]["usable"])

    def test_real_silence_does_not_automatically_reject_track(self):
        result = self.load_json([{"start_seconds": 0, "end_seconds": 10, "text": "开始等待安装"},
                                 {"start_seconds": 590, "end_seconds": 600, "text": "安装完成"}], duration=600)
        self.assertTrue(result["metadata"]["usable"])
        self.assertEqual(result["metadata"]["coverage"]["largest_gap_seconds"], 580)

    def test_tiny_clip_is_supported(self):
        result = self.load_json([{"start_seconds": 0, "end_seconds": .3, "text": "完成"}], duration=.3)
        self.assertTrue(result["metadata"]["usable"])

    def test_malformed_times_and_order_are_rejected(self):
        for start, end in [(-1, 1), (0, 11), (True, 10), (0, math.nan), (5, 4), (2, 2)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.load_json([{"start_seconds": start, "end_seconds": end, "text": "x"}])
        with self.assertRaises(ValueError):
            self.load_json([{"start_seconds": 5, "end_seconds": 6, "text": "x"}, {"start_seconds": 0, "end_seconds": 4, "text": "y"}])

    def test_malformed_text_formats_raise_instead_of_silently_skipping(self):
        for name, text in [("a.srt", "not timed captions"), ("a.srt", "1\n00:99:00,000 --> 00:00:10,000\nx"),
                           ("a.vtt", "00:00.000 --> 00:10.000\nx"), ("a.json", "{broken")]:
            with self.subTest(name=name, text=text), self.assertRaises(ValueError):
                subtitles.load_subtitles(self.write(name, text), 10)

    def test_bad_json_schemas_and_bad_json3_timing_rejected(self):
        for value in [{"transcript": "text"}, {"body": ["bad"]}, {"events": [{"tStartMs": 0, "segs": [{"utf8": "missing duration"}]}]},
                      {"events": [{"tStartMs": 0, "dDurationMs": True, "segs": [{"utf8": "bad boolean"}]}]}, {"unknown": []}, 42]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load_json(value)

    def test_bound_source_hash_is_checked(self):
        digest = "a" * 64
        value = {"source": {"sha256": digest}, "transcript": [{"start_seconds": 0, "end_seconds": 10, "text": "x"}]}
        self.assertTrue(self.load_json(value, source_sha256=digest.upper())["metadata"]["usable"])
        with self.assertRaises(ValueError):
            self.load_json(value, source_sha256="b" * 64)
        value["source"]["sha256"] = "not a hash"
        with self.assertRaises(ValueError):
            self.load_json(value)

    def test_file_limits_utf8_and_segment_limits(self):
        path = self.root / "large.srt"
        with path.open("wb") as stream:
            stream.truncate(subtitles.MAX_BYTES + 1)
        with self.assertRaises(ValueError):
            subtitles.load_subtitles(path, 10)
        path.write_bytes(b"\xff\xfe\x80")
        with self.assertRaises(ValueError):
            subtitles.load_subtitles(path, 10)
        with self.assertRaises(ValueError):
            self.load_json([{}] * (subtitles.MAX_SEGMENTS + 1))

    def test_discovery_is_same_video_only_and_language_priority_is_stable(self):
        video = self.write("lesson.part1.mp4", "video")
        names = ["lesson.part1.en.vtt", "lesson.part1.zh-Hans.vtt", "lesson.part1.srt", "lesson.part1.zh-CN.srt", "lesson.part1.en.json3", "lesson.part1.ai-zh.vtt",
                 "lesson.part1.json", "lesson.part1.backup.vtt", "lesson.part10.zh.vtt", "unrelated.zh.srt"]
        for name in names:
            self.write(name, "captions")
        found = [p.name for p in subtitles.find_subtitles(video)]
        self.assertEqual(found, ["lesson.part1.zh-CN.srt", "lesson.part1.zh-Hans.vtt", "lesson.part1.ai-zh.vtt", "lesson.part1.srt", "lesson.part1.en.json3", "lesson.part1.en.vtt"])


if __name__ == "__main__":
    unittest.main()
