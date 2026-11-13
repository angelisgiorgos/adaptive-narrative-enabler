"""A session's cast (``session_scope`` in config/hyperparameters.yaml): a session does not bring
every location, NPC, item and event of the world at once.

What the session's goal needs always stays: the start, the places where each stage of the goal
is advanced and the ways between them, the people, items and events the goal names (and the
homes, places and events they need), and the places the rules name (the arrest location,
prisons). Of the rest, each entry has a value for this session: how well it fits the goal's
places and people (tag similarity), how much there is to do with it (its actions) and the
session's own variety (a draw fixed per session, from the story's own randomness). The most
valuable ``keep_share`` of each kind stay; an entry below ``min_value`` is left out when its
kind has more than ``min_pool`` optional entries (an entry that adds little to this goal costs
the session its focus). A location is only left out when every kept place stays reachable.
Entries left out are switched off for the session (GameState.is_enabled), as an ``enabled_if``
would; if the goal could no longer be reached, the places are kept after all.
"""
import math

from utils.config_loader import config
from utils.tag_similarity import semantic_tag_similarity

from .world import Character, Event, Location, Object

KINDS = ("locations", "npcs", "items", "events")


class SessionScope:
    """Mixed into GameState: the session's cast."""

    session_scope = None   # class default: games saved before the cast existed keep everything

    @staticmethod
    def _scope_cfg():
        cfg = config.get("session_scope") or {}
        return cfg if cfg.get("enabled", False) else None

    def _left_out(self, entity):
        """Whether an entity is out of this session's cast."""
        scope = self.__dict__.get("session_scope")
        if not scope:
            return False
        if isinstance(entity, Location):
            return entity.name in scope["locations"]
        if isinstance(entity, Character):
            return entity.name in scope["npcs"]
        if isinstance(entity, Object):
            return entity.name in scope["items"]
        if isinstance(entity, Event):
            return entity.name in scope["events"]
        return False

    # ---- what the goal needs ---------------------------------------------------------------

    def _required_content(self):
        """{kind: names} the session cannot do without."""
        world = self.world
        required = {kind: set() for kind in KINDS}
        required["locations"].add(self.start_location)
        arrest = config.get("game_state.arrest_location")
        if arrest in world.locations:
            required["locations"].add(arrest)
        required["locations"] |= {name for name in (config.get("location_constraints") or {}) if name in world.locations}
        mission = self.mission
        if mission is None:
            return required
        stages = getattr(mission, "stages", None) or []
        goals = [dict(stage["goal"]) for stage in stages] or [dict(mission.goal or {})]
        goal_actions = set()
        for goal in goals:
            for key, value in goal.items():
                if key == "location":
                    required["locations"].add(str(value))
                elif key == "item":
                    required["items"].add(str(value))
                elif key in ("event", "event_succeeded"):
                    required["events"].add(str(value))
                elif key == "npc_removed":
                    required["npcs"].add(str(value))
                elif key == "action":
                    goal_actions.add(str(value).split(") ", 1)[-1])
        required["npcs"] |= self._mission_targets()
        # Where every stage of the goal can be advanced.
        saved = getattr(self, "mission_stage", 0)
        for index in range(max(1, len(stages))):
            self.mission_stage = index
            self.__dict__.pop("_goal_sites_cache", None)
            required["locations"] |= {site for site in self._goal_sites() if site in world.locations}
        self.mission_stage = saved
        self.__dict__.pop("_goal_sites_cache", None)
        # What the goal's actions need, and where its items are.
        for owner in list(world.locations.values()) + list(world.characters) + list(world.objects):
            for action in getattr(owner, "actions", None) or []:
                if action.name.split(") ", 1)[-1] not in goal_actions:
                    continue
                if action.required_object:
                    required["items"].add(action.required_object)
                if action.required_tag:
                    required["items"] |= {o.name for o in world.objects if action.required_tag in o.associated_tags}
                if action.triggers_event:
                    required["events"].add(action.triggers_event)
                if isinstance(owner, Character):
                    required["npcs"].add(owner.name)
                elif isinstance(owner, Object):
                    required["items"].add(owner.name)
        for item in required["items"]:
            required["locations"] |= {site for site in self._item_sites(item) if site in world.locations}
        for char in world.characters:
            if char.name not in required["npcs"]:
                continue
            home = self.npc_homes.get(char.name)
            if home:
                required["locations"].add(home)
            required["events"] |= {a.triggers_event for a in char.actions if a.triggers_event}
            if char.encounter_event:
                required["events"].add(char.encounter_event)
        # Events the engine itself starts (complications of failures, the manhunt) stay.
        manhunt = ((config.get("stakes") or {}).get("alert") or {}).get("manhunt_event")
        required["events"] |= {name for name, event in world.events.items()
                               if "complication" in event.tags or name == manhunt}
        return required

    # ---- what the rest is worth ------------------------------------------------------------

    @staticmethod
    def _tags_of(entity):
        return {str(t).lower() for t in (getattr(entity, "tags", None) or getattr(entity, "associated_tags", None) or ())}

    def _content_value(self, kind, entity, context, cfg):
        """An optional entry's value for this session: fit with the goal's places and people,
        things to do with it, and the session's variety."""
        tags = self._tags_of(entity)
        relevance = semantic_tag_similarity(tags, context) if tags and context else 0.0
        richness = min(1.0, len(getattr(entity, "actions", None) or []) / 3.0)
        if kind == "npcs" and getattr(entity, "encounter_event", None):
            richness = max(richness, 0.67)  # a hostile NPC brings danger of its own
        variety = self._story_draw("scope", kind, entity.name)
        return (float(cfg.get("relevance_weight", 1.0)) * relevance + float(cfg.get("richness_weight", 0.5)) * richness
                + float(cfg.get("variety_weight", 0.6)) * variety)

    def _reachable(self, kept):
        """Whether every kept place can be reached from the start over the map's kept places."""
        start = self.start_location
        seen, frontier = {start}, [start]
        while frontier:
            here = frontier.pop()
            for step in self.neighbours(here) or ():
                if step in kept and step not in seen:
                    seen.add(step)
                    frontier.append(step)
        return kept <= seen

    def _choose_scope(self):
        """{kind: names left out} for this session (all empty when the cast is off)."""
        cfg = self._scope_cfg()
        empty = {kind: set() for kind in KINDS}
        if cfg is None:
            return empty
        required = self._required_content()
        world = self.world
        context = set()
        for name in required["locations"]:
            context |= self._tags_of(world.locations[name])
        for char in world.characters:
            if char.name in required["npcs"]:
                context |= self._tags_of(char)
        pools = {
            "npcs": [c for c in world.characters if c.name not in required["npcs"]],
            "items": [o for o in world.objects if o.name not in required["items"]],
            # Only events that start by themselves; the others come with who or what starts them.
            "events": [e for name, e in world.events.items() if name not in required["events"] and e.trigger == "random"],
            "locations": [loc for name, loc in world.locations.items() if name not in required["locations"]],
        }
        shares = cfg.get("keep_share") or {}
        min_value = float(cfg.get("min_value", 0.0))
        min_pool = int(cfg.get("min_pool", 4))
        left_out = {kind: set() for kind in KINDS}
        for kind, pool in pools.items():
            if not pool:
                continue
            values = {e.name: self._content_value(kind, e, context, cfg) for e in pool}
            ranked = sorted(pool, key=lambda e: (-values[e.name], e.name))
            keep = math.ceil(float(shares.get(kind, 1.0)) * len(ranked))
            out = [e.name for e in ranked[keep:]]
            if len(pool) > min_pool:
                out += [e.name for e in ranked[:keep] if values[e.name] < min_value]
            if kind == "locations":
                if self.neighbours(self.start_location) is None:
                    continue  # no map to keep connected: every place stays
                kept = set(world.locations)
                dropped = set()
                for name in sorted(set(out), key=lambda n: (values[n], n)):
                    if self._reachable(kept - dropped - {name}):
                        dropped.add(name)
                left_out[kind] = dropped
            else:
                left_out[kind] = set(out)
        return left_out

    def _apply_scope(self):
        """Choose the session's cast and settle the world around it: unique NPCs and items
        whose place was left out move to the best place still in the session. If the goal
        could no longer be reached, every place is kept; if still not, the whole world is."""
        reachable_before = self.mission is not None and self._goal_still_reachable()
        self.session_scope = self._choose_scope()
        if not any(self.session_scope.values()):
            self.session_scope = None
            return
        self._settle_scope()
        if reachable_before and not self._goal_still_reachable():
            self.session_scope["locations"] = set()
            self._settle_scope()
            if not self._goal_still_reachable():
                self.session_scope = None
                self._settle_scope()

    def _goal_still_reachable(self):
        for cache in ("_viability_cache", "_item_sites_cache", "_site_index", "_goal_sites_cache", "_route_cache"):
            self.__dict__.pop(cache, None)
        return self._mission_viable_from(self.mission, self.start_location)

    def _settle_scope(self):
        self.npc_homes = self._assign_npc_homes()
        dropped = (self.session_scope or {}).get("locations") or set()
        for name, instance in self.item_instances.items():
            if instance.location not in dropped:
                continue
            obj = next((o for o in self.world.objects if o.name == name), None)
            fitting = [loc for loc in self.world.locations.values() if loc.name not in dropped
                       and obj is not None and obj.associated_tags & loc.tags]
            place = max(fitting, key=lambda loc: (len(obj.associated_tags & loc.tags), loc.name)).name if fitting \
                else self.start_location
            instance.location = place
            for entry in self.item_log:
                if entry["item"] == name and entry["what"] == "placed":
                    entry["location"] = place
        for cache in ("_viability_cache", "_item_sites_cache", "_site_index", "_goal_sites_cache", "_route_cache"):
            self.__dict__.pop(cache, None)

    def scope(self):
        """The session's cast for the player and the session report: {left_out: {kind: names},
        kept: {kind: count}}."""
        scope = self.__dict__.get("session_scope") or {kind: set() for kind in KINDS}
        totals = {"locations": len(self.world.locations), "npcs": len(self.world.characters),
                  "items": len(self.world.objects), "events": len(self.world.events)}
        return {"left_out": {kind: sorted(scope.get(kind) or ()) for kind in KINDS},
                "kept": {kind: totals[kind] - len(scope.get(kind) or ()) for kind in KINDS}}
