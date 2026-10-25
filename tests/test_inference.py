import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from screen_guide.inference import evidence_steps, parse_observation


def test_observation_cannot_choose_evidence_paths_timestamps_or_approval():
    result = parse_observation('```json\n{"title":"导出结果", "description":"显示完成提示", "timestamp_seconds":999,"image":"https://x/a.png","review_status":"confirmed"}\n```')
    assert set(result) == {"title", "description", "uncertainty"}
    assert result["title"] == "导出结果"


@pytest.mark.parametrize("raw", ['{"title": "", "description":"x"}', "not json", "[1,2]", '{"title":3,"description":"x"}', "x" * 20001])
def test_invalid_model_output_is_not_a_step(raw):
    with pytest.raises(ValueError):
        parse_observation(raw)


def test_evidence_keeps_source_time_and_requires_review():
    frames = [{"id":"f1", "timestamp_seconds":1.2, "image":"frames/f1.jpg", "ocr":[{"text":"候选", "confidence":0.9}, {"text":"低分", "confidence":0.2}]},
              {"id":"f2", "timestamp_seconds":3.4, "image":"frames/f2.jpg"}]
    steps = evidence_steps(frames, 9)
    assert [s["timestamp_seconds"] for s in steps] == [1.2,3.4]
    assert [s["end_seconds"] for s in steps] == [3.4,9]
    assert all(s["review_status"] == "pending" for s in steps)
    assert steps[0]["observed_text"] == ["候选"]
