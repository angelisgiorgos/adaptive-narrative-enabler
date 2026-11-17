"""The engine's code version and the runtime protocol version.

``code_version()`` fingerprints the engine code an exported ANE AI runs (core_engine,
narrative_runtime and the two utils modules it ships with): the API and a runtime built from
the same commit agree on it, so a snapshot or an export can tell whether it fits. A frozen
runtime (PyInstaller) has no sources to hash: its build writes ``_build_info.py`` with the
value computed from the sources it was built from.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

PROTOCOL = 1
ROOT = Path(__file__).resolve().parent.parent
_CACHE = {}


def runtime_files(root=ROOT):
    """The source files of the offline runtime (relative paths), sorted."""
    files = [path for path in sorted((root / "core_engine").glob("*.py"))
             if path.name not in ("augmenter.py", "_build_info.py")]
    files += sorted((root / "narrative_runtime").glob("*.py"))
    files += [root / "utils" / "config_loader.py", root / "utils" / "tag_similarity.py"]
    return [path for path in files if path.exists()]


def source_version(root=ROOT):
    """The fingerprint of the runtime's sources (line endings normalised)."""
    digest = hashlib.sha256()
    for path in runtime_files(root):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()[:16]


def code_version():
    if "version" not in _CACHE:
        try:
            from ._build_info import CODE_VERSION  # written when a runtime is frozen
            _CACHE["version"] = CODE_VERSION
        except ImportError:
            _CACHE["version"] = source_version()
    return _CACHE["version"]
