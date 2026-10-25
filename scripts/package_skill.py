"""Build a distributable Skill archive without models, outputs or local caches."""
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    version = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))["version"]
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version):
        raise ValueError("config.json version must be a semantic version")
    target = ROOT.parent / f"screen-to-guide-v{version}.zip"
    allowed = {"SKILL.md", "README.md", "THIRD_PARTY_NOTICES.md", "LICENSE", "config.json",
               "requirements.txt", "requirements-dev.txt", "requirements-download.txt", ".gitignore"}
    folders = {"scripts", "assets", "agents", "references", "tests", "vendor", "docs", "examples"}
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sorted(ROOT.rglob("*")):
            if not file.is_file():
                continue
            relative = file.relative_to(ROOT)
            if relative.parts[0] not in folders and relative.as_posix() not in allowed:
                continue
            if any(part in {"__pycache__", ".pytest_cache", ".runtime", ".git"} for part in relative.parts) or file.suffix == ".pyc":
                continue
            if file.is_symlink() or not file.resolve().is_relative_to(ROOT):
                raise ValueError("Archive members must be regular files inside this Skill")
            archive.write(file, (Path("screen-to-guide") / relative).as_posix())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".zip.sha256").write_text(digest + "  " + target.name + "\n", encoding="ascii")
    print(target)
    print(digest)


if __name__ == "__main__":
    main()
