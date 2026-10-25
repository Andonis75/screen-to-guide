"""Behavior and boundary tests for the standalone, offline lesson renderer."""
from __future__ import annotations

import copy
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "render_lesson.py"
SPEC = importlib.util.spec_from_file_location("render_lesson_under_test", SCRIPT)
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.payload = []
        self.in_payload = False

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "script" and dict(attrs).get("id") == "lesson-data":
            self.in_payload = True

    def handle_data(self, data):
        if self.in_payload:
            self.payload.append(data)

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_payload = False


class LessonRendererTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="lesson-render-test-")
        self.addCleanup(self.scratch.cleanup)
        self.base = Path(self.scratch.name)
        self.output = self.base / "lesson"
        (self.output / "frames").mkdir(parents=True)
        (self.output / "playback.mp4").write_bytes(b"local-video-fixture")
        (self.output / "frames" / "chapter 1.png").write_bytes(b"local-image-fixture")
        self.manifest = {
            "schema_version": 1,
            "kind": "lesson",
            "title": "本地视频教材",
            "summary": "完整说明原理、工具及步骤。",
            "source": {
                "name": "课程.mp4", "url": "https://example.org/lesson?q=1&v=2",
                "author": "讲师", "duration_seconds": 20,
                "sha256": "a" * 64, "width": 640, "height": 360,
            },
            "video": "playback.mp4",
            "prerequisites": ["准备本地环境。"],
            "sections": [
                {
                    "id": "principle", "title": "原理与工具", "start_seconds": 0,
                    "end_seconds": 10, "paragraphs": ["第一段完整内容。", "第二段完整内容。"],
                    "bullets": ["工具用途。"], "steps": ["打开工具。", "核对输出。"],
                    "code_blocks": [{"language": "python", "code": "print('完整代码')"}],
                    "images": [{"path": "frames/chapter 1.png", "timestamp_seconds": 5,
                                "caption": "原始画面与细节"}],
                    "note": "保留这项补充。",
                },
                {"id": "practice", "title": "实操", "start_seconds": 10,
                 "end_seconds": 20, "paragraphs": ["最后一章完整内容。"]},
            ],
            "glossary": [{"term": "本地", "definition": "保存在当前计算机。"}],
            "references": [{"title": "课程来源", "url": "https://example.org/reference"}],
            "coverage": {"internal_marker": "INTERNAL_COVERAGE_SENTINEL"},
        }

    def read_outputs(self):
        return {name: (self.output / name).read_text(encoding="utf-8")
                for name in ("guide.html", "guide.md", "lesson.json")}

    def assert_rejected_without_guides(self, manifest):
        with self.assertRaises(ValueError):
            renderer.render_lesson(manifest, self.output)
        for name in ("guide.html", "guide.md", "lesson.json"):
            self.assertFalse((self.output / name).exists())

    def test_cli_writes_three_outputs_with_complete_content_and_shared_markdown(self):
        input_path = self.base / "manifest.json"
        input_path.write_text(json.dumps(self.manifest, ensure_ascii=False), encoding="utf-8")
        before = {path.relative_to(self.output) for path in self.output.rglob("*") if path.is_file()}
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--manifest", str(input_path),
             "--output-dir", str(self.output)], capture_output=True, text=True,
            encoding="utf-8", check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        outputs = self.read_outputs()
        after = {path.relative_to(self.output) for path in self.output.rglob("*") if path.is_file()}
        self.assertEqual(after - before, {Path("guide.html"), Path("guide.md"), Path("lesson.json")})
        parsed = PageParser()
        parsed.feed(outputs["guide.html"])
        payload = json.loads("".join(parsed.payload))
        self.assertEqual(payload["markdown"], outputs["guide.md"])
        self.assertEqual(payload["lesson"]["sections"][1]["paragraphs"], ["最后一章完整内容。"])
        for text in ("第一段完整内容。", "第二段完整内容。", "工具用途。", "打开工具。",
                     "核对输出。", "保留这项补充。", "最后一章完整内容。", "原始画面与细节"):
            self.assertIn(text, outputs["guide.md"])
        self.assertIn("frames/chapter%201.png", outputs["guide.md"])
        self.assertIn("INTERNAL_COVERAGE_SENTINEL", outputs["lesson.json"])
        self.assertNotIn("INTERNAL_COVERAGE_SENTINEL", outputs["guide.html"])
        self.assertNotIn("INTERNAL_COVERAGE_SENTINEL", outputs["guide.md"])

    def test_html_and_embedded_json_treat_user_content_as_text(self):
        attack = '</script><script>alert("payload")</script><img src=x onerror=alert(1)> & __LESSON_DATA__'
        self.manifest["title"] = attack
        self.manifest["quick_overview"] = [attack]
        self.manifest["sections"][0]["paragraphs"] = [attack]
        self.manifest["sections"][0]["note"] = attack
        self.manifest["sections"][0]["images"][0]["caption"] = attack
        renderer.render_lesson(self.manifest, self.output)
        outputs = self.read_outputs()
        parsed = PageParser()
        parsed.feed(outputs["guide.html"])
        self.assertEqual(sum(tag == "script" for tag, _ in parsed.tags), 2)
        self.assertFalse(any(tag == "img" and attrs.get("src") == "x" for tag, attrs in parsed.tags))
        self.assertFalse(any("onerror" in attrs for _, attrs in parsed.tags))
        payload = json.loads("".join(parsed.payload))
        self.assertEqual(payload["lesson"]["title"], attack)
        self.assertEqual(payload["lesson"]["quick_overview"], [attack])
        self.assertEqual(payload["lesson"]["sections"][0]["paragraphs"], [attack])
        self.assertNotIn("</script><script>alert", outputs["guide.html"])
        self.assertNotIn("<img src=x", outputs["guide.md"])
        self.assertIn("&lt;/script&gt;", outputs["guide.md"])
        self.assertIn("&amp;", outputs["guide.md"])

    def test_quick_overview_is_shared_by_reading_exports_before_preparation(self):
        self.manifest["quick_overview"] = ["  先配置环境，再验证连接。  ", "保留完整章节，不把概览当操作步骤。"]
        before = copy.deepcopy(self.manifest)
        renderer.render_lesson(self.manifest, self.output)
        outputs = self.read_outputs()
        saved = json.loads(outputs["lesson.json"])
        parsed = PageParser()
        parsed.feed(outputs["guide.html"])
        payload = json.loads("".join(parsed.payload))
        expected = ["先配置环境，再验证连接。", "保留完整章节，不把概览当操作步骤。"]
        self.assertEqual(saved["quick_overview"], expected)
        self.assertEqual(payload["lesson"]["quick_overview"], expected)
        self.assertEqual(payload["markdown"], outputs["guide.md"])
        self.assertLess(outputs["guide.md"].index("## 先看重点"), outputs["guide.md"].index("## 开始前"))
        self.assertIn("最后一章完整内容。", outputs["guide.md"])
        self.assertEqual(self.manifest, before)

    def test_quick_overview_rejects_nontext_blank_or_unbounded_input(self):
        for value in (None, "a summary", {}, [1], ["   "], ["\n\t"], ["x" * 301], ["point"] * 9):
            with self.subTest(value=value):
                bad = copy.deepcopy(self.manifest)
                bad["quick_overview"] = value
                self.assert_rejected_without_guides(bad)
        self.manifest["quick_overview"] = ["x" * 300] * 8
        self.assertEqual(len(renderer.validate_lesson(self.manifest, self.output)["quick_overview"]), 8)

    def test_empty_quick_overview_does_not_add_a_markdown_section(self):
        self.manifest["quick_overview"] = []
        renderer.render_lesson(self.manifest, self.output)
        self.assertNotIn("## 先看重点", self.read_outputs()["guide.md"])

    def test_code_fence_preserves_code_with_long_backtick_runs(self):
        code = "# exact code\n```markdown\ntext\n```\n````\n<script>code only</script>"
        self.manifest["sections"][0]["code_blocks"] = [{"language": "python", "code": code}]
        renderer.render_lesson(self.manifest, self.output)
        markdown = self.read_outputs()["guide.md"]
        self.assertIn("`````python\n" + code + "\n`````\n", markdown)
        broken = copy.deepcopy(self.manifest)
        broken["sections"][0]["code_blocks"][0]["language"] = "python\n<script>"
        with self.assertRaises(ValueError):
            renderer.validate_lesson(broken, self.output)

    def test_local_media_rejects_traversal_urls_absolute_and_missing_files(self):
        bad_paths = ["../outside.png", "frames/../../outside.png", "/tmp/a.png",
                     "C:/outside.png", "C:\\outside.png", "//host/a.png",
                     "https://example.org/a.png", "file:///a.png", "frames/missing.png",
                     "frames/%2e%2e/outside.png", "frames/../chapter 1.png", "frames/a.svg"]
        for path in bad_paths:
            with self.subTest(path=path):
                bad = copy.deepcopy(self.manifest)
                bad["sections"][0]["images"][0]["path"] = path
                self.assert_rejected_without_guides(bad)
        for path in ("../outside.mp4", "https://example.org/video.mp4", "missing.mp4"):
            with self.subTest(video=path):
                bad = copy.deepcopy(self.manifest)
                bad["video"] = path
                self.assert_rejected_without_guides(bad)

    def test_non_http_sources_and_reference_urls_are_rejected(self):
        for url in ("javascript:alert(1)", "data:text/html,test", "file:///tmp/source",
                    "https://example.org/\nattack", "https://user:secret@example.org/"):
            for field in ("source", "reference"):
                with self.subTest(url=url, field=field):
                    bad = copy.deepcopy(self.manifest)
                    if field == "source":
                        bad["source"]["url"] = url
                    else:
                        bad["references"][0]["url"] = url
                    self.assert_rejected_without_guides(bad)

    def test_time_ranges_and_nonfinite_values_are_rejected(self):
        mutations = [
            ("negative chapter", lambda m: m["sections"][0].update(start_seconds=-1)),
            ("reversed chapter", lambda m: m["sections"][0].update(start_seconds=11)),
            ("overlapping chapters", lambda m: m["sections"][1].update(start_seconds=9)),
            ("beyond duration", lambda m: m["sections"][1].update(end_seconds=21)),
            ("boolean time", lambda m: m["sections"][0].update(start_seconds=True)),
            ("nan time", lambda m: m["sections"][0].update(start_seconds=float("nan"))),
            ("infinite duration", lambda m: m["source"].update(duration_seconds=float("inf"))),
            ("early image", lambda m: m["sections"][0].update(start_seconds=6)),
            ("late image", lambda m: m["sections"][0]["images"][0].update(timestamp_seconds=11)),
            ("unordered images", lambda m: m["sections"][0]["images"].extend(
                [{"path": "frames/chapter 1.png", "timestamp_seconds": 4}])),
        ]
        for label, mutate in mutations:
            with self.subTest(label=label):
                bad = copy.deepcopy(self.manifest)
                mutate(bad)
                self.assert_rejected_without_guides(bad)

    def test_any_existing_output_blocks_whole_render_without_overwrite_flag(self):
        for existing in ("guide.html", "guide.md", "lesson.json"):
            with self.subTest(existing=existing):
                existing_path = self.output / existing
                existing_path.write_text("KEEP_EXISTING", encoding="utf-8")
                with self.assertRaises(FileExistsError):
                    renderer.render_lesson(self.manifest, self.output)
                self.assertEqual(existing_path.read_text(encoding="utf-8"), "KEEP_EXISTING")
                for other in {"guide.html", "guide.md", "lesson.json"} - {existing}:
                    self.assertFalse((self.output / other).exists())
                existing_path.unlink()
        renderer.render_lesson(self.manifest, self.output)
        self.manifest["title"] = "覆盖后的教材"
        renderer.render_lesson(self.manifest, self.output, overwrite_guide=True)
        self.assertEqual(json.loads(self.read_outputs()["lesson.json"])["title"], "覆盖后的教材")

    def test_omitted_optional_content_is_normalized_without_requiring_action_evidence(self):
        minimal = {"schema_version": 1, "kind": "lesson", "title": "概念课",
                   "source": {"name": "concept.mp4", "duration_seconds": 2},
                   "sections": [{"id": "concept", "title": "一个概念", "start_seconds": 0,
                                 "end_seconds": 2, "paragraphs": ["概念解释正文。"]}]}
        renderer.render_lesson(minimal, self.output)
        outputs = self.read_outputs()
        self.assertIn("概念解释正文。", outputs["guide.md"])
        saved = json.loads(outputs["lesson.json"])
        self.assertEqual(saved["sections"][0]["steps"], [])
        self.assertEqual(saved["quick_overview"], [])
        self.assertNotIn("## 先看重点", outputs["guide.md"])
        self.assertNotIn("action_evidence", saved)


if __name__ == "__main__":
    unittest.main()
