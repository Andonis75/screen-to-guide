"""Behavioral publication checks for action/automatic-transition evidence."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from screen_guide.render import render_guide


class ActionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.manifest = {
            "schema_version": 1, "title": "导出报告", "source": {"duration_seconds": 10},
            "tutorial": {"action_evidence_required": True},
            "frames": [
                {"id": "before", "timestamp_seconds": 1, "image": "frames/before.jpg"},
                {"id": "after", "timestamp_seconds": 2, "image": "frames/after.jpg"},
                {"id": "done", "timestamp_seconds": 3, "image": "frames/done.jpg"},
            ],
            "steps": [{"id": "step-001", "title": "保存报告", "timestamp_seconds": 2,
                       "image": "frames/after.jpg", "frame_ids": ["after"],
                       "actions": ["点击保存按钮。", "等待导出完成提示。"],
                       "action_refs": [["save-click"], ["export-completes"]]}],
            "action_evidence": [
                {"id": "save-click", "event_id": "save", "kind": "click",
                 "frame_ids": ["before", "after"], "start_seconds": 1, "end_seconds": 2,
                 "support": "observed_transition", "description": "保存前后状态与按钮触发。"},
                {"id": "export-completes", "event_id": "export", "kind": "automatic",
                 "frame_ids": ["after", "done"], "start_seconds": 2, "end_seconds": 3,
                 "support": "system_transition", "description": "导出进度自行结束并出现结果。"},
            ],
        }

    def test_explicit_click_and_automatic_wait_publish_without_mutation(self):
        original = copy.deepcopy(self.manifest)
        paths = render_guide(self.manifest, self.root)
        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        self.assertEqual(self.manifest, original)
        self.assertEqual(data["action_evidence"], original["action_evidence"])
        self.assertEqual(data["steps"][0]["action_refs"], [["save-click"], ["export-completes"]])
        markdown = paths["markdown"].read_text(encoding="utf-8")
        self.assertNotIn("system_transition", markdown)
        self.assertNotIn("action_refs", markdown)
        self.assertIn("等待导出完成提示", markdown)

    def test_missing_misaligned_or_unknown_action_references_are_rejected(self):
        for value in (None, [], [["save-click"]], [["save-click"], []],
                      [["invented"], ["export-completes"]],
                      [["save-click", "save-click"], ["export-completes"]]):
            manifest = copy.deepcopy(self.manifest)
            manifest["steps"][0]["action_refs"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                render_guide(manifest, self.root)

    def test_unknown_frame_and_invalid_interval_are_rejected(self):
        mutations = [
            lambda record: record.update(frame_ids=["invented"]),
            lambda record: record.update(frame_ids=[]),
            lambda record: record.update(start_seconds=1.5),
            lambda record: record.update(start_seconds=3),
            lambda record: record.update(end_seconds=11),
            lambda record: record.update(end_seconds=True),
            lambda record: record.update(start_seconds="1"),
            lambda record: record.update(start_seconds=10**400),
        ]
        for index, mutate in enumerate(mutations):
            manifest = copy.deepcopy(self.manifest)
            mutate(manifest["action_evidence"][0])
            with self.subTest(index=index), self.assertRaises(ValueError):
                render_guide(manifest, self.root)

    def test_inferred_or_static_click_evidence_does_not_publish(self):
        mutations = [
            lambda r: r.update(support="inferred"),
            lambda r: r.update(support="visible_value"),
            lambda r: r.update(support="system_transition"),
            lambda r: r.update(frame_ids=["before"]),
        ]
        for mutate in mutations:
            manifest = copy.deepcopy(self.manifest)
            mutate(manifest["action_evidence"][0])
            with self.subTest(record=manifest["action_evidence"][0]), self.assertRaises(ValueError):
                render_guide(manifest, self.root)

    def test_same_event_cannot_be_automatic_and_a_second_click(self):
        manifest = copy.deepcopy(self.manifest)
        duplicate = copy.deepcopy(manifest["action_evidence"][1])
        duplicate.update(id="invented-resend", kind="click", support="observed_transition")
        manifest["action_evidence"].append(duplicate)
        manifest["steps"][0]["actions"].append("再次点击发送。")
        manifest["steps"][0]["action_refs"].append(["invented-resend"])
        with self.assertRaisesRegex(ValueError, "Conflicting automatic and manual"):
            render_guide(manifest, self.root)
        # Renaming the event id does not make identical footage a new click.
        duplicate["event_id"] = "renamed-export"
        with self.assertRaisesRegex(ValueError, "Conflicting automatic and manual"):
            render_guide(manifest, self.root)

    def test_visible_value_supports_configuration_without_claiming_typing(self):
        manifest = copy.deepcopy(self.manifest)
        record = manifest["action_evidence"][0]
        record.update(kind="input", support="visible_value", frame_ids=["after"])
        manifest["steps"][0]["actions"][0] = "将格式设为 PDF。"
        render_guide(manifest, self.root)

    def test_automatic_record_requires_system_transition(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["action_evidence"][1]["support"] = "observed_transition"
        with self.assertRaisesRegex(ValueError, "does not justify automatic"):
            render_guide(manifest, self.root)

    def test_duration_flag_and_evidence_types_are_checked(self):
        mutations = [
            lambda m: m["source"].pop("duration_seconds"),
            lambda m: m["tutorial"].update(action_evidence_required="true"),
            lambda m: m["tutorial"].update(action_evidence_required=1),
            lambda m: m.update(action_evidence={}),
            lambda m: m["action_evidence"].append(copy.deepcopy(m["action_evidence"][0])),
            lambda m: m["action_evidence"][0].update(kind="guess"),
            lambda m: m["action_evidence"][0].update(description=""),
        ]
        for index, mutate in enumerate(mutations):
            manifest = copy.deepcopy(self.manifest)
            mutate(manifest)
            with self.subTest(index=index), self.assertRaises(ValueError):
                render_guide(manifest, self.root)

    def test_strict_empty_evidence_steps_or_actions_do_not_publish(self):
        mutations = [
            lambda m: m.update(action_evidence=[]),
            lambda m: m.update(steps=[]),
            lambda m: m["steps"][0].update(actions=[], action_refs=[]),
        ]
        for index, mutate in enumerate(mutations):
            manifest = copy.deepcopy(self.manifest)
            mutate(manifest)
            with self.subTest(index=index), self.assertRaises(ValueError):
                render_guide(manifest, self.root)

    def test_legacy_tutorial_without_gate_stays_compatible(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["tutorial"].pop("action_evidence_required")
        manifest.pop("action_evidence")
        manifest["steps"][0].pop("action_refs")
        render_guide(manifest, self.root)


if __name__ == "__main__":
    unittest.main()
