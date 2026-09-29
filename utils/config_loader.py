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
