"""Construit le catalogue de plugins : plugins/dist/<id>-<version>.zip et plugins/index.json.

    python scripts/build_catalog.py          # reconstruit après avoir modifié un plugin
    python scripts/build_catalog.py --check  # vérifie que le catalogue est à jour (tests et GitHub Actions)

Les zips sont reproductibles (mêmes fichiers = même sha256), donc un plugin pas modifié garde son zip.
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from botpanel.plugins import PluginError, read_zip, validate_manifest  # noqa: E402

PLUGINS = ROOT / "plugins"
DIST = PLUGINS / "dist"
INDEX = PLUGINS / "index.json"
SKIP_DIRS = {"__pycache__", "tests", ".pytest_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo"}
STAMP = (2024, 1, 1, 0, 0, 0)


def plugin_dirs() -> list[Path]:
    return sorted(p for p in PLUGINS.iterdir() if p.is_dir() and (p / "plugin.json").is_file())


def build_zip(folder: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in sorted(folder.rglob("*")):
            rel = path.relative_to(folder)
            if path.is_dir() or SKIP_DIRS & set(rel.parts) or path.suffix in SKIP_SUFFIXES or path.name.startswith("."):
                continue
            info = zipfile.ZipInfo(rel.as_posix(), STAMP)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, path.read_bytes())
    return buffer.getvalue()


def build() -> tuple[dict, dict[str, bytes]]:
    entries, zips = [], {}
    for folder in plugin_dirs():
        manifest = validate_manifest(json.loads((folder / "plugin.json").read_text(encoding="utf-8")))
        if manifest["id"] != folder.name:
            raise PluginError(f"{folder.name} : l'id de plugin.json doit être le nom du dossier")
        data = build_zip(folder)
        read_zip(data, max_bytes=50 * 1024 * 1024)  # le panel doit l'accepter
        name = f"{manifest['id']}-{manifest['version']}.zip"
        zips[name] = data
        entries.append({k: manifest[k] for k in ("id", "name", "version", "description", "author", "runtime")}
                       | {"download": f"dist/{name}", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    index = {"format": 1, "name": "Catalogue de plugins Bot Panel", "plugins": entries}
    return index, zips


def main() -> int:
    index, zips = build()
    text = json.dumps(index, ensure_ascii=False, indent=2) + "\n"
    if "--check" in sys.argv:
        problems = [] if INDEX.is_file() and INDEX.read_text(encoding="utf-8") == text else ["plugins/index.json"]
        problems += [f"plugins/dist/{n}" for n, d in zips.items() if not (DIST / n).is_file() or (DIST / n).read_bytes() != d]
        if problems:
            print("Catalogue pas à jour (lance python scripts/build_catalog.py) :", ", ".join(problems))
            return 1
        print(f"Catalogue à jour : {len(zips)} plugin(s)")
        return 0
    DIST.mkdir(exist_ok=True)
    for old in DIST.glob("*.zip"):
        if old.name not in zips:
            old.unlink()
    for name, data in zips.items():
        (DIST / name).write_bytes(data)
    INDEX.write_text(text, encoding="utf-8")
    for entry in index["plugins"]:
        print(f"{entry['id']} {entry['version']} : {entry['size']} octets, sha256 {entry['sha256'][:12]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
