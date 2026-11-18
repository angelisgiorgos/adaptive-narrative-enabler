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

UPGRADED_FILES = {  # file -> (world_definition key, shape): its entries, added and refreshed one by one
    "locations.yaml": ("locations", "mapping"),
    "npcs.yaml": ("characters", "list"),
    "items.yaml": ("objects", "list"),
    "events.yaml": ("events", "mapping"),
    "missions.yaml": ("missions", "mapping"),
    "variables.yaml": ("variables", "mapping"),
}
OFFERED_FILE = ".ane-default-entries.json"
HISTORY_FILE = "default_history.json"   # every value the defaults have had (packaging/default_history.py)
BACKUP_DIR = ".ane-backups"


def _named(entries, shape):
    if shape == "mapping":
        return dict(entries) if isinstance(entries, dict) else {}
    return {e["name"]: e for e in entries or [] if isinstance(e, dict) and e.get("name")}


def digest(value):
    """A short digest of a configuration value (the same value: the same digest)."""
    import hashlib
    import json

    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _collection_path(name):
    collection = UPGRADED_FILES.get(name)
    return (f"world_definition.{collection[0]}", collection[1]) if collection else (None, None)


def document_digests(name, document):
    """(key, digest) of every entry (``world_definition.locations[Harbor]``) and every setting
    path (``evolution.max_steps``, a section as a whole too) of a config file's content."""
    entries_at, shape = _collection_path(name)

    def walk(node, path):
        if path:
            yield path, digest(node)
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            at = f"{path}.{key}" if path else str(key)
            if at == entries_at:
                for entry, content in _named(value, shape).items():
                    yield f"{at}[{entry}]", digest(content)
                continue
            yield from walk(value, at)

    yield from walk(document if isinstance(document, dict) else {}, "")


def _taken(take, key):
    """Whether ``key`` (a setting path, or an entry ``...[name]``) is one asked to take the
    default even though it was edited: ``all``, its name or its path."""
    if not take:
        return False
    entry = key.rsplit("[", 1)[1][:-1] if key.endswith("]") else None
    return "all" in take or key in take or (entry is not None and entry in take)


def _refresh(name, current, default, history, offered, changes, path="", take=()):
    """Bring ``current`` (a file's content) toward ``default``: what is missing is added (an entry
    only once: one deleted afterwards stays deleted), and what still has a value the defaults once
    had (``history``: never edited) takes the current default. Anything else was edited: kept,
    unless ``take`` names it (or is ``all``)."""
    entries_at, shape = _collection_path(name)
    for key, value in default.items():
        at = f"{path}.{key}" if path else str(key)
        if at == entries_at:
            if current.get(key) is None:
                current[key] = {} if shape == "mapping" else []
            if isinstance(current[key], dict if shape == "mapping" else list):
                _refresh_entries(current[key], _named(value, shape), shape, at, history, offered, changes, take)
            continue
        if key not in current:
            current[key] = copy.deepcopy(value)
            changes.append(("added", at))
            continue
        mine = current[key]
        if mine == value:
            continue
        sections = isinstance(mine, dict) and isinstance(value, dict)
        # `all` takes entries and settings one by one, never a whole section: it may hold additions.
        asked = _taken(take, at) and (not sections or at in take)
        if digest(mine) in history.get(at, ()) or asked:
            current[key] = copy.deepcopy(value)
            changes.append(("updated", at))
        elif sections:
            _refresh(name, mine, value, history, offered, changes, at, take)
        else:
            changes.append(("kept", at))


def _refresh_entries(current, defaults, shape, at, history, offered, changes, take=()):
    mine = _named(current, shape)
    for entry, value in defaults.items():
        key = f"{at}[{entry}]"
        if entry not in mine:
            if entry not in offered:
                if shape == "mapping":
                    current[entry] = copy.deepcopy(value)
                else:
                    current.append(copy.deepcopy(value))
                changes.append(("added", key))
        elif mine[entry] != value:
            if digest(mine[entry]) in history.get(key, ()) or _taken(take, key):
                if shape == "mapping":
                    current[entry] = copy.deepcopy(value)
                else:
                    current[current.index(mine[entry])] = copy.deepcopy(value)
                changes.append(("updated", key))
            else:
                changes.append(("kept", key))


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


def _world_problem(target_dir, staged):
    """Why the world the upgraded folder makes cannot be built (None: it can)."""
    import random
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory(prefix="ane-upgrade-") as folder:
        for name in os.listdir(target_dir):
            if name.endswith(".yaml"):
                shutil.copy(os.path.join(target_dir, name), os.path.join(folder, name))
        for name, (data, _) in staged.items():
            with open(os.path.join(folder, name), "w", encoding="utf-8") as stream:
                yaml.safe_dump(data, stream, sort_keys=False, allow_unicode=True)
        merged = Config(folder).as_dict()
    try:
        from core_engine import Genome
        from core_engine.builder import build_world
    except Exception as exc:  # the engine cannot be loaded here: nothing to check against
        return None if isinstance(exc, ImportError) else str(exc)
    config.use_override(merged)
    try:
        build_world(Genome(rng=random.Random(0)))
    except Exception as exc:
        return str(exc)
    finally:
        config.clear_override()
    return None


