import copy
import heapq
from collections import Counter
import random
import re
from utils.config_loader import config
from .actions import Action, Outcome
from .health import HealthPlayer
from .narration import Narrator
from utils.tag_similarity import semantic_tag_similarity

_ACTION_PREFIX = re.compile(r"^\s*(\([^)]*\)|\[[^\]]*\])\s*")


def _action_keys(name):
    """Match "(Priest) Seek a blessing" by its full name or as "Seek a blessing"."""
    full = str(name).strip().lower()
    return {full, _ACTION_PREFIX.sub("", full)}

# ============================
# GAME STATE
# ============================

class GameState:
    def __init__(self, world, genome, start="Harbor", verbose=False, interactive=False, rng=None, mission=None):
        self.world = world
        self.genome = genome
        self.verbose = verbose
        self.interactive = interactive
        self.rng = rng or random
        if not world.locations:
            raise ValueError("The world must contain at least one location.")
        start = start if start in world.locations else next(iter(world.locations))
        self.current = world.locations[start]

        # Health and power, bounded by max_health and power_model.max_power.
        self.health_player = HealthPlayer()
        self.actions_taken = 0

        self.tags_seen = []
        self.history = []
        self.narrate = Narrator()  # customisable story text (config/messages.yaml)
        
        # Track location visits
        self.location_counts = {start: 1}
        self.path = [start] # Sequence of locations
        self.unlocked_locations = set()
        self.confined_in = None  # location the player was sent to and must escape
        self.last_location_name = None # For tracking transitions
        self.current_inhabitants = [] # NPCs currently in the scene
        self.consecutive_steps_in_location = 0 # Track how long we've stayed

        self.setup_indices = {}
        self.payoff_indices = {}
        
        self.character_interactions = {} # Track speaker visits
        
        # Track which actions were performed during the current visit to a location
        self.visit_actions_performed = set()
        
        self.inventory = set()
        self.coins = config.get("game_state.initial_coins", 5)

        # Narrative Discovery State
        self.visible_inhabitants = set()
        self.visible_objects = set()
        self.ended = False
        self.end_reason = None
        self.end_kind = None  # death, mission, action_limit, stalled, dungeon

        # Repeatable vs one-time options (Action.repeat / exclusive_group).
        self.spent_actions = set()   # "once" actions already taken
        self.closed_groups = set()   # exclusive groups whose choice has been made

        # What the story has already shown, for genome.novelty_bias and fitness.
        self.choice_counts = Counter()   # _novelty_key(action) -> times chosen
        self.outcome_counts = Counter()  # outcome text -> times shown
        self.unexpected_events = []
        self._last_event_step = -1000

        # Authored Events: while active, the player stays anchored to
        # event_return_location and only the event's actions are available.
        self.active_event = None
        self.event_return_location = None
        self.event_source_npc = None
        self.event_description = None
        self.event_log = []
        self.removed_npcs = set()      # every NPC ever removed (mission goals)
        self._scene_removals = set()   # (npc, location, visit) for generic NPCs
        self.encounters = set()
        self._announced_visit = None   # visit whose arrival text is in the history
        self._printed_visit = None     # visit whose scene step() last printed
        self.npc_homes = self._assign_npc_homes()
        self.update_power()

        # Mission: the win condition, chosen at random unless one is requested.
        self.mission = None
        self.won = False
        self.succeeded_actions = set()
        self.succeeded_outcome_tags = set()
        self._start_mission(mission)

    @property
    def health(self):
        return self.health_player.health

    @health.setter
    def health(self, value):
        # Direct assignment is still clamped to 0 .. max_health.
        self.health_player.set(value)

    @property
    def max_health(self):
        return self.health_player.max_health

    @property
    def power(self):
        return self.health_player.power

    @property
    def scene_name(self):
        if self.active_event:
            return f"{self.active_event.title} (at {self.current.name})"
        return self.current.name

    def _pick_text(self, options, offset=0):
        if not options:
            return None
        index = (self.actions_taken + self.location_counts.get(self.current.name, 0) + offset) % len(options)
        return options[index]

    def _describe_visible_scene(self):
        """Builds a readable scene description for the current interactive turn."""
        if self.active_event:
            npc_lines = []
            source = self._event_source_character()
            if source:
                source_desc = self._pick_text(source.descriptions, offset=len(source.name)) or f"{source.name} is here."
                npc_lines.append((source.name, source_desc))
            return self.event_description, npc_lines, []

        location_desc = self.current.name
        if self.current.entered_descriptions:
            location_desc = self._pick_text(self.current.entered_descriptions)
        elif self.current.distant_descriptions:
            location_desc = self._pick_text(self.current.distant_descriptions)

        npc_lines = []
        for char in self.current_inhabitants:
            if char.name not in self.visible_inhabitants:
                continue
            char_desc = self._pick_text(char.descriptions, offset=len(char.name)) if char.descriptions else f"{char.name} is here."
            npc_lines.append((char.name, char_desc))

        object_lines = []
        for obj in self.location_objects:
            if obj.name in self.inventory or obj.name not in self.visible_objects:
                continue
            obj_desc = self._pick_text(obj.descriptions, offset=len(obj.name)) if obj.descriptions else f"You notice a {obj.name}."
            object_lines.append((obj.name, obj_desc))

        return location_desc, npc_lines, object_lines

    def _is_guard_failure(self, action, outcome):
        action_name = (action.name if action else "").lower()
        character_name = (action.character_name if action and action.character_name else "").lower()
        current_tags = {tag.lower() for tag in self.current.tags}
        outcome_tags = {tag.lower() for tag in outcome.tags}

        return any([
            "guard" in action_name,
            "guard" in character_name,
            "guards" in outcome_tags,
            "guarded" in current_tags,
            ("combat" in outcome_tags and ("guard" in action_name or "guard" in character_name)),
        ])

    @property
    def mission_status(self):
        if not self.mission:
            return "No mission"
        if self.won:
            return f"{self.mission.title}: completed"
        return f"{self.mission.title}: {'failed' if self.ended else 'in progress'}"

    def _start_mission(self, requested=None):
        if not config.get("mission_settings.enabled", True) or not self.world.missions:
            return
        if requested is not None:
            if requested not in self.world.missions:
                raise ValueError(f"Unknown mission '{requested}'.")
            self.mission = self.world.missions[requested]
        else:
            # A goal that already holds at the start would win without playing.
            candidates = [m for m in self.world.missions.values() if not self._goal_met(m)]
            if not candidates:
                return
            self.mission = self.rng.choices(candidates, weights=[max(0.0, m.weight) for m in candidates], k=1)[0]
        if self.mission.description:
            message = self.narrate("mission_start", title=self.mission.title, description=self.mission.description)
        else:
            message = self.narrate("mission_start_brief", title=self.mission.title)
        self.history.append(message)
        if self.interactive or self.verbose:
            print(f"  >>> {message}")

    def _goal_condition_met(self, key, value):
        if key == "action":
            return bool(_action_keys(value) & self.succeeded_actions)
        if key == "location":
            return value in self.location_counts
        if key == "item":
            return value in self.inventory
        if key == "event":
            return any(e["event"] == value and "action" in e for e in self.event_log)
        if key == "npc_removed":
            return value in self.removed_npcs
        if key == "outcome_tag":
            return str(value).lower() in self.succeeded_outcome_tags
        if key == "coins":
            return self.coins >= value
        return False

    def _goal_met(self, mission):
        return all(self._goal_condition_met(key, value) for key, value in mission.goal.items())

    def _pending_goal(self):
        if not self.mission or self.won:
            return {}
        return {k: v for k, v in self.mission.goal.items() if not self._goal_condition_met(k, v)}

    @staticmethod
    def _advances(action, key, value, npc=None, outcomes=None):
        """Whether taking ``action`` directly works toward one goal condition.

        ``npc`` is the NPC the action acts for (event actions act for the NPC
        who triggered the event), which resolves ``remove_npc: true``.
        ``outcomes`` narrows the check to specific outcomes of the action.
        """
        npc = action.character_name or npc
        outcomes = action.outcomes if outcomes is None else outcomes
        if key == "action":
            return bool(_action_keys(value) & _action_keys(action.name))
        if key == "location":
            return any(value in (o.move_to, o.reveal_location, o.send_to) for o in outcomes)
        if key == "item":
            return action.collects_object == value or any(o.reveal_object == value for o in outcomes)
        if key == "event":
            return action.triggers_event == value
        if key == "npc_removed":
            return any(o.remove_npc == value or (o.remove_npc is True and npc == value) for o in outcomes)
        if key == "outcome_tag":
            return any(str(value).lower() in {t.lower() for t in o.tags} for o in outcomes)
        if key == "coins":
            return any(o.coin_change > 0 for o in outcomes)
        return False

    def _npc_sites(self, npc_name):
        char = next((c for c in self.world.characters if c.name == npc_name), None)
        if char is None or (char.unique and char.name in self.removed_npcs):
            return set()
        if char.unique:
            return {self.npc_homes[char.name]} if char.name in self.npc_homes else set()
        return {name for name, loc in self.world.locations.items() if self._npc_fits_location(char, loc)}

    def _item_sites(self, item_name):
        """Locations where the item is on show when the player arrives (the
        same top-N visibility ranking a scene uses)."""
        cache = self.__dict__.setdefault("_item_sites_cache", {})
        if item_name in cache:
            return cache[item_name]
        limit = config.get("scene.max_visible_objects", 2)
        sites = set()
        for name, loc in self.world.locations.items():
            ranked = sorted(
                (obj for obj in self.world.objects if obj.associated_tags & loc.tags),
                key=lambda obj: (self._score_object_visibility(obj, loc), obj.name),
                reverse=True,
            )
            if item_name in {obj.name for obj in ranked[:limit]}:
                sites.add(name)
        cache[item_name] = sites
        return sites

    def _can_discover_from(self, location_name):
        """Whether a location's own actions can open new paths (spawn/reveal)."""
        loc = self.world.locations[location_name]
        return any(o.spawn or o.reveal_location for a in loc.actions for o in a.outcomes)

    def _action_site_index(self):
        """Every authored action as (action, sites, npc, gate), built once per game.

        ``gate`` is what must still be unused for the action to be offered: the
        action itself, the trigger action of its event, or the NPC's encounter.
        """
        if getattr(self, "_site_index", None) is None:
            index = []
            for name, loc in self.world.locations.items():
                index.extend((a, {name}, None, ("action", a.name)) for a in loc.actions)
            for char in self.world.characters:
                index.extend((a, ("npc", char.name), char.name, ("action", a.name)) for a in char.actions)
            for obj in self.world.objects:
                index.extend((a, ("item", obj.name), None, ("action", a.name)) for a in obj.actions)
            triggers = [(a.triggers_event, sites, npc, gate) for a, sites, npc, gate in index if a.triggers_event]
            triggers += [
                (char.encounter_event, ("npc", char.name), char.name, ("encounter", char))
                for char in self.world.characters if char.encounter_event
            ]
            for event_name, sites, npc, gate in triggers:
                event = self.world.events.get(event_name)
                if event:
                    index.extend((ev_a, sites, npc, gate) for ev_a in event.actions)
            self._site_index = index
        return self._site_index

    def _entry_available_here(self, action, sites, gate):
        """Whether an indexed action can still be taken during this visit."""
        if self._is_closed(action):
            return False
        if isinstance(sites, tuple):
            kind, name = sites
            if kind == "npc" and (name not in self.visible_inhabitants or self._is_removed(name)):
                return False
            if kind == "item" and (name not in self.visible_objects or name in self.inventory):
                return False
        elif self.current.name not in sites:
            return False
        kind, value = gate
        if kind == "encounter":
            return self._encounter_key(value) not in self.encounters
        if value == action.name:
            return self._is_offered(action)
        return value not in self.visit_actions_performed  # the trigger of its event

    def _goal_actionable_here(self):
        """Whether an action that advances the goal is still on offer this visit."""
        pending = {k: v for k, v in self._pending_goal().items() if k not in ("location", "coins")}
        return any(
            self._advances(action, key, value, npc) and self._entry_available_here(action, sites, gate)
            for action, sites, npc, gate in self._action_site_index()
            for key, value in pending.items()
        )

    def _resolve_sites(self, sites):
        if isinstance(sites, tuple):
            kind, name = sites
            return self._npc_sites(name) if kind == "npc" else self._item_sites(name)
        return sites

    def _goal_sites(self):
        """Locations where some unmet goal condition can be advanced."""
        pending = self._pending_goal()
        key = (
            tuple(sorted(pending.items(), key=str)), frozenset(self.removed_npcs),
            len(self.spent_actions), len(self.closed_groups),
        )
        cached = getattr(self, "_goal_sites_cache", None)
        if cached and cached[0] == key:
            return cached[1]
        sites = set()
        for key, value in pending.items():
            if key == "coins":
                continue  # coins can be earned almost anywhere; no destination to plan for
            if key == "location":
                sites.add(value)
            elif key == "item":
                sites |= self._item_sites(value)
            elif key == "event":
                for char in self.world.characters:
                    if char.encounter_event == value:
                        sites |= self._npc_sites(char.name)
            for action, action_sites, npc, _ in self._action_site_index():
                # A one-time option already used (for unique owners) is no longer a way to the goal.
                if action.source != "npc" and self._is_closed(action):
                    continue
                if key != "location" and self._advances(action, key, value, npc):
                    sites |= self._resolve_sites(action_sites)
        sites = {name for name in sites if name in self.world.locations}
        self._goal_sites_cache = (key, sites)
        return sites

    def _goal_distances(self):
        """Steps from each location to the nearest goal site (backward Dijkstra).

        Known routes cost 1; a route that must first be discovered through the
        world's spawn rules costs ``mission_settings.discovery_route_cost``.
        Locked locations (e.g. the Dungeon) are dead ends unless they are a site.
        """
        sites = frozenset(self._goal_sites())
        if not sites:
            return {}
        edge_count = sum(len(loc.connected) for loc in self.world.locations.values())
        # A site whose goal actions are used up for this visit must be left and
        # re-entered (actions come back on the next visit).
        exhausted = self.current.name in sites and not self._goal_actionable_here()
        key = (sites, edge_count, frozenset(self.unlocked_locations), self.confined_in, self.current.name, exhausted)
        cached = getattr(self, "_route_cache", None)
        if cached and cached[0] == key:
            return cached[1]

        discovery_cost = float(config.get("mission_settings.discovery_route_cost", 2))
        predecessors = {name: [] for name in self.world.locations}
        for name, loc in self.world.locations.items():
            if self._location_is_locked(name):
                continue
            for neighbour in loc.connected:
                if neighbour in predecessors:
                    predecessors[neighbour].append((name, 1.0))
            if not self._can_discover_from(name):
                continue
            for child in self.world.spawn_rules.get(name, []):
                if child in predecessors and child not in loc.connected and child != "Dungeon":
                    predecessors[child].append((name, discovery_cost))

        distances = {name: 0.0 for name in sites}
        queue = [(0.0, name) for name in sites]
        heapq.heapify(queue)
        while queue:
            dist, name = heapq.heappop(queue)
            if dist > distances.get(name, float("inf")):
                continue
            for previous, cost in predecessors.get(name, []):
                candidate = dist + cost
                if candidate < distances.get(previous, float("inf")):
                    distances[previous] = candidate
                    heapq.heappush(queue, (candidate, previous))
        if exhausted:
            exits = [
                distances[name] + 1.0 for name in self.current.connected
                if name in distances and not self._location_is_locked(self.current.name)
            ]
            distances[self.current.name] = min(exits, default=float("inf"))
        self._route_cache = (key, distances)
        return distances

    def _goal_drive(self):
        """How hard the story pursues its goal right now: the genome's goal_bias,
        paced like a story arc. Off during the opening, ramping up to full
        strength between mission_settings.pursuit_start_step and
        pursuit_full_step, so the goal becomes the story's climax."""
        start = config.get("mission_settings.pursuit_start_step", 12)
        full = max(start + 1, config.get("mission_settings.pursuit_full_step", 30))
        pacing = min(1.0, max(0.0, (self.actions_taken - start) / (full - start)))
        return self.genome.goal_bias * pacing

    def _goal_route_pull(self, location_name):
        """Genome-scaled bonus for destinations on the way to the mission goal."""
        return (
            self.goal_route_score(location_name)
            * config.get("mission_settings.route_weight", 6.0)
            * self._goal_drive()
        )

    def _discovery_route_score(self):
        """Route value of discovering a new path from here: half the score of the
        best undiscovered spawn target that is closer to the goal."""
        distances = self._goal_distances()
        here = distances.get(self.current.name, float("inf"))
        undiscovered = [
            child for child in self.world.spawn_rules.get(self.current.name, [])
            if child not in self.current.connected and child in distances and distances[child] < here
        ]
        best = min(undiscovered, key=lambda child: distances[child], default=None)
        return 0.5 * self.goal_route_score(best) if best else 0.0

    def goal_route_score(self, location_name):
        """0..1: how close a location is to where the mission goal can be advanced."""
        distances = self._goal_distances()
        if location_name not in distances:
            return 0.0
        return 1.0 / (1.0 + distances[location_name])

    def _mission_affinity(self, a, depth=0, npc=None):
        """How strongly an action works toward the mission: direct goal
        conditions it advances, plus progress along the route to the goal."""
        pending = self._pending_goal()
        if not pending:
            return 0.0
        hits = float(sum(
            1 for key, value in pending.items()
            if key != "location" and self._advances(a, key, value, npc)
        ))

        # Route progress: travelling to, revealing, or discovering a path to a
        # location closer to the goal. Reaching a goal location scores 1.
        distances = self._goal_distances()
        here = distances.get(self.current.name, float("inf"))
        route = 0.0
        for o in a.outcomes:
            target = o.move_to or o.send_to or o.reveal_location
            if target in distances and distances[target] < here:
                route = max(route, self.goal_route_score(target))
            if o.spawn:
                route = max(route, self._discovery_route_score())
        hits += route

        # Look inside the event a trigger action leads to (bounded, events can chain).
        event = self.world.events.get(a.triggers_event) if a.triggers_event else None
        if event and depth < 2:
            hits += max(
                (self._mission_affinity(ev_a, depth + 1, npc=a.character_name or npc) for ev_a in event.actions),
                default=0.0,
            )
        return hits

    def _check_end_conditions(self):
        if self.health_player.is_dead:
            return self._end("death", self.narrate("end_death"))

        if self.mission and not self.won and self._goal_met(self.mission):
            self.won = True
            complete = self.narrate("mission_complete", title=self.mission.title)
            self.history.append(complete)
            if self.interactive or self.verbose:
                print(f"  >>> {complete}")
            if config.get("mission_settings.end_on_goal", True):
                return self._end("mission", self.narrate("end_mission", title=self.mission.title))

        end_cfg = config.get("narrative_end", {})
        max_actions = max(100, end_cfg.get("max_actions", 100))
        if self.actions_taken >= max_actions:
            return self._end("action_limit", self.narrate("end_action_limit", limit=max_actions))

        max_consecutive = end_cfg.get("max_consecutive_steps_in_location", 5)
        if self.consecutive_steps_in_location >= max_consecutive:
            return self._end("stalled", self.narrate("end_stalled", location=self.current.name))

        if self.current.name == "Dungeon" and end_cfg.get("end_on_dungeon", False):
            return self._end("dungeon", self.narrate("end_dungeon", location=self.current.name))

        return False

    def _end(self, kind, reason):
        """End the story. ``end_kind`` is what scoring uses; ``end_reason`` is story text."""
        self.ended = True
        self.end_kind = kind
        self.end_reason = reason
        return True

    def _finalize_if_ended(self):
        if self._check_end_conditions():
            end_msg = self.narrate("end_line", reason=self.end_reason)
            self.history.append(end_msg)
            if self.interactive or self.verbose:
                print(f"  >>> {end_msg}")
            return True
        return False

    def _score_character_visibility(self, char):
        score = self._npc_location_affinity(char)
        if "social" in char.associated_tags:
            score += self.genome.bias_social
        if "threat" in char.associated_tags:
            score += self.genome.bias_threat * 0.5
        if "urban" in char.associated_tags:
            score += self.genome.bias_urban * 0.25
        return score

    def _npc_location_affinity(self, char, location=None):
        """Score an NPC against a location (default: current) using contextual tags."""
        location = location or self.current
        generic_tags = set(
            config.get(
                "scene.generic_npc_tags",
                ["urban", "social", "discovery", "threat", "success", "neutral"],
            )
        )
        npc_tags = {tag.lower() for tag in char.associated_tags}
        location_tags = {tag.lower() for tag in location.tags}
        distinctive_npc_tags = npc_tags - generic_tags
        distinctive_location_tags = location_tags - generic_tags
        exact_matches = distinctive_npc_tags & distinctive_location_tags

        score = len(exact_matches) * config.get("scene.npc_exact_tag_weight", 1.25)
        score += semantic_tag_similarity(
            distinctive_npc_tags,
            distinctive_location_tags,
        ) * config.get("scene.npc_semantic_weight", 1.0)
        score += len(npc_tags & location_tags & generic_tags) * config.get(
            "scene.npc_generic_tag_weight", 0.05
        )
        if location.name in char.known_locations:
            score += config.get("scene.npc_known_location_bonus", 3.0)
        configured_tags = {
            str(tag).lower()
            for tag in config.get("scene.location_npc_tags", {}).get(location.name, [])
        }
        score += len(npc_tags & configured_tags) * config.get("scene.location_tag_match_bonus", 4.0)
        return score

    def _npc_fits_location(self, char, location=None):
        location = location or self.current
        if location.name in char.known_locations:
            return True

        configured_tags = {
            str(tag).lower()
            for tag in config.get("scene.location_npc_tags", {}).get(location.name, [])
        }
        npc_all_tags = {tag.lower() for tag in char.associated_tags}
        if configured_tags:
            return bool(npc_all_tags & configured_tags)

        generic_tags = set(
            config.get(
                "scene.generic_npc_tags",
                ["urban", "social", "discovery", "threat", "success", "neutral"],
            )
        )
        npc_tags = {tag.lower() for tag in char.associated_tags} - generic_tags
        location_tags = {tag.lower() for tag in location.tags} - generic_tags
        if npc_tags & location_tags:
            return True
        return semantic_tag_similarity(npc_tags, location_tags) >= config.get(
            "scene.npc_similarity_threshold", 0.68
        )

    def _assign_npc_homes(self):
        """Give every unique NPC the single location it fits best for this game."""
        homes = {}
        for char in self.world.characters:
            if not char.unique:
                continue
            candidates = [loc for loc in self.world.locations.values() if self._npc_fits_location(char, loc)]
            if not candidates:
                candidates = [self.world.locations[name] for name in char.known_locations if name in self.world.locations]
            if candidates:
                homes[char.name] = max(
                    candidates,
                    key=lambda loc: (self._npc_location_affinity(char, loc), loc.name),
                ).name
        return homes

    def _npc_can_appear_here(self, char, explicit=False):
        """Unique NPCs only appear at their home; generic ones wherever they fit.

        ``explicit`` is used for writer-driven reveals, which bypass tag fitting.
        """
        if self._is_removed(char.name):
            return False
        if char.unique:
            return self.npc_homes.get(char.name) == self.current.name
        return explicit or self._npc_fits_location(char)

    def _location_is_locked(self, location_name=None):
        """Whether the player cannot leave a location until they escape.

        With ``lock_when: sent`` (the default) only a player who was *sent* there
        (``send_to``, e.g. an arrest) is confined; one who walked in by a normal
        route may leave. ``lock_when: always`` confines everyone until an escape.
        """
        location_name = location_name or self.current.name
        rule = config.get("location_constraints", {}).get(location_name, {})
        if not rule.get("requires_escape", False):
            return False
        if rule.get("lock_when", "sent") == "always":
            return location_name not in self.unlocked_locations
        return self.confined_in == location_name

    def _score_object_visibility(self, obj, location=None):
        location = location or self.current
        score = semantic_tag_similarity(obj.associated_tags, location.tags) * 2.0
        if "urban" in obj.associated_tags:
            score += self.genome.bias_urban * 0.4
        if "maritime" in obj.associated_tags:
            score += self.genome.bias_maritime * 0.4
        if "combat" in obj.associated_tags:
            score += self.genome.bias_threat * 0.2
        return score

    def _commit_score(self, target_name):
        """How strongly the genome wants to head down a newly found path."""
        target = self.world.locations[target_name]
        score = semantic_tag_similarity(self.current.tags, target.tags)
        score += self.genome.voyage_bias
        score += self.genome.discovery_bias * 0.4
        score -= self.location_counts.get(target_name, 0) * 0.6
        # Paths toward the mission goal are more tempting as the climax nears.
        score += self.goal_route_score(target_name) * self._goal_drive()
        return score

    def _should_commit_to_new_path(self, target_name, threshold=None):
        if not target_name or target_name not in self.world.locations:
            return False
        if threshold is None:
            threshold = config.get("game_state.auto_travel_commit_threshold", 1.2)
        return self._commit_score(target_name) >= threshold

    def _location_objective_score(self, location, intent_tags=None, character=None):
        """Calculate deterministic narrative value for a possible transition."""
        transition_cfg = config.get("transition_objective", {})
        intent_tags = set(intent_tags or [])
        location_tags = {tag.lower() for tag in location.tags}
        current_tags = {tag.lower() for tag in self.current.tags}

        score = 0.0
        score += semantic_tag_similarity(intent_tags, location_tags) * transition_cfg.get(
            "intent_similarity_weight", 4.0
        )
        score += (1.0 - semantic_tag_similarity(current_tags, location_tags)) * transition_cfg.get(
            "novelty_weight", 2.0
        )
        score += len(intent_tags & location_tags) * transition_cfg.get("exact_intent_weight", 2.0)
        score += len(location.actions) * transition_cfg.get("available_action_weight", 0.2)
        score -= self.location_counts.get(location.name, 0) * transition_cfg.get(
            "revisit_penalty", 2.5
        )

        progress = self.actions_taken / max(100, config.get("narrative_end.max_actions", 100))
        phase_tags = (
            {"exploration", "urban", "maritime", "trade", "nature"}
            if progress < 0.3
            else {"threat", "social", "dark", "guarded", "discovery"}
            if progress < 0.75
            else {"palace", "vault", "luxury", "payoff", "success"}
        )
        score += len(location_tags & phase_tags) * transition_cfg.get("story_phase_weight", 0.8)

        bias_map = {
            "urban": self.genome.bias_urban,
            "maritime": self.genome.bias_maritime,
            "social": self.genome.bias_social,
            "threat": self.genome.bias_threat,
            "stealth": self.genome.bias_stealth,
            "luxury": self.genome.bias_luxury,
        }
        score += sum(bias_map[tag] for tag in location_tags if tag in bias_map)
        score += self.genome.voyage_bias * transition_cfg.get("voyage_weight", 1.0)
        score += self.genome.discovery_bias * transition_cfg.get("discovery_weight", 0.8)
        score += self._goal_route_pull(location.name)
        if character is not None:
            score += semantic_tag_similarity(character.associated_tags, location_tags) * transition_cfg.get(
                "character_goal_weight", 2.0
            )
            if location.name in character.known_locations:
                score += transition_cfg.get("known_location_bonus", 3.0)
        return score

    def _best_transition_destination(
        self,
        candidate_names=None,
        intent_tags=None,
        character=None,
        exclude=None,
        allow_special=False,
    ):
        """Choose the highest-value destination without random sampling."""
        exclude = set(exclude or ()) | {self.current.name}
        names = candidate_names or self.world.locations.keys()
        candidates = [
            self.world.locations[name]
            for name in names
            if name in self.world.locations
            and name not in exclude
            and (name != "Dungeon" or allow_special)
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda location: (
                self._location_objective_score(location, intent_tags, character),
                location.name,
            ),
        )

    def _outcome_objective_score(self, outcome, action):
        """Score an action outcome against current story and survival objectives."""
        objective_cfg = config.get("outcome_objective", {})
        tags = {tag.lower() for tag in outcome.tags}
        # Healing at full health is worth nothing: score the change that would apply.
        score = self.health_player.effective_change(outcome.health_change) * objective_cfg.get("health_weight", 1.5)
        # Novelty: prefer outcomes the story has not shown yet.
        score -= (
            self.outcome_counts[outcome.desc]
            * self.genome.novelty_bias
            * objective_cfg.get("repetition_penalty", 1.5)
        )
        score += outcome.coin_change * objective_cfg.get("coin_weight", 0.15)
        score += outcome.success_probability(self.genome, self, action) * objective_cfg.get(
            "success_probability_weight", 2.0
        )
        if outcome.spawn or outcome.reveal_location or outcome.reveal_npc or outcome.reveal_object:
            score += self.genome.discovery_bias * objective_cfg.get("discovery_weight", 2.0)
        if outcome.move_to or outcome.send_to or outcome.move_to_tags or outcome.lead_to_known:
            score += self.genome.voyage_bias * objective_cfg.get("movement_weight", 2.5)
        if "threat" in tags or "combat" in tags:
            score += self.genome.threat_prob * objective_cfg.get("threat_interest_weight", 1.0)
            if self.health_player.is_low:
                score -= objective_cfg.get("low_health_risk_penalty", 3.0)
        if "success" in tags:
            score += objective_cfg.get("success_tag_bonus", 1.0)
        if any(tag.startswith("setup_") for tag in tags) and self.actions_taken < 5:
            score += self.genome.setup_prob * objective_cfg.get("setup_weight", 1.5)
        if any(tag.startswith("payoff_") for tag in tags) and self.actions_taken >= 4:
            score += objective_cfg.get("payoff_weight", 2.0)

        # Outcomes that advance the mission (e.g. revealing the goal item).
        score += sum(
            1 for key, value in self._pending_goal().items()
            if key in {"location", "item", "npc_removed", "outcome_tag", "coins"}
            and self._advances(action, key, value, outcomes=[outcome])
        ) * config.get("action_selection.mission_goal_weight", 6.0) * self._goal_drive()
        if outcome.spawn:
            score += (
                self._discovery_route_score()
                * config.get("mission_settings.route_weight", 6.0)
                * self._goal_drive()
            )

        target_name = outcome.move_to or outcome.reveal_location
        if target_name in self.world.locations:
            score += self._location_objective_score(
                self.world.locations[target_name], outcome.move_to_tags or tags
            )
        return score

    def _choose_calculated_outcome(self, action):
        if not action.outcomes:
            return None
        return max(
            action.outcomes,
            key=lambda outcome: (self._outcome_objective_score(outcome, action), outcome.desc),
        )

    def _inventory_combat_profile(self):
        combat_cfg = config.get("combat_resolution", {})
        attack_values = combat_cfg.get("attack_tags", {})
        defense_values = combat_cfg.get("defense_tags", {})
        attack = 0.0
        defense = 0.0

        for obj in self.world.objects:
            if obj.name not in self.inventory:
                continue
            tags = {tag.lower() for tag in obj.associated_tags}
            for tag, value in attack_values.items():
                if tag in tags:
                    attack += value
            for tag, value in defense_values.items():
                if tag in tags:
                    defense += value

        return attack, defense

    def update_power(self):
        return self.health_player.calculate_power(*self._inventory_combat_profile())

    def _rank_actions(self, actions):
        ranked = []
        for a in actions:
            all_o_tags = []
            discovery_score = 0
            movement_score = 0
            recovery_score = 0
            weighted_outcomes = [(o, 1.0) for o in a.outcomes]
            # A trigger action is worth what its event offers; the player will
            # pick one of the event's actions, so each counts as a share.
            event = self.world.events.get(a.triggers_event) if a.triggers_event else None
            if event and event.actions:
                share = 1.0 / len(event.actions)
                weighted_outcomes += [(o, share) for ev_a in event.actions for o in ev_a.outcomes]
            for o, share in weighted_outcomes:
                all_o_tags.extend(o.tags)
                if o.spawn or o.reveal_location or o.reveal_npc or o.reveal_object:
                    discovery_score += share
                if o.move_to or o.send_to or o.move_to_tags or o.lead_to_known:
                    movement_score += share
                if o.health_change > 0 or o.coin_change > 0:
                    recovery_score += share

            weight = config.get("action_selection.base_weight", 0.1)
            if "threat" in all_o_tags or "guards" in all_o_tags:
                weight += self.genome.threat_prob * config.get("action_selection.threat_weight", 1.0)
            if "spawn" in all_o_tags:
                weight += self.genome.spawn_prob * config.get("action_selection.spawn_weight", 1.2)
            if any(t.startswith("setup_") for t in all_o_tags):
                weight += self.genome.setup_prob * config.get("action_selection.setup_weight", 1.0)
            if discovery_score:
                weight += discovery_score * self.genome.discovery_bias * config.get("action_selection.discovery_weight", 1.2)
            if movement_score:
                weight += movement_score * self.genome.voyage_bias * config.get("action_selection.movement_weight", 1.4)
            if recovery_score and self.health_player.is_low:
                weight += recovery_score * self.genome.recovery_bias * config.get("action_selection.recovery_weight", 1.2)
            if a.triggers_event:
                weight += self.genome.event_prob * config.get("action_selection.event_trigger_weight", 1.5)
            # Novelty: every earlier use of the same choice makes it less appealing.
            weight -= (
                self.choice_counts[self._novelty_key(a)]
                * self.genome.novelty_bias
                * config.get("action_selection.repetition_penalty", 1.0)
            )
            weight += (
                self._mission_affinity(a)
                * config.get("action_selection.mission_goal_weight", 6.0)
                * self._goal_drive()
            )

            is_movement = any(o.move_to or o.send_to or o.move_to_tags or o.spawn or o.lead_to_known or o.reveal_location for o in a.outcomes)
            if is_movement:
                stay_penalty_power = config.get("action_selection.stay_penalty_power", 2.0)
                weight *= (1.0 + (self.genome.voyage_bias * self.consecutive_steps_in_location) ** stay_penalty_power)

            selection_cfg = config.get("action_selection", {})
            location_cfg = selection_cfg.get("location_penalties", {}).get(self.current.name, {})
            penalty = 0.0
            action_name = a.name.lower()
            for term, value in location_cfg.get("action_terms", {}).items():
                if str(term).lower() in action_name:
                    penalty += float(value)
            destination_names = {
                outcome.move_to for outcome in a.outcomes if outcome.move_to in self.world.locations
            }
            for destination_name in destination_names:
                destination_tags = {tag.lower() for tag in self.world.locations[destination_name].tags}
                for tag, value in location_cfg.get("destination_tags", {}).items():
                    if str(tag).lower() in destination_tags:
                        penalty += float(value)
            weight -= penalty * selection_cfg.get("consistency_penalty_weight", 1.0)

            ranked.append((weight, a))

        ranked.sort(key=lambda pair: (pair[0], pair[1].name), reverse=True)
        return ranked


    # ---- repeatable vs one-time options ------------------------------------

    def _repeat_rule(self, a):
        """once | per_visit | always, from the action or action_rules.default_repeat."""
        defaults = {"location": "per_visit", "npc": "per_visit", "item": "once",
                    "event": "always", "navigation": "always", "conversation": "always"}
        defaults.update(config.get("action_rules.default_repeat") or {})
        return a.repeat or defaults.get(a.source, "per_visit")

    def _action_scope(self, a, location=None):
        """Who a one-time choice belongs to. A generic NPC is a different person in
        each location (the Palace gate guard is not the Vault gate guard)."""
        char = next((c for c in self.world.characters if c.name == a.owner), None) if a.source == "npc" else None
        place = (location or self.current.name) if char is not None and not char.unique else None
        return (a.source, a.owner, place)

    def _is_closed(self, a, location=None):
        """Whether a one-time action or its exclusive group is used up."""
        scope = self._action_scope(a, location)
        if self._repeat_rule(a) == "once" and (scope, a.name) in self.spent_actions:
            return True
        return bool(a.exclusive_group) and (scope, a.exclusive_group) in self.closed_groups

    def _is_offered(self, a):
        if self._is_closed(a):
            return False
        return not (self._repeat_rule(a) == "per_visit" and a.name in self.visit_actions_performed)

    @staticmethod
    def _novelty_key(a):
        # Travelling counts per destination, so Harbor -> Market -> Harbor loops wear out.
        return ("travel", a.owner) if a.source == "navigation" else ("action", a.name)

    def _record_choice(self, a, succeeded, scope):
        self.choice_counts[self._novelty_key(a)] += 1
        if self._repeat_rule(a) == "once":
            self.spent_actions.add((scope, a.name))
        if succeeded and a.exclusive_group:
            # The choice has been made: the other options of the group disappear.
            self.closed_groups.add((scope, a.exclusive_group))

    def _prepare_action(self, a):
        """Resolve item/coin requirements on a copied action; return None if unavailable."""
        # Check for required object/tag
        if a.required_tag:
            matching_items = [obj for obj in self.world.objects
                              if a.required_tag in obj.associated_tags and obj.name in self.inventory]
            if not matching_items:
                return None
            item = sorted(matching_items, key=lambda obj: (self._score_object_visibility(obj), obj.name), reverse=True)[0]
            a.active_item = item.name
            if "{" in a.name and "}" in a.name:
                item_desc = self._pick_text(item.descriptions, offset=len(item.name)) if item.descriptions else item.name
                a.name = re.sub(r'\{.*?\}', item_desc, a.name)

        if a.required_object:
            if a.required_object not in self.inventory:
                return None
            a.active_item = a.required_object

        if a.required_coins and a.required_coins > self.coins:
            return None
        if a.collects_object and a.collects_object in self.inventory:
            return None
        return a

    def _event_source_character(self):
        if not self.event_source_npc:
            return None
        return next((c for c in self.world.characters if c.name == self.event_source_npc), None)

    def _available_event_actions(self):
        """Only the active event's actions are offered; no travel, NPC, or item actions."""
        actions = []
        for template in self.active_event.actions:
            if not self._is_offered(template):
                continue
            a = self._prepare_action(copy.deepcopy(template))
            if a is None:
                continue
            # Event actions act on behalf of the NPC who triggered the event, so
            # remove_npc: true, lead_to_known and NPC tags refer to that NPC.
            a.character_name = a.character_name or self.event_source_npc
            actions.append(a)

        if not actions:
            # Never trap the player when every event action has unmet requirements.
            event_cfg = config.get("authored_events", {})
            actions.append(Action(
                name=event_cfg.get("fallback_action", "Step away from the situation"),
                character_name=self.event_source_npc,
                outcomes=[Outcome(
                    desc=event_cfg.get("fallback_description", "You step back and let the moment pass."),
                    tags=["neutral"],
                )],
            ))
        return actions

    def _visit_id(self):
        return (self.current.name, self.location_counts.get(self.current.name, 0))

    def _announce_arrival(self):
        """Log the location, its NPCs, and its items once per visit."""
        if self.active_event or self._announced_visit == self._visit_id():
            return
        self._announced_visit = self._visit_id()
        desc, npc_lines, object_lines = self._describe_visible_scene()
        self.history.append(self.narrate("arrival", location=self.current.name, description=desc))
        for char_name, char_desc in npc_lines:
            self.history.append(self.narrate("arrival_npc", npc=char_name, description=char_desc))
        for obj_name, obj_desc in object_lines:
            self.history.append(self.narrate("arrival_item", item=obj_name, description=obj_desc))

    def _refresh_scene(self):
        """Place NPCs and items when the player enters a location (or the scene emptied)."""
        # 1. Detect New Location Entry or Stale Inhabitants
        if not self.current_inhabitants or self.current.name != self.last_location_name:
            self.last_location_name = self.current.name
            
            # Determine ALL possible NPCs for this visit. Unique NPCs who live
            # here are always present; generic NPCs fill the remaining slots.
            all_possible = [char for char in self.world.characters if self._npc_can_appear_here(char)]
            residents = [char for char in all_possible if char.unique]
            scored = sorted(
                (char for char in all_possible if not char.unique),
                key=lambda char: (self._score_character_visibility(char), char.name),
                reverse=True,
            )
            count = max(0, config.get("scene.max_visible_npcs", 3) - len(residents))
            self.current_inhabitants = residents + scored[:count]

            # 2. Dynamic NPC visibility (Discovery System)
            self.visible_inhabitants = {c.name for c in self.current_inhabitants}
            self.current_inhabitants = [c for c in self.world.characters if c.name in self.visible_inhabitants]

            # 3. Dynamic Object visibility
            self.location_objects = sorted(
                [obj for obj in self.world.objects if obj.associated_tags & self.current.tags],
                key=lambda obj: (self._score_object_visibility(obj), obj.name),
                reverse=True,
            )
            self.visible_objects = {obj.name for obj in self.location_objects[:config.get("scene.max_visible_objects", 2)]}

        self._announce_arrival()

    def get_available_actions(self):
        """Returns a list of valid actions for the current state with narrative visibility."""
        if self.active_event:
            return self._available_event_actions()

        self._refresh_scene()

        # A hostile NPC in the scene confronts the player before anything else.
        if self._start_encounter():
            return self._available_event_actions()

        # 2. Gather actions for VISIBLE entities only
        raw_actions = []
        raw_actions.extend(self.current.actions)
        
        # Filter inhabitants
        for char in self.current_inhabitants:
            if char.name in self.visible_inhabitants:
                raw_actions.extend(char.actions)
                conversation_detail = self._pick_text(
                    char.descriptions,
                    offset=len(char.name) + self.character_interactions.get(char.name, 0),
                ) or self.narrate("talk_default", stable=char.name, npc=char.name)
                if char.goals:
                    conversation_detail += self.narrate("talk_goal", stable=char.name, npc=char.name, goal=char.goals[0])
                raw_actions.append(
                    Action(
                        name=f"({char.name}) " + self.narrate("talk_action", stable=char.name, npc=char.name),
                        character_name=char.name,
                        outcomes=[Outcome(
                            desc=conversation_detail,
                            tags=["social", "conversation"],
                        )],
                        repeat="always",
                        source="conversation",
                        owner=char.name,
                    )
                )
        
        # Filter objects
        for obj in self.location_objects:
            if obj.name in self.visible_objects:
                raw_actions.extend(obj.actions)

        # 3. Process & Template Actions
        filtered_actions = [copy.deepcopy(a) for a in raw_actions if self._is_offered(a)]

        final_actions = [
            a for a in (self._prepare_action(a) for a in filtered_actions) if a is not None
        ]

        # 4. Navigation
        navigation_targets = [] if self._location_is_locked() else sorted(self.current.connected)
        for loc_name in navigation_targets:
            target_loc = self.world.locations.get(loc_name)
            name = self.narrate("travel_new", stable=loc_name, location=loc_name)
            if self.location_counts.get(loc_name, 0) > 0:
                name = self.narrate("travel_return", stable=loc_name, location=loc_name)
            elif target_loc and target_loc.interact_prompts:
                name = self._pick_text(target_loc.interact_prompts, offset=len(loc_name))
            
            navigation_action = Action(
                name=name,
                outcomes=[Outcome(
                    desc=self.narrate("travel_result", stable=loc_name, location=loc_name),
                    move_to=loc_name,
                )],
                repeat="always",
                source="navigation",
                owner=loc_name,
            )
            final_actions.append(navigation_action)
        
        return final_actions

    def ranked_available_actions(self, limit=None):
        """Return currently valid actions in the same order used by the simulator."""
        actions = self.get_available_actions()
        attempted_locations = {self.current.name}
        while (
            not actions
            and not self.ended
            and config.get("game_state.auto_transfer_when_stuck", True)
            and len(attempted_locations) < len(self.world.locations)
        ):
            if not self._auto_transfer_when_stuck(exclude=attempted_locations):
                break
            attempted_locations.add(self.current.name)
            actions = self.get_available_actions()
        ranked = [action for _, action in self._rank_actions(actions)]
        return ranked if limit is None else ranked[:limit]

    def _auto_transfer_when_stuck(self, exclude=None):
        """Move a stuck player to a useful location without consuming an action."""
        if self._location_is_locked():
            return False
        exclude = set(exclude or ())
        exclude.add(self.current.name)

        configured_targets = self.world.spawn_rules.get(self.current.name, [])
        target = self._best_transition_destination(
            candidate_names=configured_targets or None,
            intent_tags=self.current.tags,
            exclude=exclude,
        )
        if target is None:
            return False

        origin = self.current.name
        self.current.connect(target)
        if not self._move_to(target.name):
            return False

        message = self.narrate("auto_transfer", origin=origin, destination=target.name)
        self.history.append(message)
        if self.interactive or self.verbose:
            print(f"  >>> {message}")
        return True

    def _maybe_trigger_unexpected_event(self):
        """Create a lightweight procedural event from configurable narrative parts."""
        event_cfg = config.get("unexpected_events", {})
        if not event_cfg.get("enabled", True):
            return None
        if len(self.unexpected_events) >= int(event_cfg.get("max_per_story", 4)):
            return None
        cooldown = int(event_cfg.get("cooldown_steps", 2))
        if self.actions_taken - self._last_event_step < cooldown:
            return None

        base_chance = float(event_cfg.get("base_chance", 0.12))
        event_pressure = base_chance + self.genome.event_prob * float(
            event_cfg.get("genome_weight", 0.35)
        )
        event_pressure += self.genome.chaos_bias * float(event_cfg.get("chaos_weight", 0.2))
        event_pressure += self.consecutive_steps_in_location * event_cfg.get(
            "stagnation_pressure", 0.04
        )
        if event_pressure < event_cfg.get("objective_threshold", 0.24):
            return None

        actors = event_cfg.get("actors", ["a masked courier", "a frightened witness"])
        omens = event_cfg.get("omens", ["a bell rings backwards", "the lights suddenly fail"])
        twists = event_cfg.get("twists", ["offers a dangerous bargain", "reveals a hidden route"])
        objective_index = (
            self.actions_taken
            + self.location_counts.get(self.current.name, 0)
            + sum(ord(character) for character in self.current.name)
        )
        actor = actors[objective_index % len(actors)]
        omen = omens[(objective_index + len(self.path)) % len(omens)]
        twist = twists[(objective_index + len(self.tags_seen)) % len(twists)]
        description = f"{omen.capitalize()}. {actor.capitalize()} {twist}."

        effects = event_cfg.get("effects", [{"tags": ["unexpected", "social"]}])

        def effect_objective(effect):
            effect_tags = {tag.lower() for tag in effect.get("tags", [])}
            score = self.health_player.effective_change(effect.get("health_change", 0)) * (
                2.0 if self.health_player.is_low else 0.5
            )
            score += effect.get("coin_change", 0) * 0.1
            if effect.get("spawn"):
                score += self.genome.discovery_bias + self.genome.voyage_bias
            if "threat" in effect_tags:
                score += self.genome.threat_prob
                if self.health_player.is_low:
                    score -= 2.0
            if "social" in effect_tags:
                score += self.genome.bias_social
            if "success" in effect_tags or "recovery" in effect_tags:
                score += self.genome.recovery_bias
            return score

        effect = max(
            effects,
            key=lambda candidate: (effect_objective(candidate), str(candidate)),
        )
        tags = list(effect.get("tags", ["unexpected"]))
        if "unexpected" not in tags:
            tags.append("unexpected")
        coin_change = int(effect.get("coin_change", 0))

        health_change = self.health_player.change(effect.get("health_change", 0))
        self.coins = max(0, self.coins + coin_change)  # a purse cannot go below empty
        self.update_power()
        self.tags_seen.append(tags)
        self.unexpected_events.append(description)
        self._last_event_step = self.actions_taken
        message = self.narrate("unexpected_event", description=description)
        self.history.append(message)
        if health_change or coin_change:
            self.history.append(self.narrate("unexpected_effect", health=health_change, coins=coin_change))

        if effect.get("spawn"):
            new_location = self.world.spawn(self.current, state=self)
            if new_location:
                self.current.connect(new_location)
                self.history.append(self.narrate("unexpected_discovery", location=new_location.name))

        if self.interactive or self.verbose:
            print(f"  >>> {message}")
        return description

    def _enter_event(self, event_name, source_npc=None):
        """Start an authored Event anchored to the current location."""
        if not config.get("authored_events.enabled", True):
            return False
        event = self.world.events.get(event_name)
        if event is None:
            self.history.append(self.narrate("event_unknown", event=event_name))
            return False

        self.active_event = event
        self.event_return_location = self.current.name
        self.event_source_npc = source_npc if source_npc and not self._is_removed(source_npc) else None
        self.event_description = self._pick_text(event.descriptions) or event.title
        self.event_log.append({
            "event": event.name,
            "location": self.current.name,
            "npc": self.event_source_npc,
            "step": self.actions_taken,
        })
        message = self.narrate("event_start", title=event.title, description=self.event_description)
        self.history.append(message)
        if self.interactive or self.verbose:
            print(f"  >>> {message}")
        return True

    def _finish_event(self, action, succeeded):
        """End the active event after one of its actions, or chain into the next event."""
        event = self.active_event
        return_location = self.event_return_location
        source_npc = self.event_source_npc
        self.active_event = None
        self.event_return_location = None
        self.event_source_npc = None
        self.event_description = None
        self.event_log[-1]["action"] = action.name
        self.event_log[-1]["succeeded"] = succeeded
        if self.health_player.is_dead:
            return

        if succeeded and action.triggers_event:
            self.history.append(self.narrate("event_chain", title=event.title))
            self._enter_event(action.triggers_event, source_npc=source_npc)
            return

        if self.current.name == return_location:
            message = self.narrate("event_end_return", title=event.title, location=return_location)
        else:
            message = self.narrate("event_end_moved", title=event.title, location=self.current.name)
        self.history.append(message)
        if self.interactive or self.verbose:
            print(f"  >>> {message}")

    def _encounter_key(self, char):
        return self._visit_key(char.name) if char.encounter_repeat else char.name

    def _start_encounter(self):
        """Start the encounter event of the first visible NPC not yet encountered."""
        if self.ended or self.active_event:
            return False
        for char in self.current_inhabitants:
            if (
                not char.encounter_event
                or char.name not in self.visible_inhabitants
                or self._is_removed(char.name)
                or self._encounter_key(char) in self.encounters
            ):
                continue
            self.encounters.add(self._encounter_key(char))
            message = self.narrate("encounter", npc=char.name)
            self.history.append(message)
            if self.interactive or self.verbose:
                print(f"  >>> {message}")
            if self._enter_event(char.encounter_event, source_npc=char.name):
                return True
        return False

    def _visit_key(self, npc_name):
        return (npc_name, self.current.name, self.location_counts.get(self.current.name, 0))

    def _is_removed(self, npc_name):
        """A removed unique NPC is gone for good; a removed generic NPC (one bandit
        of many) is only gone from the current visit to the current location."""
        char = next((c for c in self.world.characters if c.name == npc_name), None)
        if char is not None and not char.unique:
            return self._visit_key(npc_name) in self._scene_removals
        return npc_name in self.removed_npcs

    def _remove_npc(self, npc_name):
        """Remove an NPC, e.g. a defeated thief (see _is_removed for the scope)."""
        if not npc_name or self._is_removed(npc_name):
            return
        self.removed_npcs.add(npc_name)
        self._scene_removals.add(self._visit_key(npc_name))
        self.visible_inhabitants.discard(npc_name)
        self.current_inhabitants = [c for c in self.current_inhabitants if c.name != npc_name]
        if self.event_source_npc == npc_name:
            self.event_source_npc = None
        char = next((c for c in self.world.characters if c.name == npc_name), None)
        if char is not None and not char.unique:
            message = self.narrate("npc_removed_scene", npc=npc_name, location=self.current.name)
        else:
            message = self.narrate("npc_removed", npc=npc_name)
        self.history.append(message)
        if self.interactive or self.verbose:
            print(f"  >>> {message}")

    def step(self, action_index=None):
        """Executes one simulation step."""
        if self.ended:
            return False

        actions = self.ranked_available_actions()
        if not actions:
            return False

        in_event = self.active_event is not None
        if not in_event:
            self._maybe_trigger_unexpected_event()
        if self.health_player.is_dead:
            self._finalize_if_ended()
            return False

        # Record beginning of story segment (the arrival itself was logged by
        # _announce_arrival when the scene was placed).
        visit = self._visit_id()
        is_new_loc = visit != self._printed_visit
        self._printed_visit = visit
        desc, npc_lines, object_lines = self._describe_visible_scene()

        if in_event:
            # The event was announced in the history when it started.
            if self.interactive or self.verbose:
                print(f"\n--- EVENT: {self.active_event.title} at {self.current.name} (Health: {self.health_player.status()}) ---")
                print(f"Description: {desc}")
            if self.interactive:
                for char_name, char_desc in npc_lines:
                    print(f"  (NPC) {char_name}: {char_desc}")
        else:
            if not is_new_loc:
                self.history.append(self.narrate("continue_here", location=self.current.name))
            if self.interactive or (self.verbose and is_new_loc):
                status = "is at" if is_new_loc else "remains at"
                print(f"\n--- The traveler {status}: {self.current.name} (Health: {self.health_player.status()}) ---")
                print(f"Location: {self.current.name}")
                print(f"Description: {desc}")
            if self.interactive:
                for char_name, char_desc in npc_lines:
                    print(f"  (NPC) {char_name}: {char_desc}")
                for obj_name, obj_desc in object_lines:
                    print(f"  (Item) {obj_name}: {obj_desc}")

        if self.interactive:
            # Present the most narratively relevant choices first.
            options = actions[:min(5, len(actions))]
            
            print("Choose an action:")
            for i, opt in enumerate(options):
                # Distinguish between Location, NPC, and Item actions
                prefix = ""
                if opt.character_name:
                    if opt.character_name.lower() not in opt.name.lower():
                        prefix = f"({opt.character_name}) "
                elif any(o.name == opt.name for o in self.world.objects):
                    if opt.name.lower() not in opt.name.lower(): # Should always check
                        prefix = f"[{opt.name}] "
                
                print(f" [{i+1}] {prefix}{opt.name}")
            
            choice = -1
            while choice < 0 or choice >= len(options):
                try:
                    val = input("Enter choice (number): ")
                    choice = int(val) - 1
                except ValueError:
                    print("Invalid input. Please enter a number.")
            
            action = options[choice]
        else:
            if action_index is None:
                action_index = 0
            if action_index < 0 or action_index >= len(actions):
                raise IndexError(f"Action index {action_index} is out of range.")
            action = actions[action_index]

        # Record beginning of story segment
        is_new_loc = self.current.name != self.last_location_name
        self.last_location_name = self.current.name
        self.history.append(self.narrate("action_taken", action=action.name))
        choice_scope = self._action_scope(action)  # before the action can move the player
        if self._repeat_rule(action) == "per_visit":
            self.visit_actions_performed.add(action.name)
        
        # Outcomes and their transitions are selected by the objective function.
        outcome = self._choose_calculated_outcome(action)
        if outcome is None:
            return False
        self.actions_taken += 1
        is_conversation = "conversation" in {tag.lower() for tag in outcome.tags}
        if not is_conversation and not in_event:
            self.consecutive_steps_in_location += 1
        
        # Reset stay counter if we move
        if outcome.move_to:
            self.consecutive_steps_in_location = 0

        calculated_success = outcome.success_probability(
            genome=self.genome,
            state=self,
            action=action,
        ) >= config.get("outcome_objective.success_threshold", 0.5)
        if calculated_success:
            if self.interactive:
                print(f"\nRESULT: {outcome.desc}")
            self.succeeded_actions |= _action_keys(action.name)
            self.succeeded_outcome_tags.update(tag.lower() for tag in outcome.tags)
            self.apply(outcome, action=action)
        else:
            if self.verbose:
                print(f"  FAILED: {action.name}")
            _, defense_bonus = self._inventory_combat_profile()
            outcome_tags = {tag.lower() for tag in outcome.tags}
            dangerous = self._is_guard_failure(action, outcome) or bool(outcome_tags & {"combat", "threat"})
            self.health_player.damage(HealthPlayer.failure_penalty(defense_bonus, dangerous))
            self.update_power()
            self.tags_seen.append("failure")
            
            # Narrative Consequence: Failure against guards leads to arrest
            if self._is_guard_failure(action, outcome):
                arrest_location = config.get("game_state.arrest_location", "Dungeon")
                arrest_msg = self.narrate("failure_arrest", action=action.name, location=arrest_location)
                self.history.append(arrest_msg)
                if self.interactive or self.verbose:
                    print(f"  >>> {arrest_msg}")
                # Arrested: sent there one way, not a route the player can walk back.
                outcome.send_to = arrest_location
                outcome.move_to = None
                outcome.reveal_location = None
                self.apply(outcome, action=action)
            else:
                fail_msg = self.narrate("failure", action=action.name)
                self.history.append(fail_msg)
                if self.interactive or self.verbose:
                    print(f"  >>> {fail_msg}")
                if self._finalize_if_ended():
                    return False

        self._record_choice(action, calculated_success, choice_scope)

        # Any choice inside an event ends it; a successful trigger action starts one.
        if in_event:
            self._finish_event(action, calculated_success)
        elif calculated_success and action.triggers_event and self.health_player.is_alive:
            self._enter_event(action.triggers_event, source_npc=action.character_name)

        if self._finalize_if_ended():
            return False
        # A hostile NPC at the destination confronts the player on arrival.
        if not self.active_event:
            self._refresh_scene()
            self._start_encounter()
        return True

    def resolve_destination(self, move_to=None, move_to_tags=None):
        """Objective-driven selection of a target location among candidates."""
        if move_to and move_to in self.world.locations:
            return move_to

        if not move_to_tags:
            return None

        target = self._best_transition_destination(intent_tags=move_to_tags)
        return target.name if target else None

    def discover_new_node(self, node_type=None, source_npc=None):
        """Genome-driven discovery of hidden inhabitants or objects, prioritized by source NPC's knowledge."""
        if not node_type:
            # Voyage bias increases the chance of discovering a location lead
            node_scores = {
                "loc": 0.3 + self.genome.voyage_bias * 0.4,
                "npc": 0.2 + self.genome.bias_social * 0.3,
                "obj": 0.2 + self.genome.discovery_bias * 0.3,
            }
            node_type = max(node_scores, key=node_scores.get)

        # 1. CHARACTER KNOWLEDGE PRIORITY
        if source_npc:
            char_obj = next((c for c in self.world.characters if c.name == source_npc), None)
            if char_obj:
                if node_type == "npc":
                    # Characters don't have known_npcs yet, so we fall back to generic but could be added later
                    pass
                elif node_type == "obj" and char_obj.known_objects:
                    hidden_known = [o for o in char_obj.known_objects 
                                   if o in [lo.name for lo in self.location_objects] 
                                   and o not in self.visible_objects]
                    if hidden_known:
                        picked = sorted(hidden_known)[0]
                        self.visible_objects.add(picked)
                        return "obj", picked
                elif node_type == "loc" and char_obj.known_locations:
                    # Locations are global, check if we haven't connected them yet
                    hidden_locs = [
                        l for l in char_obj.known_locations
                        if l not in self.current.connected and l != self.current.name
                    ]
                    if hidden_locs:
                        scored = []
                        for loc_name in hidden_locs:
                            loc = self.world.locations.get(loc_name)
                            if not loc:
                                continue
                            score = semantic_tag_similarity(loc.tags, self.current.tags)
                            score += semantic_tag_similarity(loc.tags, char_obj.associated_tags)
                            scored.append((score, loc_name))
                        if scored:
                            scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
                            picked = scored[0][1]
                        else:
                            picked = sorted(hidden_locs)[0]
                        return "loc", picked

        # 2. GENERIC TAG-BASED FALLBACK
        if node_type == "npc":
            hidden = [c for c in self.current_inhabitants if c.name not in self.visible_inhabitants]
            if not hidden: return None, None
            picked = sorted(
                hidden,
                key=lambda c: (self._score_character_visibility(c), c.name),
                reverse=True,
            )[0]
            self.visible_inhabitants.add(picked.name)
            return "npc", picked.name
            
        elif node_type == "obj":
            hidden = [o for o in self.location_objects if o.name not in self.visible_objects]
            if not hidden: return None, None
            picked = sorted(
                hidden,
                key=lambda o: (self._score_object_visibility(o), o.name),
                reverse=True,
            )[0]
            self.visible_objects.add(picked.name)
            return "obj", picked.name
            
        elif node_type == "loc":
            # Just trigger a generic spawn
            return "loc", None

        return None, None

    def _move_to(self, target_name, one_way=False):
        """Helper to transition to a new location and update history/counters.

        ``one_way`` (a ``send_to`` outcome) links the origin to the target but not
        back, and confines the player there when the target requires an escape.
        """
        if target_name != self.current.name and self._location_is_locked():
            return False
        if target_name not in self.world.locations:
            forced_loc = self.world.spawn(
                self.current,
                force_target=target_name,
                state=self,
                allow_special=(target_name == "Dungeon"),
            )
            if forced_loc:
                spawn_msg = self.narrate("spawn_forced", location=forced_loc.name)
                self.history.append(spawn_msg)
                if self.interactive: print(f"  >>> {spawn_msg}")
                if not one_way:
                    self.current.connect(forced_loc)
            else:
                return False

        origin = self.current
        target = self.world.locations[target_name]
        if one_way:
            origin.connect_one_way(target)
            rule = config.get("location_constraints", {}).get(target_name, {})
            if rule.get("requires_escape", False):
                self.confined_in = target_name
                self.unlocked_locations.discard(target_name)  # every arrest needs a new escape
        else:
            origin.connect(target)
        self.last_location_name = origin.name
        self.current = target
        self.location_counts[self.current.name] = self.location_counts.get(self.current.name, 0) + 1
        self.path.append(self.current.name)
        self.consecutive_steps_in_location = 0
        self.visit_actions_performed = set()
        return True

    def apply(self, outcome, action=None):
        """Applies an action's outcome to the game state."""
        origin_location = self.current.name

        # Update tags seen for narrative memory
        # Merge outcome tags with situational tags (Location or NPC) for richer fitness evaluation
        situational_tags = set(outcome.tags)
        if action:
            if action.character_name:
                self.character_interactions[action.character_name] = self.character_interactions.get(action.character_name, 0) + 1
                char = next((c for c in self.world.characters if c.name == action.character_name), None)
                if char: situational_tags.update(char.associated_tags)
            else:
                situational_tags.update(self.current.tags)
        if self.active_event:
            situational_tags.update(self.active_event.tags)
        # Engine-driven travel must not pull the player out of an event, or away
        # from the location where an event is about to start.
        event_anchored = bool(self.active_event or (action and action.triggers_event))

        self.tags_seen.append(list(situational_tags))
        self.outcome_counts[outcome.desc] += 1
        self.history.append(self.narrate("result", description=outcome.desc))
        self.history.append(self.narrate("location_current", location=origin_location))

        # 0. Apply Outcome Mechanical Changes
        lower_tags = {tag.lower() for tag in outcome.tags}
        constraint = config.get("location_constraints", {}).get(origin_location, {})
        escape_tags = {str(tag).lower() for tag in constraint.get("escape_tags", ["escape"])}
        if constraint.get("requires_escape", False) and lower_tags & escape_tags:
            self.unlocked_locations.add(origin_location)
            if self.confined_in == origin_location:
                self.confined_in = None
            escape_msg = self.narrate("escape", location=origin_location)
            self.history.append(escape_msg)
            if self.interactive or self.verbose:
                print(f"  >>> {escape_msg}")
        health_delta = outcome.health_change
        if "combat" in lower_tags or "threat" in lower_tags:
            health_delta = HealthPlayer.mitigated_combat_change(health_delta, *self._inventory_combat_profile())
        applied = self.health_player.change(health_delta)
        if outcome.health_change > 0 and applied < outcome.health_change:
            self.history.append(self.narrate(
                "health_capped", health=self.health_player.health, max_health=self.health_player.max_health,
            ))
        self.coins = max(0, self.coins + outcome.coin_change)  # a purse cannot go below empty
        self.update_power()
        if self.interactive or self.verbose:
            print(f"  >>> Health: {self.health_player.status()} | Coins: {self.coins} | Power: {self.power:.2f}")

        # 1. Reveal and spawn logic
        if outcome.spawn or outcome.reveal_location:
            target = outcome.reveal_location if outcome.reveal_location else None
            new_loc = self.world.spawn(
                self.current,
                force_target=target,
                state=self,
                allow_special=(target == "Dungeon"),
            )
            if new_loc and new_loc.name != self.current.name:
                self.current.connect(new_loc)
                spawn_msg = self.narrate("spawn", location=new_loc.name)
                self.history.append(spawn_msg)
                if self.interactive or self.verbose:
                    print(f"  >>> {spawn_msg}")
                # Deterministic and genome-driven (no dice roll): follow the new
                # path only when it is compelling enough for this genome.
                if (
                    outcome.move_to is None
                    and not event_anchored
                    and config.get("game_state.auto_travel_on_spawn", True)
                    and self._should_commit_to_new_path(
                        new_loc.name, config.get("game_state.auto_travel_on_spawn_threshold", 1.5),
                    )
                ):
                    outcome.move_to = new_loc.name
                    travel_msg = self.narrate("voyage", location=new_loc.name)
                    self.history.append(travel_msg)
                    if self.interactive or self.verbose:
                        print(f"  >>> {travel_msg}")

        revealed = next((c for c in self.world.characters if c.name == outcome.reveal_npc), None)
        if revealed and self._npc_can_appear_here(revealed, explicit=True):
            self.visible_inhabitants.add(revealed.name)
            if revealed not in self.current_inhabitants:
                self.current_inhabitants.append(revealed)
            msg = self.narrate("reveal_npc", npc=outcome.reveal_npc)
            self.history.append(msg)
            if self.interactive or self.verbose:
                print(f"  >>> {msg}")

        if outcome.reveal_object:
            self.visible_objects.add(outcome.reveal_object)
            msg = self.narrate("reveal_item", item=outcome.reveal_object)
            self.history.append(msg)
            if self.interactive or self.verbose:
                print(f"  >>> {msg}")

        if outcome.remove_npc:
            removed_name = action.character_name if outcome.remove_npc is True and action else outcome.remove_npc
            if isinstance(removed_name, str):
                self._remove_npc(removed_name)

        # 2. NPC-driven leads can unlock travel even if the action itself does not move
        if action and action.character_name and not self._is_removed(action.character_name):
            c_name = action.character_name
            if self.character_interactions[c_name] % 2 == 0 or "success" in outcome.tags or "social" in outcome.tags:
                node_type, node_name = self.discover_new_node(source_npc=c_name)
                if node_type == "loc":
                    new_loc = self.world.spawn(self.current, force_target=node_name, state=self)
                    if new_loc:
                        self.current.connect(new_loc)
                        lead_msg = self.narrate("lead_path", npc=c_name, location=new_loc.name)
                        self.history.append(lead_msg)
                        if self.interactive or self.verbose:
                            print(f"  >>> {lead_msg}")
                        if (
                            "conversation" not in lower_tags
                            and outcome.move_to is None
                            and not event_anchored
                            and self._should_commit_to_new_path(new_loc.name)
                        ):
                            outcome.move_to = new_loc.name

        # 3. Handle Regular Movement (a one-way send_to wins over everything else)
        if outcome.send_to:
            if self._move_to(outcome.send_to, one_way=True):
                sent_msg = self.narrate("sent", location=outcome.send_to)
                self.history.append(sent_msg)
                if self.interactive or self.verbose:
                    print(f"  >>> {sent_msg}")

        elif outcome.move_to:
            target_name = self.resolve_destination(move_to=outcome.move_to)
            if target_name:
                self._move_to(target_name)

        elif outcome.move_to_tags:
            target_name = self.resolve_destination(move_to_tags=outcome.move_to_tags)
            if target_name:
                self._move_to(target_name)

        # 4. Handle Guided Movement (NPC Lead)
        if getattr(outcome, 'lead_to_known', False) and action and action.character_name:
            char = next((c for c in self.world.characters if c.name == action.character_name), None)
            if char and char.known_locations:
                candidates = [l for l in char.known_locations if l in self.world.locations and l != self.current.name]
                if candidates:
                    scored = []
                    for loc_name in candidates:
                        loc = self.world.locations[loc_name]
                        score = 1.0 + len(loc.tags & char.associated_tags) * 0.5 + semantic_tag_similarity(loc.tags, char.associated_tags)
                        if "social" in loc.tags: score += self.genome.bias_social
                        if "urban" in loc.tags: score += self.genome.bias_urban
                        scored.append((score, loc_name))
                    scored.sort(key=lambda x: x[0], reverse=True)
                    target = scored[0][1]
                    
                    self.history.append(self.narrate("lead_move", npc=char.name, location=target))
                    if self.interactive: print(f" >>> {char.name} leads the way to the {target}...")
                    self._move_to(target)

        destination_location = self.current.name
        if destination_location != origin_location:
            location_msg = self.narrate("location_moved", origin=origin_location, destination=destination_location)
        else:
            location_msg = self.narrate("location_stayed", location=origin_location)
        self.history.append(location_msg)
        if self.interactive or self.verbose:
            print(f"  >>> Current location: {origin_location}")
            if destination_location != origin_location:
                print(f"  >>> Transferred to: {destination_location}")
            else:
                print(f"  >>> Staying in: {origin_location}")
        
        # 5. Resource & Inventory updates
        if action:
            if action.required_coins > 0:
                self.coins = max(0, self.coins - action.required_coins)
                self.update_power()
                coin_msg = self.narrate("paid", coins=action.required_coins, balance=self.coins)
                self.history.append(coin_msg)
                if self.interactive or self.verbose: print(f"  >>> {coin_msg}")
            
            if action.collects_object:
                self.inventory.add(action.collects_object)
                self.update_power()
                item_msg = self.narrate("item_collected", item=action.collects_object)
                self.history.append(item_msg)
                if self.interactive: print(f"  >>> {item_msg} (Inventory: {list(self.inventory)} | Power: {self.power:.2f})")
                    
            if action.consumes_object and action.active_item:
                if action.active_item in self.inventory:
                    self.inventory.remove(action.active_item)
                    self.update_power()
                    item_msg = self.narrate("item_used", item=action.active_item)
                    self.history.append(item_msg)
                    if self.interactive: print(f"  >>> {item_msg} (Remaining: {list(self.inventory)} | Power: {self.power:.2f})")

    def get_full_story(self):
        """Returns a string representation of the full story transcript."""
        return "\n".join(self.history)
