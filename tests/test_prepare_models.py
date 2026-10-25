import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_models


@pytest.fixture
def model_root():
    with tempfile.TemporaryDirectory(prefix="screen-guide-model-test-") as directory:
        yield Path(directory)


def test_explicit_default_model_keeps_pinned_revision(model_root, monkeypatch):
    tmp_path = model_root
    config = {"model_id": "example/model", "model_revision": "pinned-commit", "model_dir": "models/vlm"}
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(prepare_models, "ROOT", tmp_path)
    requested = []

    class FakeApi:
        def model_info(self, repo, revision=None):
            requested.append((repo, revision))
            return SimpleNamespace(sha=revision or "unwanted-latest")

    def download(repo, revision, local_dir, **kwargs):
        Path(local_dir).mkdir(parents=True)
        requested.append((repo, revision))

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(HfApi=FakeApi, snapshot_download=download))
    source = prepare_models.prepare(model_id="example/model")
    assert requested == [("example/model", "pinned-commit"), ("example/model", "pinned-commit")]
    assert source["revision"] == "pinned-commit"


def test_custom_model_rejected_before_network_request(model_root, monkeypatch):
    tmp_path = model_root
    config = {"model_id": "example/model", "model_revision": "pinned", "model_dir": "models/vlm"}
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(prepare_models, "ROOT", tmp_path)
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(HfApi=lambda: pytest.fail("Unexpected network call"), snapshot_download=lambda **kw: None))
    with pytest.raises(ValueError, match="固定模型"):
        prepare_models.prepare(model_id="another/model")