def upgrade_config_dir(defaults_dir, target_dir, log=print, take_defaults=()):
    """Bring a config folder kept from an older release (a Docker volume) up to date without losing
    what anyone wrote in it. For every file of the defaults:

    - what the folder lacks is added: a setting, or a default entry (place, NPC, item, event,
      mission, variable) once (an entry deleted afterwards stays deleted: the names offered are
      kept in .ane-default-entries.json). A release's new entries refer to each other (a new
      mission names its new NPC), and files copied whole from different releases otherwise make a
      world that cannot be played;
    - an entry or a setting whose value is one the defaults once had (default_history.json: never
      edited) takes the current default, so a volume does not keep an old release's world and
      settings forever; anything edited is kept as it is.

    ``take_defaults``: entries (by name) or setting paths that take the current default even though
    they were edited, or ``all`` (entries and settings this folder added itself are never touched).
    The files changed are backed up first (.ane-backups/<time>/). If the world the folder would make
    cannot be built, only the additions are made. Returns the (file, entry) pairs added."""
    import datetime
    import json
    import shutil

    take = {str(item).strip() for item in take_defaults if str(item).strip()}
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
        try:
            with open(os.path.join(defaults_dir, HISTORY_FILE), "r", encoding="utf-8") as stream:
                history = json.load(stream).get("files") or {}
        except (OSError, ValueError):
            history = {}
        # A request to take the defaults applies once per release, so a setting left in place
        # (ANE_CONFIG_TAKE_DEFAULTS) does not undo the studio's edits on every start.
        release = digest({name: _read_yaml(os.path.join(defaults_dir, name))
                          for name in sorted(os.listdir(defaults_dir)) if name.endswith(".yaml")})
        applied = set((offered.get("_taken") or {}).get(release, []))
        take -= applied

        def stage(with_history):
            staged = {}
            for name in sorted(os.listdir(defaults_dir)):
                source, target = os.path.join(defaults_dir, name), os.path.join(target_dir, name)
                if not (name.endswith(".yaml") and os.path.isfile(source) and os.path.isfile(target)):
                    continue  # docker-entrypoint.sh copies a missing file whole
                default, data = _read_yaml(source), _read_yaml(target)
                if not isinstance(default, dict) or not isinstance(data, dict):
                    continue  # not a shape this can extend safely: leave the file as it is
                changes = []
                _refresh(name, data, default, history.get(name, {}) if with_history else {},
                         set(offered.get(name, [])), changes, take=take if with_history else ())
                if any(kind != "kept" for kind, _ in changes):
                    staged[name] = (data, changes)
            return staged

        staged = stage(True)
        if any(kind == "updated" for _, changes in staged.values() for kind, _ in changes):
            problem = _world_problem(target_dir, staged)
            if problem:
                log(f"[config] Kept the old values: with the new defaults the world could not be built ({problem[:300]}).")
                staged = stage(False)
        if staged:
            backup = os.path.join(target_dir, BACKUP_DIR, datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
            os.makedirs(backup, exist_ok=True)
        for name, (data, _) in staged.items():
            target = os.path.join(target_dir, name)
            shutil.copy2(target, os.path.join(backup, name))
            source = os.path.join(defaults_dir, name)
            same = data == _read_yaml(source)  # all default again: the default file, with its comments
            with open(source, "r", encoding="utf-8") as stream:
                text = stream.read() if same else yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
            _replace(target, text)
        for name, (key, shape) in UPGRADED_FILES.items():
            source = os.path.join(defaults_dir, name)
            if os.path.isfile(source):
                defaults = _named((_read_yaml(source).get("world_definition") or {}).get(key), shape)
                offered[name] = sorted(set(offered.get(name, [])) | set(defaults))
        if take:
            offered["_taken"] = {release: sorted(applied | take)}
        _replace(offered_path, json.dumps(offered, indent=1, ensure_ascii=False))
    finally:
        lock.close()
    added = []
    for name, (_, changes) in staged.items():
        counts = {kind: [at for done, at in changes if done == kind] for kind in ("added", "updated", "kept")}
        entries = [at.split("[", 1)[1][:-1] for at in counts["added"] if at.endswith("]")]
        added += [(name, entry) for entry in entries]
        for kind, verb in (("added", "Added"), ("updated", "Updated to this release's default"),
                           ("kept", "Kept as edited (ANE_CONFIG_TAKE_DEFAULTS=<names>|all takes the default)")):
            if counts[kind]:
                shown = ", ".join(at.rsplit("[", 1)[-1].rstrip("]") if at.endswith("]") else at for at in counts[kind][:12])
                more = f" and {len(counts[kind]) - 12} more" if len(counts[kind]) > 12 else ""
                log(f"[config] {name}: {verb}: {shown}{more}.")
    if staged:
        log(f"[config] The files changed were backed up in {BACKUP_DIR}/.")
    return added


if __name__ == "__main__":  # python -m utils.config_loader upgrade <defaults> <target> [--take-defaults a,b|all]
    import sys

    arguments = sys.argv[1:]
    take = []
    if "--take-defaults" in arguments:
        at = arguments.index("--take-defaults")
        take = (arguments[at + 1] if at + 1 < len(arguments) else "").split(",")
        arguments = arguments[:at] + arguments[at + 2:]
    if len(arguments) == 3 and arguments[0] == "upgrade":
        upgrade_config_dir(arguments[1], arguments[2], take_defaults=take)
    else:
        sys.exit("usage: python -m utils.config_loader upgrade <defaults folder> <config folder> "
                 "[--take-defaults <name>,<setting.path>,...|all]")
