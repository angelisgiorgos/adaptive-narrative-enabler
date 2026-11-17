"""Build the standalone ANE AI runtime for this machine's OS (PyInstaller, onedir) and zip it.

    python packaging/build_runtime.py --target windows-x64 [--dist dist]

The zip (dist/ane-runtime-<target>.zip) holds the folder's content at its root: the program
`ane-ai` (`ane-ai.exe`), `_internal/` and `runtime.json` (the code version, the protocol, the
Python version and the target). The API puts an engine package next to it to make an ANE AI
download (POST /v1/exports with `platform`).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core_engine import version  # noqa: E402

TARGETS = ("windows-x64", "linux-x64", "macos-arm64", "macos-x64")


def zip_folder(folder, archive):
    """Zip a folder's content at the archive's root, keeping execute bits."""
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(folder.rglob("*")):
            if path.is_dir():
                continue
            info = zipfile.ZipInfo.from_file(path, path.relative_to(folder).as_posix())
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(path, "rb") as stream:
                zipped.writestr(info, stream.read())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--target", required=True, choices=TARGETS)
    parser.add_argument("--dist", default=str(ROOT / "dist"))
    args = parser.parse_args(argv)
    dist, work = Path(args.dist), Path(args.dist) / "build"
    code_version = version.source_version()
    build_info = ROOT / "core_engine" / "_build_info.py"
    build_info.write_text(f'"""Written by packaging/build_runtime.py."""\nCODE_VERSION = "{code_version}"\n',
                          encoding="utf-8")
    try:
        subprocess.run([sys.executable, "-m", "PyInstaller", str(ROOT / "packaging" / "ane_ai.spec"), "--noconfirm",
                        "--distpath", str(dist), "--workpath", str(work)], check=True, cwd=ROOT)
    finally:
        build_info.unlink(missing_ok=True)
    folder = dist / "ane-ai"
    executable = "ane-ai.exe" if os.name == "nt" else "ane-ai"
    runtime = {"format": "ane.runtime/1", "target": args.target, "code_version": code_version,
               "protocol": version.PROTOCOL, "python": platform.python_version(), "executable": executable,
               "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    (folder / "runtime.json").write_text(json.dumps(runtime, indent=2), encoding="utf-8")
    archive = dist / f"ane-runtime-{args.target}.zip"
    zip_folder(folder, archive)
    shutil.rmtree(work, ignore_errors=True)
    print(json.dumps({**runtime, "zip": str(archive)}))


if __name__ == "__main__":
    main()
