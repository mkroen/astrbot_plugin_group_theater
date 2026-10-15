"""Build a reproducible allowlisted plugin archive without local settings or keys."""

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import yaml

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output-dir", type=Path, default=root / "dist")
args = parser.parse_args()
metadata = yaml.safe_load((root / "metadata.yaml").read_text(encoding="utf-8"))
json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
files = [
    root / x
    for x in (
        "main.py",
        "metadata.yaml",
        "_conf_schema.json",
        "requirements.txt",
        "requirements-dev.txt",
        "README.md",
        "LICENSE",
        "pyproject.toml",
        ".gitignore",
        ".gitattributes",
        "CHANGELOG.md",
    )
]
for folder, pattern in [("core", "*.py"), ("tests", "*.py"), ("tools", "*.py")]:
    files.extend((root / folder).glob(pattern))
destination = args.output_dir / f"{metadata['name']}-{metadata['version']}.zip"
destination.parent.mkdir(parents=True, exist_ok=True)
with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
    for path in sorted(files):
        name = f"{metadata['name']}/{path.relative_to(root).as_posix()}"
        info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, path.read_bytes())
with ZipFile(destination) as archive:
    assert archive.testzip() is None
print(destination)
print(f"Files: {len(files)}, bytes: {destination.stat().st_size}")
print("SHA256: " + hashlib.sha256(destination.read_bytes()).hexdigest())
