"""Download model assets only; never upload user media or execute remote code."""
import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def prepare(kind="vlm", model_id=None):
    cache = ROOT / ".runtime" / "cache"
    locations = {"HF_HOME": cache / "huggingface", "HF_HUB_CACHE": cache / "huggingface" / "hub",
                 "HF_XET_CACHE": cache / "huggingface" / "xet", "TMP": cache / "tmp", "TEMP": cache / "tmp"}
    for name, path in locations.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    from huggingface_hub import HfApi, snapshot_download
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    prefix = "asr_" if kind == "asr" else ""
    repo = model_id or config[prefix + "model_id"]
    if model_id and model_id != config[prefix + "model_id"]:
        raise ValueError("预置下载入口只管理config.json中的固定模型，避免混合权重。其他已下载模型请通过 --model-dir 使用。")
    revision = config[prefix + "model_revision"]
    info = HfApi().model_info(repo, revision=revision)
    destination = ROOT / config[prefix + "model_dir"]
    provenance = destination / "model_source.json"
    if provenance.exists():
        previous = json.loads(provenance.read_text(encoding="utf-8"))
        if previous.get("repo_id") != repo or previous.get("revision") != info.sha:
            raise ValueError("模型目录已记录其他版本，请使用独立目录，保留原权重。")
    print(f"Downloading {repo}@{info.sha} to {destination}", flush=True)
    snapshot_download(repo, revision=info.sha, local_dir=str(destination),
                      allow_patterns=["*.json", "*.xml", "*.bin", "*.txt", "*.jinja", "README.md"], max_workers=4)
    source = {"repo_id": repo, "revision": info.sha, "url": "https://huggingface.co/" + repo,
              "downloaded_at": datetime.now(timezone.utc).isoformat(), "kind": kind,
              "remote_code_executed": False}
    (destination / "model_source.json").write_text(json.dumps(source, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "kind": kind, "model_dir": str(destination), "source": source}, ensure_ascii=False), flush=True)
    return source


if __name__ == "__main__":
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["vlm", "asr"], default="vlm")
    parser.add_argument("--model-id")
    options = parser.parse_args()
    prepare(options.kind, options.model_id)
