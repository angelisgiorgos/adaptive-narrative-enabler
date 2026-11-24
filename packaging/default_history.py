"""config/default_history.json: every value the default configuration has had, from git.

    python packaging/default_history.py          # rewrite it (after changing config/*.yaml)
    python packaging/default_history.py --check  # exit 1 when it misses the current defaults

A config folder kept from an older release (a Docker volume) is brought up to date on start
(utils/config_loader.upgrade_config_dir): a world entry or a setting whose value is one the defaults
once had was never edited, so it takes the current default; anything else was edited and is kept.
This file is how it tells the two apart: for every file, every entry (a place, an NPC, an item, an
event, a mission, a variable) and every setting path, the digests of every value it has had in
the repository's history and in the working tree. tests/test_studio_api.py checks that it covers
the current defaults.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils.config_loader import HISTORY_FILE, document_digests  # noqa: E402

OUT = ROOT / "config" / HISTORY_FILE


def _versions(name):
    """Every text config/<name> has had: each commit that touched it, then the working tree."""
    try:
        commits = subprocess.run(["git", "log", "--format=%H", "--", f"config/{name}"], cwd=ROOT, check=True,
                                 capture_output=True, text=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        commits = []
    for commit in commits:
        shown = subprocess.run(["git", "show", f"{commit}:config/{name}"], cwd=ROOT, capture_output=True, text=True)
        if shown.returncode == 0:
            yield shown.stdout
    path = ROOT / "config" / name
    if path.is_file():
        yield path.read_text(encoding="utf-8")


def build():
    files = {}
    for path in sorted((ROOT / "config").glob("*.yaml")):
        seen = {}
        for text in _versions(path.name):
            try:
                document = yaml.safe_load(text) or {}
            except yaml.YAMLError:
                continue
            for key, digest in document_digests(path.name, document):
                seen.setdefault(key, set()).add(digest)
        files[path.name] = {key: sorted(digests) for key, digests in sorted(seen.items())}
    return {"format": 1, "files": files, "shipped": {"hyperparameters.yaml": shipped_numbers("hyperparameters.yaml")}}


def shipped_numbers(name):
    """{dotted path: number} of config/<name> as it is released (the working tree being packaged): what
    the studio's Weights tab compares saved values with (backend/tuning.shipped_values)."""
    path = ROOT / "config" / name
    document = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    found = {}

    def visit(node, parts=()):
        if isinstance(node, dict):
            for key, child in node.items():
                visit(child, parts + (str(key),))
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            found[".".join(parts)] = node

    visit(document or {})
    return dict(sorted(found.items()))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    history = build()
    if "--check" in argv:
        current = json.loads(OUT.read_text(encoding="utf-8")) if OUT.is_file() else {"files": {}}
        missing = [f"{name}: {key}" for name, keys in history["files"].items() for key, digests in keys.items()
                   if not set(digests) <= set(current["files"].get(name, {}).get(key, []))]
        if missing:
            sys.exit(f"{OUT.name} misses {len(missing)} value(s), e.g. {missing[:3]}; run python {Path(__file__).name}")
        if current.get("shipped") != history["shipped"]:
            sys.exit(f"{OUT.name}'s shipped values are not the configuration's; run python {Path(__file__).name}")
        return
    # Values recorded before are kept (a copy without the repository's history, e.g. an exported source
    # folder, can only add the working tree's values: they must not replace the older ones).
    if OUT.is_file():
        before = json.loads(OUT.read_text(encoding="utf-8")).get("files", {})
        for name, keys in before.items():
            merged = history["files"].setdefault(name, {})
            for key, digests in keys.items():
                merged[key] = sorted(set(merged.get(key, [])) | set(digests))
            history["files"][name] = dict(sorted(merged.items()))
    OUT.write_text(json.dumps(history, indent=0, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)}: {sum(len(v) for v in history['files'].values())} entries and settings.")


if __name__ == "__main__":
    main()
