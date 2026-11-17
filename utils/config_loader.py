import yaml
import os
import copy

class Config:
    def __init__(self, config_dir="config"):
        self._config = {}
        self._config_dir = config_dir
        self._override = None
        self.reload()

    def reload(self):
        loaded = {}
        config_dir = self._config_dir
        if os.path.isdir(config_dir):
            for filename in sorted(os.listdir(config_dir)):
                if filename.endswith(".yaml"):
                    path = os.path.join(config_dir, filename)
                    with open(path, "r", encoding="utf-8") as f:
                        new_config = yaml.safe_load(f)
                        if new_config:
                            self._deep_merge(loaded, new_config)
        elif os.path.isfile(config_dir):
            # Fallback for single file
            with open(config_dir, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
        self._default_config = loaded
        self._config = copy.deepcopy(self._override if self._override is not None else loaded)

    def use_override(self, new_config):
        """Use a complete, already validated configuration until it is cleared."""
        self._override = copy.deepcopy(new_config)
        self._config = copy.deepcopy(new_config)

    def clear_override(self):
        self._override = None
        self.reload()

    def default_config(self):
        return copy.deepcopy(self._default_config)

    def as_dict(self):
        """Return a safe copy of the currently active merged configuration."""
        return copy.deepcopy(self._config)

    @property
    def using_override(self):
        return self._override is not None

    def _deep_merge(self, base, update):
        for k, v in update.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                self._deep_merge(base[k], v)
            elif k in base and isinstance(base[k], list) and isinstance(v, list):
                base[k].extend(v)
            else:
                base[k] = v

    def get(self, key, default=None):
        keys = key.split('.')
        value = self._config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            if value is None:
                return default
        return value

    def set_world_definition(self, new_definition):
        if "world_definition" not in self._config:
            self._config["world_definition"] = {}
        self._config["world_definition"] = new_definition

# Singleton instance
# Look for 'config' directory in the project root
project_root = os.path.dirname(os.path.dirname(__file__))
config_dir = os.path.join(project_root, "config")
if not os.path.exists(config_dir):
    # Fallback to single file if directory missing (transition phase)
    config_dir = os.path.join(project_root, "config.yaml")

config = Config(config_dir)


# ---- a config folder kept from an older release (a Docker volume) ------------------------------

UPGRADED_FILES = {  # file -> (world_definition key, shape)
    "locations.yaml": ("locations", "mapping"),
    "npcs.yaml": ("characters", "list"),
    "items.yaml": ("objects", "list"),
    "events.yaml": ("events", "mapping"),
    "missions.yaml": ("missions", "mapping"),
}
OFFERED_FILE = ".ane-default-entries.json"


def _named(entries, shape):
    if shape == "mapping":
        return dict(entries) if isinstance(entries, dict) else {}
    return {e["name"]: e for e in entries or [] if isinstance(e, dict) and e.get("name")}


def _read_yaml(path):
    with open(path, "r", encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {}


def _replace(path, text):
    """Write a file atomically, keeping its permissions."""
    import tempfile

    mode = os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o644
    handle, temporary = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".upgrade-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def upgrade_config_dir(defaults_dir, target_dir, log=print):
    """Bring a config folder kept from an older release (a Docker volume) up to date without changing
    what is in it: each default entry (place, NPC, item, event, mission) that the folder lacks is
    added, once. A release's new entries refer to each other (a new mission names its new NPC), and
    files copied whole from different releases otherwise make a world that cannot be played. An entry
    deleted afterwards stays deleted: the names already offered are kept in .ane-default-entries.json.
    Returns the (file, name) pairs added."""
    import json

    lock = open(os.path.join(target_dir, ".ane-upgrade.lock"), "a")
    try:
        try:  # the studio's and the API's containers start together on one volume
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX)
        except ImportError:
            pass
        offered_path = os.path.join(target_dir, OFFERED_FILE)
        try:
            with open(offered_path, "r", encoding="utf-8") as stream:
                offered = json.load(stream)
        except (OSError, ValueError):
            offered = {}
        added = []
        for name, (key, shape) in UPGRADED_FILES.items():
            source, target = os.path.join(defaults_dir, name), os.path.join(target_dir, name)
            if not (os.path.isfile(source) and os.path.isfile(target)):
                continue
            defaults = _named((_read_yaml(source).get("world_definition") or {}).get(key), shape)
            data = _read_yaml(target)
            world = data.setdefault("world_definition", {}) if isinstance(data, dict) else None
            current = world.get(key) if isinstance(world, dict) else None
            if current is None and isinstance(world, dict):
                current = {} if shape == "mapping" else []
            if not isinstance(current, dict if shape == "mapping" else list):
                continue  # not a shape this can extend safely: leave the file as it is
            seen = set(offered.get(name, []))
            missing = [entry for entry in defaults if entry not in _named(current, shape) and entry not in seen]
            if missing:
                for entry in missing:
                    if shape == "mapping":
                        current[entry] = copy.deepcopy(defaults[entry])
                    else:
                        current.append(copy.deepcopy(defaults[entry]))
                world[key] = current
                _replace(target, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
                added += [(name, entry) for entry in missing]
            offered[name] = sorted(seen | set(defaults))
        _replace(offered_path, json.dumps(offered, indent=1, ensure_ascii=False))
    finally:
        lock.close()
    for name, entry in added:
        log(f"[config] Added the default entry `{entry}` to {name} (new in this release).")
    return added


if __name__ == "__main__":  # python -m utils.config_loader upgrade <defaults> <target> (docker-entrypoint.sh)
    import sys

    if len(sys.argv) == 4 and sys.argv[1] == "upgrade":
        upgrade_config_dir(sys.argv[2], sys.argv[3])
    else:
        sys.exit("usage: python -m utils.config_loader upgrade <defaults folder> <config folder>")
