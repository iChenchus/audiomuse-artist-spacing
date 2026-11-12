#!/usr/bin/env python3
"""Package the plugin and write the catalog files.

    python build.py OWNER/REPO [BRANCH]

Zips plugins/artist_spacing (code only, no plugin.json) into
dist/artist_spacing/artist_spacing_<version>.zip, then fills in sourceUrl and
checksum for the newest entry of plugin.json and writes manifest.json, all
pointing at raw.githubusercontent.com/OWNER/REPO/BRANCH.

A version that already has a zip is never rebuilt: installed copies re-download
it by checksum, so a release must not change. Add a new entry at the top of
"versions" in plugin.json instead.
"""

import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLUGIN_DIR = ROOT / "plugins" / "artist_spacing"


def main():
    if len(sys.argv) < 2 or "/" not in sys.argv[1]:
        sys.exit(__doc__)
    repo = sys.argv[1].strip("/")
    branch = sys.argv[2] if len(sys.argv) > 2 else "main"
    raw = f"https://raw.githubusercontent.com/{repo}/{branch}"

    meta_path = PLUGIN_DIR / "plugin.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    plugin_id = meta["id"]
    release = meta["versions"][0]
    version = release["version"]

    zip_rel = Path("dist") / plugin_id / f"{plugin_id}_{version}.zip"
    zip_path = ROOT / zip_rel
    if zip_path.exists():
        print(f"{zip_rel} already exists, keeping it (bump the version to release new code)")
    else:
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(PLUGIN_DIR.rglob("*")):
                if path.is_dir() or path.name == "plugin.json" or "__pycache__" in path.parts:
                    continue
                info = zipfile.ZipInfo(path.relative_to(PLUGIN_DIR).as_posix(), (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zf.writestr(info, path.read_bytes())
        print(f"built {zip_rel}")

    for entry in meta["versions"]:
        rel = Path("dist") / plugin_id / f"{plugin_id}_{entry['version']}.zip"
        if (ROOT / rel).exists():
            entry["sourceUrl"] = f"{raw}/{rel.as_posix()}"
            entry["checksum"] = hashlib.md5((ROOT / rel).read_bytes()).hexdigest()
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    manifest = {
        "plugins": [
            {
                "id": plugin_id,
                "name": meta["name"],
                "author": meta["author"],
                "description": meta["description"],
                "pluginUrl": f"{raw}/plugins/{plugin_id}/plugin.json",
            }
        ]
    }
    (ROOT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"catalog: {raw}/manifest.json")


if __name__ == "__main__":
    main()
