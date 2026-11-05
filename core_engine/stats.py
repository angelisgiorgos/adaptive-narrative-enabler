"""Character stats defined by the writer (config/stats.yaml).

One story can use attack / defence / agility, another stealth / persuasion /
watchfulness / intimidation: the names are free. A player receives a budget
of points (default 50) to spread over the stats, each between ``min_points``
(5) and ``max_points`` (20). Points convert to skill values on the 1-100
check scale (``value_per_point``, default 5: 5..20 points -> 25..100).

During play, held items add bonuses (``stat_bonus``) and outcomes change
stats permanently for the rest of the playthrough (``change_stats``).
"""
from utils.config_loader import config

MODES = ("defined", "random", "balanced")
VALUE_MIN, VALUE_MAX = 1, 100


def stats_config(cfg=None):
    cfg = config if cfg is None else cfg
    return dict(cfg.get("world_definition.character_stats") or {})


def _budget(settings):
    allocation = settings.get("allocation") or {}
    return {
        "total_points": int(allocation.get("total_points", 50)),
        "min_points": int(allocation.get("min_points", 5)),
        "max_points": int(allocation.get("max_points", 20)),
        "value_per_point": float(allocation.get("value_per_point", 5)),
    }


def validate_settings(settings):
    """Problems with a ``character_stats`` definition (empty when valid)."""
    if not isinstance(settings, dict):
        return ["`character_stats` must be a mapping."]
    errors = []
    stats = settings.get("stats") or {}
    if not isinstance(stats, dict):
        return ["`character_stats.stats` must be a mapping of stat names to descriptions."]
    for name in stats:
        if not str(name).replace("_", "").isalnum():
            errors.append(f"Stat name `{name}` may only use letters, digits and `_`.")
    try:
        budget = _budget(settings)
    except (TypeError, ValueError):
        return ["`character_stats.allocation` values must be numbers."]
    if budget["min_points"] < 0 or budget["max_points"] < budget["min_points"]:
        errors.append("`allocation.max_points` must be at least `min_points` (and both 0 or more).")
    if budget["value_per_point"] <= 0:
        errors.append("`allocation.value_per_point` must be above 0.")
    if stats and not budget["min_points"] * len(stats) <= budget["total_points"] <= budget["max_points"] * len(stats):
        errors.append(
            f"{len(stats)} stats need a total between {budget['min_points'] * len(stats)} and "
            f"{budget['max_points'] * len(stats)} points; `total_points` is {budget['total_points']}."
        )
    if settings.get("mode", "defined") not in MODES:
        errors.append(f"`character_stats.mode` must be one of: {', '.join(MODES)}.")
    for field in ("tag_checks",):
        mapping = settings.get(field) or {}
        if not isinstance(mapping, dict):
            errors.append(f"`character_stats.{field}` must map outcome tags to stats.")
            continue
        for tag, stat in mapping.items():
            if stat not in stats:
                errors.append(f"`{field}.{tag}` uses undefined stat `{stat}`.")
    reduction = settings.get("damage_reduction_stat")
    if reduction and reduction not in stats:
        errors.append(f"`damage_reduction_stat` uses undefined stat `{reduction}`.")
    defined = settings.get("defined_points")
    if defined:
        errors += CharacterStats.validate_points(defined, list(stats), budget)
    return errors


class CharacterStats:
    """The stats of one player for one playthrough."""

    def __init__(self, points=None, mode=None, rng=None, settings=None):
        """``points`` (stat -> points) defines the stats; otherwise ``mode``
        (default ``character_stats.mode``) picks: defined (``defined_points``),
        random (a random split using ``rng``), or balanced (an even split)."""
        self.settings = stats_config() if settings is None else settings
        self.names = list(self.settings.get("stats") or {})
        self.budget = _budget(self.settings)
        if points is not None:
            self.mode = "defined"
        else:
            self.mode = mode or self.settings.get("mode", "defined")
            if self.mode == "defined":
                points = self.settings.get("defined_points") or None
                if points is None:
                    self.mode = "balanced"
            if self.mode == "random":
                points = self.random_points(self.names, self.budget, rng)
            elif self.mode == "balanced":
                points = self.balanced_points(self.names, self.budget)
        errors = self.validate_points(points, self.names, self.budget)
        if errors:
            raise ValueError("; ".join(errors))
        self.points = {name: int(points[name]) for name in self.names}
        self.changes = {name: 0 for name in self.names}  # permanent changes from outcomes

    # ---- allocation --------------------------------------------------------

    @staticmethod
    def validate_points(points, names, budget):
        if not isinstance(points, dict):
            return ["Stat points must be a mapping of stat names to points."]
        if not names and not points:
            return []  # a story without stats has no checks
        errors = []
        missing = [name for name in names if name not in points]
        unknown = [name for name in points if name not in names]
        if missing:
            errors.append(f"Points are missing for: {', '.join(missing)}.")
        if unknown:
            errors.append(f"Unknown stat(s): {', '.join(unknown)}.")
        for name, value in points.items():
            if not isinstance(value, int) or isinstance(value, bool):
                errors.append(f"`{name}` needs a whole number of points.")
            elif not budget["min_points"] <= value <= budget["max_points"]:
                errors.append(f"`{name}` must have {budget['min_points']}-{budget['max_points']} points, not {value}.")
        if not errors and sum(points.values()) != budget["total_points"]:
            errors.append(f"The points add up to {sum(points.values())}; spend exactly {budget['total_points']}.")
        return errors

    @staticmethod
    def balanced_points(names, budget):
        if not names:
            return {}
        share, extra = divmod(budget["total_points"], len(names))
        return {name: share + (1 if index < extra else 0) for index, name in enumerate(names)}

    @staticmethod
    def random_points(names, budget, rng):
        """A uniformly random split that respects the minimum, maximum and total."""
        points = {name: budget["min_points"] for name in names}
        remaining = budget["total_points"] - budget["min_points"] * len(names)
        while remaining > 0:
            open_stats = [name for name in names if points[name] < budget["max_points"]]
            if not open_stats:
                break
            points[rng.choice(open_stats)] += 1
            remaining -= 1
        return points

    # ---- values --------------------------------------------------------------

    def base_value(self, name):
        value = self.points[name] * self.budget["value_per_point"] + self.changes[name]
        return int(min(VALUE_MAX, max(VALUE_MIN, round(value))))

    def value(self, name, bonus=0):
        """Skill value on the 1-100 check scale, including item bonuses."""
        return int(min(VALUE_MAX, max(VALUE_MIN, self.base_value(name) + bonus)))

    def change(self, name, delta):
        """Permanently raise or lower a stat; return (old, new) base values."""
        old = self.base_value(name)
        self.changes[name] += int(delta)
        new = self.base_value(name)
        self.changes[name] = new - self.points[name] * self.budget["value_per_point"]  # stay within 1..100
        return old, new

    def values(self, bonuses=None):
        bonuses = bonuses or {}
        return {name: self.value(name, bonuses.get(name, 0)) for name in self.names}

    def average(self, bonuses=None):
        values = self.values(bonuses)
        return sum(values.values()) / len(values) if values else 0.0

    def summary(self, bonuses=None):
        return {"mode": self.mode, "points": dict(self.points), "values": self.values(bonuses)}
