"""Make an engine package from this repository's configuration, without the studio: for testing
a runtime build (the API's export makes the real ones, with a trained storyteller).

    python packaging/make_package.py --out pkg [--seed 5] [--with-code]

--with-code adds the runtime's Python code and run.py, so `python run.py serve` runs it.
"""
from __future__ import annotations

import argparse
import datetime
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", type=int, default=5)
    parser.add_argument("--name", default="ane-test-package")
    parser.add_argument("--with-code", action="store_true", help="add the runtime code and run.py (Python)")
    args = parser.parse_args(argv)
    import yaml

    from core_engine.genome import Genome
    from core_engine.model import world_tags
    from narrative_runtime.engine import FORMAT
    from utils.config_loader import config
    from utils.tag_similarity import export_tag_vectors

    out = Path(args.out)
    (out / "config").mkdir(parents=True, exist_ok=True)
    (out / "data").mkdir(exist_ok=True)
    active = config.as_dict()
    world = active.get("world_definition") or {}
    manifest = {"format": FORMAT, "version": 1, "name": args.name, "source": "test package",
                "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "score": None,
                "genome": Genome(rng=random.Random(args.seed)).traits(), "learning": None,
                "world": {"locations": len(world.get("locations") or {}), "npcs": len(world.get("characters") or [])}}
    (out / "engine.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out / "config" / "engine.yaml").write_text(yaml.safe_dump(active, sort_keys=False, allow_unicode=True),
                                                encoding="utf-8")
    (out / "data" / "tag_vectors.json").write_text(json.dumps(export_tag_vectors(world_tags(active))), encoding="utf-8")
    if args.with_code:
        import shutil

        from core_engine import version

        for path in version.runtime_files():
            target = out / path.relative_to(version.ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        (out / "run.py").write_text("from narrative_runtime.__main__ import main\n\nif __name__ == \"__main__\":\n    main()\n",
                                    encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
