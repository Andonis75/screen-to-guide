"""Security and portability checks for the offline guide renderer."""
import copy
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from screen_guide.render import render_guide


class RendererTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.manifest = {"schema_version": 1, "title": "测试指南", "video": "source.mp4", "steps": [
            {"id": "step-001", "title": "打开窗口", "timestamp_seconds": 1.25, "image": "frames/01.jpg"}
        ]}

    def test_outputs_roundtrip_and_defaults(self):
        original = copy.deepcopy(self.manifest)
        paths = render_guide(self.manifest, self.root)
        self.assertEqual(set(paths), {"html", "markdown", "json"})
        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        self.assertEqual(data["steps"][0]["review_status"], "pending")
        self.assertEqual(self.manifest, original)
        page = paths["html"].read_text(encoding="utf-8")
        embedded = re.search(r'<script id="guide-data" type="application/json">(.*?)</script>', page, re.S).group(1)
        self.assertEqual(json.loads(embedded), data)
        self.assertIn("00:01.2", paths["markdown"].read_text(encoding="utf-8"))
        self.assertNotIn("innerHTML", page)
        self.assertNotRegex(page, r'<(?:script|link)[^>]+(?:src|href)=["\']https?://')

    def test_hostile_text_cannot_terminate_json_or_title(self):
        payload = '</script><img src=x onerror=alert(1)>&\u2028__GUIDE_DATA____GUIDE_TITLE__"'
        self.manifest["title"] = payload
        self.manifest["steps"][0]["description"] = payload
        paths = render_guide(self.manifest, self.root)
        page = paths["html"].read_text(encoding="utf-8")
        self.assertNotIn('</script><img', page)
        self.assertIn(r'\u003c/script\u003e', page)
        self.assertIn('&lt;/script&gt;', page)
        embedded = re.search(r'<script id="guide-data" type="application/json">(.*?)</script>', page, re.S).group(1)
        self.assertEqual(json.loads(embedded)["steps"][0]["description"], payload)
        self.assertEqual(json.loads(paths["json"].read_text(encoding="utf-8"))["title"], payload)

    def test_rejects_unsafe_paths_in_video_frames_and_steps(self):
        for path in ("../bad.mp4", ".. /bad.mp4", "/tmp/x.mp4", "C:/x.mp4", "https://x/a.mp4", "//server/x.mp4", "frames\\x.mp4", "frames/%2e%2e/x.mp4", "./x.mp4", "frames//x.mp4", "x.mp4?evil", "x\n.mp4"):
            with self.subTest(path=path):
                manifest = copy.deepcopy(self.manifest)
                manifest["video"] = path
                with self.assertRaises(ValueError):
                    render_guide(manifest, self.root)
        for field in ("frames", "steps"):
            manifest = {"schema_version": 1, field: [{"image": "../x.jpg"}]}
            with self.assertRaises(ValueError):
                render_guide(manifest, self.root)

    def test_sparse_manifest_and_null_media(self):
        paths = render_guide({"schema_version": 1}, self.root / "new")
        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        self.assertIsNone(data["video"])
        self.assertEqual(data["steps"], [])
        self.assertIn("暂无步骤", paths["markdown"].read_text(encoding="utf-8"))

    def test_steps_without_frames_are_explicit_unverified_drafts(self):
        self.manifest["steps"][0]["basis"] = "visual_inference"
        paths = render_guide(self.manifest, self.root)
        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        self.assertEqual(data["steps"][0]["basis"], "unverified_draft")
        self.assertIn("无证据草稿", data["steps"][0]["evidence_note"])
        self.assertIn("无证据草稿", paths["markdown"].read_text(encoding="utf-8"))
        self.manifest["steps"][0]["frame_ids"] = ["invented"]
        with self.assertRaisesRegex(ValueError, "unknown frame"):
            render_guide(self.manifest, self.root)

    def test_frame_binding_rejects_forgery_and_duplicate_ids(self):
        self.manifest["source"] = {"duration_seconds": 5}
        self.manifest["frames"] = [{"id": "frame-001", "timestamp_seconds": 1.25, "image": "frames/01.jpg"}]
        self.manifest["steps"][0].update(frame_ids=["frame-001"], end_seconds=2.0, basis="visual_inference")
        render_guide(self.manifest, self.root)
        mutations = [
            ("duplicate frames", lambda m: m["frames"].append(copy.deepcopy(m["frames"][0]))),
            ("duplicate steps", lambda m: m["steps"].append(copy.deepcopy(m["steps"][0]))),
            ("frame beyond duration", lambda m: m["frames"][0].update(timestamp_seconds=6)),
            ("invented frame", lambda m: m["steps"][0].update(frame_ids=["invented"])),
            ("missing frame reference", lambda m: m["steps"][0].update(frame_ids=[])),
            ("forged step time", lambda m: m["steps"][0].update(timestamp_seconds=1.26)),
            ("forged step image", lambda m: m["steps"][0].update(image="frames/other.jpg")),
            ("end beyond duration", lambda m: m["steps"][0].update(end_seconds=6)),
            ("end before start", lambda m: m["steps"][0].update(end_seconds=1)),
            ("invalid duration", lambda m: m["source"].update(duration_seconds="5")),
            ("missing frame id", lambda m: m["frames"][0].pop("id")),
            ("missing frame time", lambda m: m["frames"][0].pop("timestamp_seconds")),
        ]
        for description, mutate in mutations:
            manifest = copy.deepcopy(self.manifest)
            mutate(manifest)
            with self.subTest(description=description), self.assertRaises(ValueError):
                render_guide(manifest, self.root)
        self.manifest["steps"][0]["timestamp_seconds"] += 5e-7
        render_guide(self.manifest, self.root)
        self.manifest.pop("source")
        render_guide(self.manifest, self.root)

    def test_first_referenced_frame_is_primary(self):
        self.manifest["frames"] = [
            {"id": "a", "timestamp_seconds": 1.25, "image": "frames/01.jpg"},
            {"id": "b", "timestamp_seconds": 2, "image": "frames/02.jpg"},
        ]
        self.manifest["steps"][0]["frame_ids"] = ["a", "b"]
        render_guide(self.manifest, self.root)
        self.manifest["steps"][0]["frame_ids"] = ["b", "a"]
        with self.assertRaises(ValueError):
            render_guide(self.manifest, self.root)

    def test_rejects_invalid_schema_and_values(self):
        for manifest in ({}, {"schema_version": True}, {"schema_version": 2}, {"schema_version": 1, "steps": "bad"},
                         {"schema_version": 1, "steps": [{"timestamp_seconds": float("nan")}]},
                         {"schema_version": 1, "steps": [{"review_status": "approved"}]},
                         {"schema_version": 1, "steps": [{"review_status": []}]},
                         {"schema_version": 1, "steps": [{"observed_text": [42]}]},
                         {"schema_version": 1, "steps": [{"timestamp_seconds": 2, "end_seconds": 1}]}):
            with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                render_guide(manifest, self.root)

    def test_markdown_escapes_html_and_links(self):
        self.manifest["steps"][0]["description"] = '<script>alert(1)</script> [link](https://evil)'
        paths = render_guide(self.manifest, self.root)
        md = paths["markdown"].read_text(encoding="utf-8")
        self.assertNotIn("<script>", md)
        self.assertIn(r"\[link\]\(https://evil\)", md)

    def test_transcript_exports_escaped_and_is_validated(self):
        self.manifest["source"] = {"has_audio": True}
        self.manifest["transcript"] = [{"start_seconds": 0, "end_seconds": 2, "text": "<b>点击</b>导出"}]
        paths = render_guide(self.manifest, self.root)
        md = paths["markdown"].read_text(encoding="utf-8")
        self.assertIn("语音转写（需核对）", md)
        self.assertIn("&lt;b&gt;点击&lt;/b&gt;导出", md)
        self.manifest["source"]["has_audio"] = False
        paths = render_guide(self.manifest, self.root)
        self.assertNotIn("语音转写", paths["markdown"].read_text(encoding="utf-8"))
        self.manifest["transcript"][0]["end_seconds"] = -1
        with self.assertRaises(ValueError):
            render_guide(self.manifest, self.root)

    def test_transcript_types_finiteness_and_order(self):
        valid = {"start_seconds": 0, "end_seconds": 1, "text": "示例"}
        for field, value in (("text", 7), ("text", None), ("start_seconds", "0"),
                             ("end_seconds", True), ("start_seconds", float("inf")),
                             ("end_seconds", float("nan")), ("end_seconds", -1),
                             ("start_seconds", 2), ("end_seconds", 10**400)):
            self.manifest["transcript"] = [{**valid, field: value}]
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                render_guide(self.manifest, self.root)
        self.manifest["transcript"] = [{}]
        with self.assertRaises(ValueError):
            render_guide(self.manifest, self.root)

    def tutorial_manifest(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["source"] = {"duration_seconds": 9, "has_audio": True}
        manifest["frames"] = [{"id": "frame-001", "timestamp_seconds": 1.25, "image": "frames/01.jpg"}]
        manifest["tutorial"] = {"summary": "创建一份 PDF 报告。", "prerequisites": ["打开示例软件"],
                                "example_settings": [{"label": "导出格式", "value": "PDF"}], "completion": "报告已导出。"}
        manifest["steps"][0].update(frame_ids=["frame-001"], actions=["点击新建报告", "选择 PDF"],
                                    outcome="出现导出设置", tip="可以使用默认参数。", review_status="pending",
                                    observed_text=["OCR_ONLY_SENTINEL"], review_notes="REVIEW_ONLY_SENTINEL")
        manifest["transcript"] = [{"start_seconds": 0, "end_seconds": 2, "text": "ASR_ONLY_SENTINEL"}]
        return manifest

    def test_tutorial_selects_reading_template_and_clean_markdown(self):
        manifest = self.tutorial_manifest()
        original = copy.deepcopy(manifest)
        paths = render_guide(manifest, self.root)
        page = paths["html"].read_text(encoding="utf-8")
        markdown = paths["markdown"].read_text(encoding="utf-8")
        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        self.assertEqual(manifest, original)
        self.assertEqual(data["tutorial"], manifest["tutorial"])
        self.assertEqual(data["steps"][0]["review_status"], "pending")
        self.assertEqual(data["transcript"], manifest["transcript"])
        self.assertEqual(data["steps"][0]["review_notes"], "REVIEW_ONLY_SENTINEL")
        self.assertIn('id="toc"', page)
        self.assertIn('id="print-guide"', page)
        self.assertIn('<dialog id="player"', page)
        self.assertNotIn('id="step-list"', page)
        self.assertIn("## 开始前", markdown)
        self.assertIn("| 导出格式 | PDF |", markdown)
        self.assertIn("1. 点击新建报告", markdown)
        self.assertIn("**完成后：** 出现导出设置", markdown)
        self.assertIn("**小提示：** 可以使用默认参数", markdown)
        self.assertIn("## 完成", markdown)
        visible_template = re.sub(r'<script id="guide-data".*?</script>', '', page, flags=re.S)
        for text in ("待复核", "模型原稿", "OCR_ONLY_SENTINEL", "ASR_ONLY_SENTINEL", "REVIEW_ONLY_SENTINEL"):
            self.assertNotIn(text, markdown)
            self.assertNotIn(text, visible_template)
        self.assertNotIn("innerHTML", page)

    def test_tutorial_optional_content_and_description_fallback(self):
        manifest = self.tutorial_manifest()
        manifest["tutorial"] = {}
        step = manifest["steps"][0]
        for field in ("actions", "outcome", "tip"):
            step.pop(field)
        step["description"] = "填写报告名称，然后选择导出。"
        paths = render_guide(manifest, self.root)
        markdown = paths["markdown"].read_text(encoding="utf-8")
        self.assertIn(step["description"], markdown)
        self.assertNotIn("## 开始前", markdown)
        self.assertNotIn("## 本例参数", markdown)
        self.assertNotIn("**完成后", markdown)
        self.assertIn('id="toc"', paths["html"].read_text(encoding="utf-8"))
        paths = render_guide({"schema_version": 1, "tutorial": {}, "video": None}, self.root)
        self.assertIn('id="toc"', paths["html"].read_text(encoding="utf-8"))

    def test_tutorial_fields_validate_types_and_preserve_evidence_checks(self):
        mutations = [
            lambda m: m.update(tutorial=None), lambda m: m.update(tutorial=[]),
            lambda m: m["tutorial"].update(summary=7), lambda m: m["tutorial"].update(completion=False),
            lambda m: m["tutorial"].update(prerequisites="bad"), lambda m: m["tutorial"].update(prerequisites=[{}]),
            lambda m: m["tutorial"].update(example_settings={}),
            lambda m: m["tutorial"].update(example_settings=[{"label": "a"}]),
            lambda m: m["tutorial"].update(example_settings=[{"label": 1, "value": "b"}]),
            lambda m: m["steps"][0].update(actions="bad"), lambda m: m["steps"][0].update(actions=[7]),
            lambda m: m["steps"][0].update(outcome={}), lambda m: m["steps"][0].update(tip=None),
            lambda m: m["steps"][0].update(timestamp_seconds=2),
            lambda m: m["steps"][0].update(image="../secret.jpg"),
        ]
        for index, mutate in enumerate(mutations):
            manifest = self.tutorial_manifest()
            mutate(manifest)
            with self.subTest(index=index), self.assertRaises(ValueError):
                render_guide(manifest, self.root)

    def test_tutorial_untrusted_fields_remain_data(self):
        manifest = self.tutorial_manifest()
        payload = '</script><img src=x onerror=alert(1)> __GUIDE_TITLE__ __GUIDE_DATA__'
        manifest["tutorial"].update(summary=payload, prerequisites=[payload], completion=payload,
                                    example_settings=[{"label": payload, "value": payload}])
        manifest["steps"][0].update(actions=[payload], outcome=payload, tip=payload)
        paths = render_guide(manifest, self.root)
        page = paths["html"].read_text(encoding="utf-8")
        self.assertNotIn('</script><img', page)
        embedded = re.search(r'<script id="guide-data" type="application/json">(.*?)</script>', page, re.S).group(1)
        self.assertEqual(json.loads(embedded)["tutorial"]["summary"], payload)
        self.assertEqual(json.loads(embedded)["steps"][0]["actions"], [payload])
        self.assertNotIn("<img src=x", paths["markdown"].read_text(encoding="utf-8"))

    def test_playback_start_is_separate_from_bound_screenshot(self):
        manifest = self.tutorial_manifest()
        manifest["steps"][0]["playback_start_seconds"] = 0
        paths = render_guide(manifest, self.root)
        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        self.assertEqual(data["steps"][0]["timestamp_seconds"], 1.25)
        self.assertEqual(data["steps"][0]["playback_start_seconds"], 0)
        self.assertIn("录屏时间：00:00.0", paths["markdown"].read_text(encoding="utf-8"))
        manifest["steps"][0]["playback_start_seconds"] = 2
        with self.assertRaises(ValueError):
            render_guide(manifest, self.root)
        paths = render_guide(self.manifest, self.root)
        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        self.assertEqual(data["steps"][0]["playback_start_seconds"], data["steps"][0]["timestamp_seconds"])
        self.assertIn('id="step-list"', paths["html"].read_text(encoding="utf-8"))

    def test_rejects_symlink_escape_when_supported(self):
        outside = self.root.parent / (self.root.name + "-outside")
        try:
            (self.root / "frames").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation unavailable on this host")
        with self.assertRaises(ValueError):
            render_guide(self.manifest, self.root)


if __name__ == "__main__":
    unittest.main()
