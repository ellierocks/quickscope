#!/usr/bin/env python3
"""Package the built plugin as out/Quickscope.zip, ready for Decky's
"Install Plugin from ZIP" or a GitHub release.

Run after `pnpm build`. Mirrors the Decky CLI layout: everything under a
top-level Quickscope/ folder, with defaults/* copied into the plugin root.
"""
import json
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "out" / "Quickscope.zip"
FILES = ["plugin.json", "package.json", "main.py", "README.md", "LICENSE", "dist/index.js"]


def main():
    missing = [f for f in FILES if not (ROOT / f).is_file()]
    if missing:
        sys.exit(f"missing {', '.join(missing)} (run `pnpm build` first)")
    name = json.loads((ROOT / "plugin.json").read_text())["name"]
    OUT.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        entries = [(f, ROOT / f) for f in FILES]
        entries += [(p.name, p) for p in sorted((ROOT / "defaults").iterdir()) if p.is_file()]
        for dest, src in entries:
            # Everything here runs on SteamOS; never ship CRLF line endings.
            z.writestr(f"{name}/{dest}", src.read_bytes().replace(b"\r\n", b"\n"))
    print(f"{OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KiB)")


if __name__ == "__main__":
    main()
