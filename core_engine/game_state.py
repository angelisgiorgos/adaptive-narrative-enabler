import copy
import hashlib
import heapq
import math
from collections import Counter
import random
import re
from utils.config_loader import config
from .actions import Action, Outcome
from .world import ItemInstance
from .health import HealthPlayer
from .stats import CharacterStats
from .combat import CombatSystem
from .narration import Narrator
from .story_beats import StoryBeats
from . import conditions, effects, rules, starts, variables as persistent
from utils.tag_similarity import semantic_tag_similarity

_action_keys = conditions.action_keys  # "(Priest) Seek a blessing" or "Seek a blessing"
_READS = {}


def _meets(value, comparison):
    """Whether a number meets a comparison mapping ({gte: 1, lt: 5, ...})."""
    checks = {"gte": lambda a, b: a >= b, "gt": lambda a, b: a > b, "lte": lambda a, b: a <= b,
              "lt": lambda a, b: a < b, "eq": lambda a, b: a == b, "ne": lambda a, b: a != b}
    return all(checks[op](value, bound) for op, bound in comparison.items() if op in checks)


def _condition_reads(value):
    """conditions.references of a goal's condition, read once per goal value."""
    cached = _READS.get(id(value))
    if cached is None or cached[0] is not value:
        if len(_READS) >= 256:
            _READS.clear()  # goal values of finished stories
        cached = (value, conditions.references(conditions.normalize(value)))
        _READS[id(value)] = cached
    return cached[1]

# ============================
# GAME STATE
# ============================

class GameState(StoryBeats):
    def __init__(self, world, genome, start=None, verbose=False, interactive=False, rng=None, mission=None,
                 variables=None, store=None, profile=None, stats=None, stats_mode=None, recent_starts=None):
        """``store`` (a persistent.VariableStore) marks real play: variable changes
        are saved to it, the start is added to the profile's start history and a
        finished story counts as a playthrough. Without it (simulations,
        evolution) nothing is ever written.

        ``start`` forces the starting location (any enabled location: tests,
        debugging); by default it is drawn among the start-eligible locations,
        discouraging the profile's ``recent_starts`` (default: its stored history)."""
        self.world = world
        self.genome = genome
        self.verbose = verbose
        self.interactive = interactive
        self.rng = rng or random
        if not world.locations:
            raise ValueError("The world must contain at least one location.")

        # Persistent variables, loaded before anything depends on them.
        self.variable_specs = persistent.definitions()
        self.profile = profile or config.get("persistence.profile", "default")
        self._store = store
        if variables is not None:
            self.variables = {**persistent.defaults(self.variable_specs), **variables}
        elif store is not None:
            self.variables = store.load(self.profile, self.variable_specs)
        elif config.get("persistence.simulate_with", "defaults") == "stored":
            self.variables = persistent.VariableStore().load(self.profile, self.variable_specs)  # read only
        else:
            self.variables = persistent.defaults(self.variable_specs)
        self.variables.setdefault("playthroughs", 0)
        self.facts = conditions.StoryFacts(self)  # what every condition reads
        self._variables_version = 0
        self._scene_dirty = False
        if start is not None and start not in world.locations:
            raise ValueError(f"Unknown starting location '{start}'.")
        # Placed at the start together with the mission choice, at the end of setup.
        self.current = None
        self.start_location = None

        # Health and power, bounded by max_health and power_model.max_power.
        self.health_player = HealthPlayer()
        # Character stats (``stats``: stat -> points, or a stats_mode) and the
        # combat system that resolves every skill check and fight.
        self.stats = CharacterStats(points=stats, mode=stats_mode, rng=self.rng)
        self.combat = CombatSystem(self.stats, world=world, rng=self.rng)
        self.actions_taken = 0

        self.tags_seen = []
        self.history = []
        self.turn_log = []  # one entry per turn taken (step): what was chosen and what came of it
        # How the map grew and was travelled: one record per move or discovery, with how it
        # was resolved (travel, named, tags, spawn, lead, back, sent, auto) - see fitness.objective_losses.
        self.transition_records = []
        self.scenario_counts = Counter()   # (place, action, outcome text) told: duplicates are a loss
        self.setups_planted = set()        # named setups (outcome setup_id)
        self.completed_payoffs = set()     # named setups paid off (outcome payoff_id)
        self.npc_goal_progress = {}        # NPC -> {goal index: times helped}
        self.allies = set()                # NPCs whose goal the player fulfilled
        # Player-directed navigation (navigation.player_directed): in real play a discovery
        # or a lead opens a path but never moves the player; they choose where to go.
        directed = str((config.get("navigation") or {}).get("player_directed", "play")).lower()
        self.player_directed = directed == "always" or (directed == "play" and store is not None)
        self.narrate = Narrator()  # customisable story text (config/messages.yaml)
        
        # Track location visits
        self.location_counts = {}
        self.path = [] # Sequence of locations
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
        self.item_log = []  # every item placed, collected, received, used or removed, held or not
        self.coins = config.get("game_state.initial_coins", 5)

        # Narrative Discovery State
        self.visible_inhabitants = set()
        self.visible_objects = set()
        self.ended = False
        self.end_reason = None
        self.end_kind = None  # death, mission, action_limit, stalled, dungeon

        # Repeatable vs one-time options (Action.repeat / exclusive_group).
        self.spent_actions = set()   # "once" actions used up (taken, or succeeded when they may be retried)
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
        # Set by effects (effects.py), resolved once the current action is done.
        self._pending_event = None      # (event, npc) of a start_event effect
        self._story_end = None          # closing text of an end_story effect
        self._mission_completed = False  # a complete_mission effect
        self._announced_visit = None   # visit whose arrival text is in the history
        self._printed_visit = None     # visit whose scene step() last printed
        self.npc_homes = self._assign_npc_homes()
        self.start_power = self.update_power()  # Power counts in fights from here (power_model.check_weight)
        self.item_instances = self._place_unique_items()  # unique items: one instance each
        self._scheduled_turn = None  # turn whose random events were already rolled
        self.update_power()

        # Mission (the win condition) and starting location, chosen at random
        # unless requested, so that the mission can be reached from the start.
        self.mission = None
        self.won = False
        self.succeeded_actions = set()
        self.succeeded_outcome_tags = set()
        if recent_starts is not None:
            self.recent_starts = [str(name) for name in recent_starts]
        elif store is not None:
            self.recent_starts = store.recent_starts(self.profile)
        elif config.get("persistence.simulate_with", "defaults") == "stored":
            self.recent_starts = persistent.VariableStore().recent_starts(self.profile)  # read only
        else:
            self.recent_starts = []
        # This story's own geography (world_definition.story_map), before the mission and
        # start are chosen: they must be reachable on it.
        self.story_map = self._draw_story_map()
        # The story's own fixed randomness for the director's choices (surprises): taken from
        # the state of its dice, without rolling them, so that skill checks roll as before.
        self._story_salt = hashlib.sha256(repr(self.rng.getstate()).encode()).hexdigest()[:16]
        self.routes = self._story_routes()  # the map every move follows (None: no map)
        self.heading = None  # a far place the story set out for (see _set_heading)
        if store is not None:
            self.recent_missions = store.recent_missions(self.profile)
        elif config.get("persistence.simulate_with", "defaults") == "stored":
            self.recent_missions = persistent.VariableStore().recent_missions(self.profile)  # read only
        else:
            self.recent_missions = []
        mission, start = self._choose_mission_and_start(mission, start)
        # Route planning caches filled by the choice are rebuilt on first use in play.
        for cache in ("_viability_cache", "_item_sites_cache", "_site_index", "_goal_sites_cache", "_route_cache"):
            self.__dict__.pop(cache, None)
        self.current = world.locations[start]
        self.start_location = start
        self.location_counts = {start: 1}
        self.path = [start]
        if self._store is not None:
            self._store.record_start(self.profile, start)
            if mission is not None:
                self._store.record_mission(self.profile, mission.name)
        self._start_mission(mission)
        # Who matters how much in this story (npc_roles): the quest's target is its nemesis.
        self.npc_roles = self._assign_roles()
        self.revealed_npcs = set()   # important NPCs brought into the story by a reveal or a lead
        self.defeated = []           # important NPCs beaten: [{npc, role, turn}]
        # The story's beats (story_beats.py): the engine's own beats, the alert level, turns of
        # fast travel (not counted toward the limit), successes per action, secrets told and
        # consequences still to show.
        self.beat_log = []
        self.alert = 0
        self.free_turns = 0
        self.success_counts = Counter()
        self.revealed_secrets = {}
        self.consequences_due = []
        self._recall_last_time()

    # ---- persistent variables -------------------------------------------------

    def is_enabled(self, entity):
        """Whether a location, NPC, item, event, action, dialogue line or mission is
        switched on by its ``enabled_if`` (one evaluator for every condition)."""
        condition = getattr(entity, "condition", None)
        return condition is None or condition.holds(self.facts)

    def holds(self, condition):
        """Whether a condition (any form, see conditions.py) holds now."""
        return conditions.holds(condition, self.facts)

    def _condition_epoch(self):
        """Changes whenever what is enabled may have changed: the persistent
        variables, plus the story's own state in worlds whose locations, NPCs or
        items depend on it (WorldGraph.dynamic_conditions)."""
        if not self.world.dynamic_conditions:
            return self._variables_version
        return (
            self._variables_version, frozenset(self.inventory), frozenset(self.location_counts),
            self.current.name if self.current else None, frozenset(self.removed_npcs),
            sum(1 for entry in self.event_log if "action" in entry), len(self.item_log), frozenset(self.succeeded_actions),
            frozenset(self.succeeded_outcome_tags), self.coins, self.health, self.actions_taken,
            tuple(sorted(self.stats.changes.items())), self.mission.name if self.mission else None,
            frozenset(self.__dict__.get("revealed_secrets", ())),
        )

    # ---- effects ----------------------------------------------------------------

    def _references(self):
        return conditions.References.from_graph(self.world, self.variable_specs, self.stats.names)

    def _checked_effects(self, effect_list):
        """The effect list, validated against this world unless that was done when
        it was built. Raises EffectError before anything changes."""
        if not isinstance(effect_list, effects.EffectList):
            effect_list = effects.EffectList(effect_list)  # raises EffectError on bad structure
        if not effect_list.checked:
            errors = effects.validate(effect_list, self._references())
            if errors:
                raise effects.EffectError("; ".join(errors))
            effect_list.checked = True
        return effect_list

    def apply_effects(self, effect_list, npc=None):
        """Apply an effect list (effects.py) as a whole, e.g. from game code.

        The list is validated first: a bad effect raises EffectError and changes
        nothing. Events and the end of the story it causes take effect at once.
        """
        self._commit_effects(self._checked_effects(effect_list), npc=npc)
        self._start_pending_event()
        self._finalize_if_ended()

    def _run_effects(self, effect_list, npc=None):
        """Apply effects during play (a mission's rewards), validated first; the
        events and endings they cause wait until the current action is resolved.
        Outcomes and dialogue lines run through apply(), which validates first too."""
        if effect_list:
            self._commit_effects(self._checked_effects(effect_list), npc=npc)

    def _commit_effects(self, effect_list, npc=None):
        variables_changed = False
        for kind, argument in effect_list:
            if kind in ("set_variable", "change_variable"):
                for name, value in argument:
                    if kind == "change_variable":
                        value = self.variables.get(name, 0) + value
                    variables_changed |= self._write_variable(name, value)
            elif kind in ("set_stat", "change_stat"):
                for name, value in argument:
                    self._change_stat(name, value - self.stats.base_value(name) if kind == "set_stat" else value)
            elif kind == "give_item":
                for name in argument:
                    self._give_item(name)
            elif kind == "take_item":
                for name in argument:
                    self._take_item(name)
            elif kind == "change_coins":
                self.coins = max(0, self.coins + argument)  # a purse cannot go below empty
                self.update_power()
            elif kind == "change_health":
                self._change_health(argument)
            elif kind == "remove_npc":
                name = npc if argument is True else argument
                if isinstance(name, str):
                    self._remove_npc(name)
            elif kind == "reveal_npc":
                self._reveal_npc(argument)
            elif kind == "reveal_item":
                self._reveal_item(argument)
            elif kind == "reveal_location":
                self._open_route(argument)
            elif kind == "unlock_location":
                self._unlock(argument)
            elif kind == "move_to":
                if self._move_to(argument):
                    self.consecutive_steps_in_location = 0
            elif kind == "send_to":
                if self._move_to(argument, one_way=True):
                    self._say("sent", location=argument)
            elif kind == "start_event":
                self._pending_event = (argument, npc)
            elif kind == "complete_mission":
                if self.mission is not None and argument in (True, self.mission.name):
                    self._mission_completed = True
            elif kind == "end_story":
                self._story_end = argument
        if variables_changed and self._store is not None and config.get("persistence.save_on", "change") == "change":
            self._store.save(self.profile, self.variables)

    def _say(self, key, **values):
        message = self.narrate(key, **values)
        if message:
            self.history.append(message)
            if self.interactive or self.verbose:
                print(f"  >>> {message}")
        return message

    def _write_variable(self, name, value):
        """Set a persistent variable (clamped); True if it changed. Saved by the caller."""
        if name not in self.variable_specs:
            return False
        old = self.variables.get(name)
        new = persistent.clamp(name, value, self.variable_specs)
        if new == old:
            return False
        self.variables[name] = new
        self._variables_version += 1
        self._scene_dirty = True  # NPCs, items or routes may have switched on or off
        message = self.narrate("variable_changed", name=name, old=old, new=new)
        if message:
            self.history.append(message)
        return True

    def _change_stat(self, name, delta):
        if name not in self.stats.names:
            return
        old, new = self.stats.change(name, delta)
        if new != old:
            self.history.append(self.narrate("stat_changed", stat=name, old=old, new=new))
            self.update_power()

    def _change_health(self, delta, mitigate=False):
        if delta < 0:
            delta = -self._scaled_damage(-delta)
        if mitigate:
            delta = self.combat.mitigate(delta, self.inventory)
        applied = self.health_player.change(delta)
        if delta > 0 and applied < delta:
            self.history.append(self.narrate(
                "health_capped", health=self.health_player.health, max_health=self.health_player.max_health,
            ))
        self.update_power()

    def _item_obtainable(self, name):
        """Whether the player can come to hold the item: always for a generic item;
        for a unique one, only while its instance is not held or gone."""
        instance = self.item_instances.get(name)
        return instance is None or instance.available

    def _give_item(self, name, how="received"):
        """The player gets the item (``how``: collected by an action, or received)."""
        if name in self.inventory or not self._item_obtainable(name):
            return  # a unique item never gets a second copy
        instance = self.item_instances.get(name)
        if instance is not None:
            instance.state, instance.location = "held", None
            self._scene_dirty = True  # it is no longer lying anywhere
        self.inventory.add(name)
        self._log_item(name, how, self.current.name if self.current else None)
        self.update_power()
        message = self.narrate("item_collected", item=name)
        self.history.append(message)
        if self.interactive:
            print(f"  >>> {message} (Inventory: {list(self.inventory)} | Power: {self.power:.2f})")

    def _take_item(self, name, how="removed"):
        """The item leaves the inventory (``how``: used by an action, or removed). A
        unique item is then gone for good; a generic one can be found again."""
        if name not in self.inventory:
            return
        self.inventory.remove(name)
        instance = self.item_instances.get(name)
        if instance is not None:
            instance.state = "gone"
        self._log_item(name, how, self.current.name if self.current else None)
        self.update_power()
        message = self.narrate("item_used", item=name)
        self.history.append(message)
        if self.interactive:
            print(f"  >>> {message} (Remaining: {list(self.inventory)} | Power: {self.power:.2f})")

    def _reveal_npc(self, name):
        revealed = next((c for c in self.world.characters if c.name == name), None)
        if revealed is not None:
            self.__dict__.setdefault("revealed_npcs", set()).add(revealed.name)
        if revealed and self._npc_can_appear_here(revealed, explicit=True):
            already = revealed.name in self.visible_inhabitants and revealed in self.current_inhabitants
            self.visible_inhabitants.add(revealed.name)
            if revealed not in self.current_inhabitants:
                self.current_inhabitants.append(revealed)
            if not already:  # someone already in view is not "noticed" again
                self._say("reveal_npc", npc=name)
            self._note_met(revealed.name)

    def _meet(self, char_name, char_desc):
        """Meet someone on arrival; finding someone who matters (importance >= 2) is a moment of its own."""
        role, settings = self.role_of(char_name)
        first = char_name not in self.__dict__.get("met", {})
        if int(settings.get("importance", 0)) >= 2 and first:
            self._say("npc_reveal", npc=self._called(char_name)[:1].upper() + self._called(char_name)[1:],
                      role=settings.get("label", role), description=char_desc)
            self.tags_seen.append(["discovery", "milestone"])
            self._beat("reveal", npc=char_name, via="met")
        else:
            self.history.append(self.narrate("arrival_npc", npc=self._called(char_name), description=char_desc))
        lead = f"setup_lead_{char_name.lower().replace(' ', '_')}"
        if first and lead in self.setup_indices and self.setup_indices[lead] < self.actions_taken:
            self._record_clues([lead.replace("setup_", "payoff_", 1)])  # the one a lead named, found
        if first:
            self._recognised(char_name)
        self._note_met(char_name)

    def _note_met(self, name):
        """Remember where the player met someone (people_met)."""
        met = self.__dict__.setdefault("met", {})  # games saved before the journal
        entry = met.setdefault(name, {"where": [], "first_turn": self.actions_taken})
        if self.current.name not in entry["where"]:
            entry["where"].append(self.current.name)

    def _reveal_item(self, name):
        if name in self.item_instances and not self._instance_at(name, self.current.name):
            return  # the one copy of a unique item lies elsewhere (or is held or gone)
        if name in self.visible_objects or name in self.inventory:
            return  # already in view (or held): not "spotted" again
        self.visible_objects.add(name)
        self._say("reveal_item", item=name)

    def _open_route(self, name):
        """Open a route from the current location to ``name`` (or toward it, on the map);
        the new location, or None."""
        name = self._lead_toward(name)
        if name is None:
            return None
        new_loc = self.world.spawn(self.current, force_target=name, state=self, allow_special=(name == "Dungeon"))
        if new_loc and new_loc.name != self.current.name:
            self.current.connect(new_loc)
            self._say("spawn", location=new_loc.name)
            return new_loc
        return None

    def _unlock(self, name):
        """A confined player may leave ``name`` (as after an escape)."""
        self.unlocked_locations.add(name)
        if self.confined_in == name:
            self.confined_in = None
            self._say("escape", location=name)

    def _start_pending_event(self):
        pending, self._pending_event = self._pending_event, None
        if pending and not self.active_event and not self.ended and self.health_player.is_alive:
            if self._event_available(pending[0]):
                self._enter_event(*pending, cause="effect")

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

    # ---- what the player sees of the quest, the people here and the ways on ------------------

    def _goal_words(self, key, value):
        """One goal condition in words, for the player."""
        if key == "location":
            return f"Reach the {value}"
        if key == "item":
            called = self._called(value)
            return f"Get {'the ' + value if called.startswith(('a ', 'an ', 'some ')) else called}"
        if key == "coins":
            return f"Have {value} coins (you have {self.coins})"
        if key in ("action", "event_succeeded"):
            return str(value)
        if key == "event":
            return f"Live through: {value}"
        if key == "npc_removed":
            return f"Be rid of {self._called(value)}"
        if key == "outcome_tag":
            return f"Bring about: {value}"
        if key == "variables" and isinstance(value, dict):
            return "; ".join(f"{name.replace('_', ' ')} {rule}" for name, rule in value.items())
        return f"{key.replace('_', ' ')}: {value}"

    def quest_log(self):
        """The story's mission as the player follows it, or None: {key, title, description,
        status, stages: [{title, description, state: done|current|todo}], todo: what the
        current step still needs, in words}."""
        if not self.mission:
            return None
        index = getattr(self, "mission_stage", 0)
        stages = [{
            "title": stage["title"], "description": stage.get("description") or "",
            "state": "done" if self.won or number < index else "current" if number == index else "todo",
        } for number, stage in enumerate(getattr(self.mission, "stages", None) or [])]
        return {
            "key": self.mission.name, "title": self.mission.title, "description": self.mission.description or "",
            "status": "completed" if self.won else "failed" if self.ended else "in progress",
            "stages": stages,
            "todo": [self._goal_words(key, value) for key, value in self._pending_goal().items()],
        }

    def people_here(self):
        """The people the player sees here: [{name, called, description, wants, hostile,
        talks (times spoken with in this story), quest (they can help with the mission now)}]."""
        if "location_objects" not in self.__dict__:
            return []  # the scene is set with the first options
        _, npc_lines, _ = self._describe_visible_scene()
        people = []
        for name, description in npc_lines:
            char = next((c for c in self.world.characters if c.name == name), None)
            people.append({
                "name": name,
                "called": self._called(name),
                "description": description,
                "wants": char.goals[0] if char is not None and char.goals else None,
                "hostile": bool(char is not None and char.encounter_event),
                "talks": int(self.character_interactions.get(name, 0)),
                "quest": bool(char is not None and self.mission and not self.won and any(
                    self._mission_affinity(a, npc=name) > 0 for a in char.actions if self._action_enabled(a))),
                # What helping them takes: times helped with their goal so far, and whether
                # they are an ally now (npc_goals).
                "helped": sum(self.__dict__.get("npc_goal_progress", {}).get(name, {}).values()),
                "ally": name in self.__dict__.get("allies", set()),
                # How much they matter in this story (npc_roles): a role and its importance 0-3.
                "role": self.role_of(name)[0],
                "importance": self.importance(name),
            })
        return people

    def people_met(self):
        """Everyone the player has met in this story, in order: [{name, called, where (the
        places met), talks, hostile, gone (left the story)}]."""
        people = []
        for name, entry in sorted(self.__dict__.get("met", {}).items(), key=lambda item: item[1]["first_turn"]):
            char = next((c for c in self.world.characters if c.name == name), None)
            people.append({
                "name": name, "called": self._called(name), "where": list(entry["where"]),
                "talks": int(self.character_interactions.get(name, 0)),
                "hostile": bool(char is not None and char.encounter_event),
                "gone": name in self.removed_npcs,
                "ally": name in self.__dict__.get("allies", set()),
                "role": self.role_of(name)[0],
                "importance": self.importance(name),
            })
        return people

    def map_view(self):
        """The map as the player knows it: {current, previous, places: [{name, visited,
        locked}], paths: [[a, b], ...] (each path once, both ways open unless one_way),
        one_way: [[from, to], ...]}. Places known are those visited and those a known path
        leads to; it is kept with the game (saved games keep it)."""
        visited = [name for name in self.world.locations if self.location_counts.get(name, 0) > 0]
        known = set(visited)
        for name in visited:
            known |= {other for other in self.world.locations[name].connected if other in self.world.locations}
        paths, one_way = set(), set()
        for name in sorted(known):
            for other in self.world.locations[name].connected:
                if other not in known:
                    continue
                if name in self.world.locations[other].connected:
                    paths.add(tuple(sorted((name, other))))
                else:
                    one_way.add((name, other))
        return {
            "current": self.current.name,
            "previous": self.path[-2] if len(self.path) > 1 else None,
            "places": [{"name": name, "visited": self.location_counts.get(name, 0) > 0,
                        "locked": self._location_is_locked(name)} for name in sorted(known)],
            "paths": [list(pair) for pair in sorted(paths)],
            "one_way": [list(pair) for pair in sorted(one_way)],
        }

    def ways_on(self):
        """The places the player can travel to from here now: [{name, visited, toward_quest}]
        (empty for a prisoner)."""
        if self._location_is_locked():
            return []
        distances = self._goal_distances() if self.mission and not self.won else {}
        here = distances.get(self.current.name, float("inf"))
        return [{
            "name": name,
            "visited": self.location_counts.get(name, 0) > 0,
            "toward_quest": distances.get(name, float("inf")) < here,
        } for name in sorted(self.current.connected)
            if name in self.world.locations and self.is_enabled(self.world.locations[name])
            and self._adjacent(self.current.name, name) and not self.sent_only(name)]

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
        stages = getattr(self.mission, "stages", None)
        step = ""
        if stages:
            index = getattr(self, "mission_stage", 0)
            step = f" (stage {index + 1}/{len(stages)}: {stages[index]['title']})"
        return f"{self.mission.title}{step}: {'failed' if self.ended else 'in progress'}"

    # ---- mission and starting location ------------------------------------------

    def _start_candidates(self):
        """Locations the story may start at: the start-eligible, enabled ones.

        A world in which no location is ``start_eligible`` has a single start,
        ``start_selection.default_location`` or else the first enabled location.
        """
        eligible = self.world.start_locations()
        if eligible:
            enabled = [loc for loc in eligible if self.is_enabled(loc)]
            if not enabled:
                raise ValueError(
                    "No starting location is available: every location with `start_eligible` "
                    f"({', '.join(loc.name for loc in eligible)}) is switched off by its enabled_if."
                )
            return enabled
        default = self.world.locations.get(starts.settings()["default_location"])
        if default is None or not self.is_enabled(default):
            default = next((loc for loc in self.world.locations.values() if self.is_enabled(loc)),
                           default or next(iter(self.world.locations.values())))
        return [default]

    def _mission_viable_from(self, mission, start):
        """Whether a story starting at ``start`` can work toward ``mission``: its
        goal does not already hold there, and a known or discoverable route leads
        from there to a place where the goal can be advanced."""
        key = (mission.name, start, self._condition_epoch())
        cache = self.__dict__.setdefault("_viability_cache", {})
        if key in cache:
            return cache[key]
        saved = self.mission, self.location_counts
        saved_stage = getattr(self, "mission_stage", 0)
        self.mission, self.location_counts, self.mission_stage = mission, {start: 1}, 0
        try:
            if self._goal_met(mission):
                viable = False
            elif set(self._pending_goal()) <= {"coins"}:
                viable = True  # coins can be earned almost anywhere
            else:
                viable = start in self._route_distances(frozenset(self._goal_sites()))
        finally:
            self.mission, self.location_counts = saved
            self.mission_stage = saved_stage
        cache[key] = viable
        return viable

    def _choose_mission_and_start(self, requested_mission=None, requested_start=None):
        """(mission or None, start location name) of a new story.

        The mission is drawn first (by weight, among the enabled missions that
        some candidate start can reach), then the start among the candidates
        from which that mission is viable (starts.draw: ``start_weight``, recent
        starts of the profile discouraged). A requested start is used as is
        unless persistent variables switch it off.
        """
        candidates = self._start_candidates()
        if requested_start is not None and self.is_enabled(self.world.locations[requested_start]):
            candidates = [self.world.locations[requested_start]]

        mission = None
        if config.get("mission_settings.enabled", True) and self.world.missions:
            if requested_mission is not None:
                if requested_mission not in self.world.missions:
                    raise ValueError(f"Unknown mission '{requested_mission}'.")
                mission = self.world.missions[requested_mission]
            else:
                enabled = [m for m in self.world.missions.values() if self.is_enabled(m)]
                pool = [m for m in enabled if any(self._mission_viable_from(m, loc.name) for loc in candidates)]
                if not pool:
                    # No route the planner can see: keep any goal that does not already hold.
                    pool = [m for m in enabled if any(not self._goal_met_at(m, loc.name) for loc in candidates)]
                if pool:
                    mission = self.rng.choices(pool, weights=self._mission_weights(pool), k=1)[0]

        if mission is not None and len(candidates) > 1:
            candidates = (
                [loc for loc in candidates if self._mission_viable_from(mission, loc.name)]
                or [loc for loc in candidates if not self._goal_met_at(mission, loc.name)]
                or candidates
            )
        return mission, starts.draw(candidates, self.recent_starts, self.rng).name

    # ---- the story's own geography ------------------------------------------------------

    def _draw_story_map(self):
        """Which places can open onto which in this story: every place keeps
        world_definition.story_map.neighbors of the places its spawn rules allow, drawn per
        story (more fitting places more often), joined into one connected map. None: the
        world's spawn rules as they are (story_map.enabled false)."""
        cfg = config.get("world_definition.story_map") or {}
        if not cfg.get("enabled", False):
            return None
        neighbors = max(1, int(cfg.get("neighbors", 3)))
        rng = random.Random()
        rng.setstate(self.rng.getstate())  # seeded by the story, without changing its dice
        names = sorted(self.world.locations)
        tags = {name: self.world.locations[name].tags for name in names}
        story_map = {}
        for name in names:
            options = sorted(o for o in set(self.world.spawn_rules.get(name, [])) if o in tags and o != name)
            weights = {o: 0.05 + max(0.0, semantic_tag_similarity(tags[name], tags[o])) ** 2 for o in options}
            chosen = []
            while options and len(chosen) < neighbors:
                pick = rng.choices(options, weights=[weights[o] for o in options], k=1)[0]
                chosen.append(pick)
                options.remove(pick)
            story_map[name] = chosen
        # One map: join every part that cannot reach the rest by its most fitting rule.
        def component(start):
            seen, todo = {start}, [start]
            while todo:
                here = todo.pop()
                for other in set(story_map.get(here, [])) | {n for n in names if here in story_map.get(n, [])}:
                    if other not in seen:
                        seen.add(other)
                        todo.append(other)
            return seen
        main = component(names[0]) if names else set()
        while len(main) < len(names):
            links = [(semantic_tag_similarity(tags[a], tags[b]), a, b) for a in sorted(main) for b in names
                     if b not in main and (b in self.world.spawn_rules.get(a, []) or a in self.world.spawn_rules.get(b, []))]
            if not links:
                break  # the spawn rules themselves leave this part apart
            _, a, b = max(links)
            if b in self.world.spawn_rules.get(a, []):
                story_map[a].append(b)
            else:
                story_map[b].append(a)
            main |= component(b)
        return story_map

    def spawn_options(self, name):
        """The places ``name`` can open onto in this story."""
        story_map = getattr(self, "story_map", None)  # games saved before story maps
        if story_map is not None:
            return list(story_map.get(name, []))
        return list(self.world.spawn_rules.get(name, []))

    # ---- the map: which places border which ----------------------------------------------

    def _story_routes(self):
        """The map every move follows: the world's authored ``routes``, or else this
        story's map (story_map, both ways) with the places a location's own actions
        lead to or reveal. None (no routes, story maps off): any move goes, as in older worlds."""
        authored = getattr(self.world, "routes", None)
        if authored is not None:
            return {name: set(others) for name, others in authored.items()}
        story_map = getattr(self, "story_map", None)
        if story_map is None or not any(self.world.spawn_rules.get(name) for name in self.world.locations):
            return None  # no geography to draw from: the places a world connects itself are all there is
        routes = {name: set() for name in self.world.locations}

        def link(a, b):
            if a != b and a in routes and b in routes:
                routes[a].add(b)
                routes[b].add(a)

        for name, others in story_map.items():
            for other in others:
                link(name, other)
        for name, loc in self.world.locations.items():
            for action in loc.actions:
                for outcome in action.outcomes:
                    for target in (outcome.move_to, outcome.reveal_location):
                        if target:
                            link(name, target)
        return routes

    def neighbours(self, name):
        """The places ``name`` borders on the story's map (and the ways already known from
        it, e.g. those a world connects itself); None for a story without a map."""
        routes = getattr(self, "routes", None)  # games saved before maps
        if routes is None:
            return None
        location = self.world.locations.get(name)
        return set(routes.get(name, set())) | (set(location.connected) if location is not None else set())

    def _adjacent(self, origin, target):
        neighbours = self.neighbours(origin)
        return neighbours is None or target in neighbours

    @staticmethod
    def sent_only(name):
        """A place one only arrives at by being sent there (``location_constraints.<place>.entry:
        sent``, e.g. an arrest's cell): never a travel option, a discovery or a stop on the way."""
        return (config.get("location_constraints", {}).get(name) or {}).get("entry") == "sent"

    def _route_path(self, origin, target):
        """The shortest way on the map from ``origin`` to ``target``: the places after the
        origin, the target last. It passes only through enabled places one may walk
        into and out of. None when no way leads there on authored routes; on a drawn map
        a place it cannot reach at all is reached directly."""
        if target == origin:
            return []
        if self.neighbours(origin) is None:
            return [target]
        if target not in self.world.locations or self.sent_only(target):
            return None
        constraints = config.get("location_constraints", {})
        previous = {origin: None}
        frontier = [origin]
        while frontier:
            following = []
            for here in frontier:
                for step in sorted(self.neighbours(here)):
                    if step in previous or step not in self.world.locations:
                        continue
                    if not self.is_enabled(self.world.locations[step]):
                        continue
                    if step != target and (self.sent_only(step) or (constraints.get(step) or {}).get("requires_escape")
                                           or getattr(self.world.locations[step], "exit_locked", False)):
                        continue  # no one walks through a cell
                    previous[step] = here
                    if step == target:
                        path = [step]
                        while previous[path[-1]] != origin:
                            path.append(previous[path[-1]])
                        return path[::-1]
                    following.append(step)
            frontier = following
        # A drawn map (no authored routes) with no way there at all: the world's own words
        # (a discovery or a lead naming the place) open one.
        return [target] if getattr(self.world, "routes", None) is None else None

    def _way_toward(self, target):
        """Where a move or a lead toward ``target`` goes now: the target when it borders
        the current place, else the next place on the way there; None when no way leads there."""
        if target == self.current.name:
            return target
        path = self._route_path(self.current.name, target)
        return path[0] if path else None

    def _set_heading(self, target):
        """Remember a far destination the story set out for (a move or a lead that had to
        go through other places first); its next step is favoured until it is reached."""
        turns = int(config.get("action_selection.heading_turns", 8))
        self.heading = target
        self._heading_until = self.actions_taken + turns

    def _learn_way(self, path):
        """The way along ``path`` (places after the current one) becomes known beyond its
        first step: one who knows the way to a far place can follow it."""
        for here, there in zip(path, path[1:]):
            self.world.locations[here].connect(self.world.locations[there])

    def _lead_toward(self, target):
        """A discovery or a lead naming ``target``: the place it opens now. That is the
        target when it borders the current place, else the next place on the way there
        (told once as a lead; the way becomes known, and the story heads on toward the
        target); None when no way leads there."""
        if not target or target not in self.world.locations:
            return target
        path = self._route_path(self.current.name, target)
        if not path:
            return None
        if len(path) == 1:
            return target
        told = self.__dict__.setdefault("_leads_told", set())  # games saved before
        if (target, path[0]) not in told:
            told.add((target, path[0]))
            self._say("lead_via", destination=target, via=path[0])
        self._learn_way(path)
        self._set_heading(target)
        return path[0]

    def _heading_step(self):
        """The next place toward the destination the story set out for, if any."""
        target = getattr(self, "heading", None)
        if not target or self.actions_taken > getattr(self, "_heading_until", 0) or target == self.current.name:
            return None
        return self._way_toward(target)

    def _mission_weights(self, pool):
        """Draw weights of missions: their ``weight``, x mission_selection.repeat_factor for
        each time the profile played it among its ``recent_missions`` latest stories."""
        cfg = config.get("mission_selection") or {}
        window = max(0, int(cfg.get("recent_missions", 3)))
        factor = float(cfg.get("repeat_factor", 0.3))
        recent = list(getattr(self, "recent_missions", []))[-window:] if window else []
        return [max(0.0, m.weight) * factor ** recent.count(m.name) for m in pool]

    def _goal_met_at(self, mission, start):
        saved, self.location_counts = self.location_counts, {start: 1}
        try:
            return self._goal_met(mission)
        finally:
            self.location_counts = saved

    def _start_mission(self, mission):
        """Announce the story's mission (chosen by _choose_mission_and_start)."""
        self.mission = mission
        self.mission_stage = 0
        if mission is None:
            return
        if self.mission.description:
            message = self.narrate("mission_start", title=self.mission.title, description=self.mission.description)
        else:
            message = self.narrate("mission_start_brief", title=self.mission.title)
        self.history.append(message)
        if self.interactive or self.verbose:
            print(f"  >>> {message}")

    def _goal_condition_met(self, key, value):
        """Whether one goal condition holds (goal keys are condition shorthand)."""
        return self.holds(conditions.goal_condition(key, value))

    def _stage_goal(self, mission=None):
        """(goal, goal conditions) the story works toward now: the current stage of a
        staged mission (the first one for a mission not under way), or the goal."""
        mission = mission or self.mission
        stages = getattr(mission, "stages", None)
        if stages:
            index = getattr(self, "mission_stage", 0) if mission is self.mission else 0
            stage = stages[min(index, len(stages) - 1)]
            return stage["goal"], stage["goal_conditions"]
        return mission.goal, mission.goal_conditions

    def _goal_met(self, mission):
        return all(condition.holds(self.facts) for condition in self._stage_goal(mission)[1].values())

    def _pending_goal(self):
        if not self.mission or self.won:
            return {}
        goal, goal_conditions = self._stage_goal()
        return {key: value for key, value in goal.items() if not goal_conditions[key].holds(self.facts)}

    def _advance_stages(self):
        """Complete the stages whose goal holds, in order, up to the last one (which
        completes the mission): each is announced with the next, and its rewards apply."""
        stages = getattr(self.mission, "stages", None) if self.mission else None
        while stages and getattr(self, "mission_stage", 0) < len(stages) - 1 and self._goal_met(self.mission):
            done = stages[self.mission_stage]
            self.mission_stage += 1
            self.last_quest_progress = self.actions_taken
            following = stages[self.mission_stage]
            self._run_effects(done["rewards"])
            message = self.narrate("stage_complete", done=done["title"], next=following["title"],
                                   description=following["description"])
            self.history.append(message)
            self.tags_seen.append(["milestone"])
            if self.interactive or self.verbose:
                print(f"  >>> {message}")

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
        # Goal keys are condition shorthand; the common ones are checked directly (hot path).
        if key == "location":
            return any(
                value in (o.move_to, o.reveal_location, o.send_to) or ("location", value) in o.effects.writes
                for o in outcomes
            )
        if key == "item":
            return action.collects_object == value or any(
                o.reveal_object == value or ("item", value) in o.effects.writes for o in outcomes
            )
        if key in ("event", "event_succeeded"):
            return action.triggers_event == value or any(("event", value) in o.effects.writes for o in outcomes)
        if key == "npc_removed":
            return any(
                o.remove_npc == value or (o.remove_npc is True and npc == value)
                or ("npc", value) in o.effects.writes or (npc == value and ("npc", True) in o.effects.writes)
                for o in outcomes
            )
        if key == "outcome_tag":
            return any(str(value).lower() in {t.lower() for t in o.tags} for o in outcomes)
        if key == "coins":
            return any(o.coin_change > 0 or ("coins_gain", None) in o.effects.writes for o in outcomes)
        if key == "variables" and isinstance(value, dict):
            return any(GameState._moves_variable(o, name, wanted) for o in outcomes for name, wanted in value.items())
        if key not in ("variables", "condition"):
            return False
        reads = _condition_reads(value)
        return any(
            (kind in ("has_item", "item_collected") and action.collects_object == name)
            or (kind in ("event_done", "event_succeeded", "event_failed") and action.triggers_event == name)
            or (kind == "action_done" and bool(_action_keys(name) & _action_keys(action.name)))
            for kind, name in reads
        ) or any(GameState._touches(o, npc, reads) for o in outcomes)

    @staticmethod
    def _moves_variable(outcome, name, wanted):
        """Whether an outcome moves variable ``name`` toward ``wanted`` (a value, or a
        comparison such as {gte: 1}): raising it for gte/gt, lowering it for lte/lt."""
        direction, target = 0, wanted
        if isinstance(wanted, dict):
            if wanted.keys() & {"gte", "gt"}:
                direction = 1
            elif wanted.keys() & {"lte", "lt"}:
                direction = -1
            else:
                target = wanted.get("eq", wanted.get("value"))
        for kind, pairs in outcome.effects.items:
            if kind not in ("change_variable", "set_variable"):
                continue
            for variable, amount in pairs:
                if variable != name:
                    continue
                if kind == "change_variable" and direction and isinstance(amount, (int, float)) and amount * direction > 0:
                    return True
                if kind == "set_variable" and (amount == target if not direction else
                                               isinstance(amount, (int, float)) and _meets(amount, wanted)):
                    return True
        return False

    @staticmethod
    def _touches(outcome, npc, reads):
        """Whether an outcome changes something a condition reads (``reads``:
        conditions.references), through its fields or its effects."""
        written = outcome.effects.writes
        for kind, name in reads:
            if kind in ("variable", "stat") and (kind, name) in written:
                return True
            if kind == "resource" and (
                ("resource", name) in written
                or (name == "coins" and outcome.coin_change) or (name == "health" and outcome.health_change)
            ):
                return True
            if kind in ("has_item", "item_collected") and (outcome.reveal_object == name or ("item", name) in written):
                return True
            if kind in ("visited", "at_location") and (
                name in (outcome.move_to, outcome.reveal_location, outcome.send_to) or ("location", name) in written
            ):
                return True
            if kind == "npc_removed" and (
                outcome.remove_npc == name or (outcome.remove_npc is True and npc == name)
                or ("npc", name) in written or (npc == name and ("npc", True) in written)
            ):
                return True
            if kind in ("event_done", "event_succeeded", "event_failed") and ("event", name) in written:
                return True
            if kind == "outcome_tag" and str(name).lower() in {t.lower() for t in outcome.tags}:
                return True
        return False

    def _npc_sites(self, npc_name):
        char = next((c for c in self.world.characters if c.name == npc_name), None)
        if char is None or (char.unique and char.name in self.removed_npcs) or not self.is_enabled(char):
            return set()
        if char.unique:
            return {self.npc_homes[char.name]} if char.name in self.npc_homes else set()
        return {name for name, loc in self.world.locations.items() if self._npc_fits_location(char, loc)}

    def _item_sites(self, item_name):
        """Locations where the item is on show when the player arrives (the same
        ranking a scene uses; a unique item only where its instance lies)."""
        cache = self.__dict__.setdefault("_item_sites_cache", {})
        epoch = self._condition_epoch()
        if (item_name, epoch) in cache:
            return cache[(item_name, epoch)]
        obj = next((o for o in self.world.objects if o.name == item_name), None)
        sites = set()
        if obj is not None and self.is_enabled(obj):
            instance = self.item_instances.get(item_name)
            if instance is not None:
                sites = {instance.location} if instance.state == "placed" else set()
            else:
                sites = {name for name, loc in self.world.locations.items() if item_name in self._scene_objects(loc)[1]}
        cache[(item_name, epoch)] = sites
        return sites

    def _scene_objects(self, location):
        """(the items that belong in a scene at ``location``, the names on show).

        Generic items fit by their tags, and the best ranked are on show. A unique
        item belongs only where its instance lies, and is always on show there.
        """
        enabled = [obj for obj in self.world.objects if self.is_enabled(obj)]
        unique = [obj for obj in enabled if obj.unique and self._instance_at(obj.name, location.name)]
        generic = sorted(
            (obj for obj in enabled if not obj.unique and obj.associated_tags & location.tags),
            key=lambda obj: (self._score_object_visibility(obj, location), obj.name),
            reverse=True,
        )
        shown = {obj.name for obj in unique} | {obj.name for obj in generic[:config.get("scene.max_visible_objects", 2)]}
        return unique + generic, shown

    def _instance_at(self, item_name, location_name):
        instance = self.item_instances.get(item_name)
        return instance is not None and instance.state == "placed" and instance.location == location_name

    def _place_unique_items(self):
        """One ItemInstance per unique item, placed at its ``location`` or else at the
        location it fits best (shared tags, then tag similarity), so the same
        content always places it in the same spot."""
        instances = {}
        for obj in self.world.objects:
            if not obj.unique:
                continue
            place = obj.location if obj.location in self.world.locations else None
            if place is None:
                fitting = [loc for loc in self.world.locations.values() if obj.associated_tags & loc.tags]
                if fitting:
                    place = max(fitting, key=lambda loc: (
                        len(obj.associated_tags & loc.tags), semantic_tag_similarity(obj.associated_tags, loc.tags), loc.name,
                    )).name
            instances[obj.name] = ItemInstance(obj.name, place)
            if place:
                self._log_item(obj.name, "placed", place)
        return instances

    def _log_item(self, item_name, what, location=None):
        self.item_log.append({"item": item_name, "what": what, "location": location, "step": self.actions_taken})

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
        if self._is_closed(action) or not self._action_enabled(action):
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
            len(self.spent_actions), len(self.closed_groups), self._condition_epoch(),
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
            elif key in ("event", "event_succeeded"):
                for char in self.world.characters:
                    if char.encounter_event == value:
                        sites |= self._npc_sites(char.name)
            for action, action_sites, npc, _ in self._action_site_index():
                # A one-time option already used (for unique owners) is no longer a way to the goal.
                if action.source != "npc" and self._is_closed(action):
                    continue
                if key != "location" and self._advances(action, key, value, npc):
                    sites |= self._resolve_sites(action_sites)
        sites = {name for name in sites if name in self.world.locations and self.is_enabled(self.world.locations[name])}
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
        key = (sites, edge_count, frozenset(self.unlocked_locations), self.confined_in, self.current.name,
               exhausted, self._condition_epoch())
        cached = getattr(self, "_route_cache", None)
        if cached and cached[0] == key:
            return cached[1]

        distances = self._route_distances(sites)
        if exhausted:
            exits = [
                distances[name] + 1.0 for name in self.current.connected
                if name in distances and not self._location_is_locked(self.current.name)
            ]
            distances[self.current.name] = min(exits, default=float("inf"))
        self._route_cache = (key, distances)
        return distances

    def _route_distances(self, sites):
        """Steps from each location that can reach one of ``sites`` (backward
        Dijkstra over known routes and routes still to be discovered)."""
        discovery_cost = float(config.get("mission_settings.discovery_route_cost", 2))
        predecessors = {name: [] for name in self.world.locations}
        if getattr(self, "routes", None) is not None:
            # On a map: a known path costs one step, a route still to be found the
            # discovery cost; nobody walks into a place one is only sent to.
            for name, loc in self.world.locations.items():
                if self._location_is_locked(name) or not self.is_enabled(loc):
                    continue
                for neighbour in self.neighbours(name):
                    if neighbour not in predecessors or not self.is_enabled(self.world.locations[neighbour]):
                        continue
                    if self.sent_only(neighbour) and neighbour not in sites:
                        continue
                    cost = 1.0 if neighbour in loc.connected else discovery_cost
                    predecessors[neighbour].append((name, cost))
            return self._dijkstra(sites, predecessors)
        for name, loc in self.world.locations.items():
            if self._location_is_locked(name) or not self.is_enabled(loc):
                continue
            for neighbour in loc.connected:
                if neighbour in predecessors and self.is_enabled(self.world.locations[neighbour]):
                    predecessors[neighbour].append((name, 1.0))
            # Routes the world's own actions open: a place's action that moves the player
            # there (one step) or reveals it (a discovery).
            for action in loc.actions:
                for outcome in action.outcomes:
                    for target, cost in ((outcome.move_to, 1.0), (outcome.reveal_location, discovery_cost)):
                        if target in predecessors and target != name and self.is_enabled(self.world.locations[target]):
                            predecessors[target].append((name, cost))
            if not self._can_discover_from(name):
                continue
            for child in self.spawn_options(name):
                if child in predecessors and child not in loc.connected and child != "Dungeon":
                    predecessors[child].append((name, discovery_cost))

        return self._dijkstra(sites, predecessors)

    @staticmethod
    def _dijkstra(sites, predecessors):
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
        return distances

    def _goal_drive(self):
        """How hard the story pursues its goal right now: the genome's goal_bias,
        paced like a story arc. Off during the opening, ramping up to full
        strength between mission_settings.pursuit_start_step and
        pursuit_full_step, so the goal becomes the story's climax."""
        start = config.get("mission_settings.pursuit_start_step", 12)
        full = max(start + 1, config.get("mission_settings.pursuit_full_step", 30))
        pacing = min(1.0, max(0.0, (self.actions_taken - start) / (full - start)))
        # Momentum: a quest that has not moved for a while pulls harder (the main quest keeps
        # the story going), from momentum_after turns since its last progress up to full at twice that.
        after = config.get("mission_settings.momentum_after", 0)
        if after and self.mission and not self.won:
            idle = self.actions_taken - self.__dict__.get("last_quest_progress", 0)
            pacing = max(pacing, min(1.0, max(0.0, (idle - after) / float(after))))
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
            child for child in self.spawn_options(self.current.name)
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

    # ---- the director: how a story builds over its course ---------------------------------

    def _director(self):
        """The director settings, or None when it is off."""
        cfg = config.get("director") or {}
        return cfg if cfg.get("enabled", False) else None

    def _max_actions(self):
        """The action limit, plus the turns spent in fast travel (travel.max_free_turns at most)."""
        free = min(int(self.__dict__.get("free_turns", 0)), int((config.get("travel") or {}).get("max_free_turns", 10)))
        return max(100, int(config.get("narrative_end.max_actions", 100))) + free

    def escalation(self):
        """0 until director.escalation_start (share of the action limit), then rising to 1
        at the limit: how far the story has built toward its end."""
        cfg = self._director()
        if cfg is None:
            return 0.0
        start = min(0.99, max(0.0, float(cfg.get("escalation_start", 0.3))))
        progress = min(1.0, self.actions_taken / self._max_actions())
        return max(0.0, min(1.0, (progress - start) / (1.0 - start)))

    def in_finale(self):
        """The last director.finale_actions actions before the limit, while the mission is open."""
        cfg = self._director()
        if cfg is None or self.won or self.ended:
            return False
        return self.actions_taken >= self._max_actions() - max(0, int(cfg.get("finale_actions", 15)))

    def _scaled_damage(self, amount):
        """A health loss as the director makes it: x damage_scale, growing with the escalation."""
        cfg = self._director()
        if cfg is None or amount <= 0:
            return amount
        scaled = amount * float(cfg.get("damage_scale", 1.0)) * (
            1.0 + float(cfg.get("damage_escalation", 0.0)) * self.escalation())
        return max(1, int(math.floor(scaled + 0.5)))

    def _would_loop(self, a, window):
        """Whether an action can only repeat itself: used within ``window`` turns, and every
        outcome it can tell has been told already. Never: travel and events, what the
        author made ``repeat: always``, a retry after a failure, and picking up an item."""
        if a.source in ("navigation", "event") or a.collects_object or getattr(a, "retry_on_failure", False):
            return False
        if a.repeat == "always" and a.source != "conversation":
            return False
        last = getattr(self, "_last_used", {}).get(self._novelty_key(a))
        if last is None or self.actions_taken - last >= window:
            return False
        told = [o.desc for o in a.outcomes if o.desc]
        return bool(told) and all(self.outcome_counts[desc] for desc in told)

    def _announce_finale(self):
        if self.in_finale() and not getattr(self, "_finale_announced", False):
            self._finale_announced = True
            message = (self.narrate("finale_start", title=self.mission.title) if self.mission
                       else self.narrate("finale_start_free"))
            self.history.append(message)
            self.tags_seen.append(["finale"])
            if self.interactive or self.verbose:
                print(f"  >>> {message}")

    def _recap(self):
        """The journey in a few words, for the epilogue of a won story: the quest's steps, the
        friends made, the threads tied up and the turns of fate."""
        parts = []
        # The journey as a road, not a tally: where it began, where it ended, the places that mattered.
        route = list(dict.fromkeys(self.path))
        if len(route) > 2:
            middle = sorted(route[1:-1], key=lambda name: (-self.location_counts.get(name, 0), route.index(name)))[:3]
            parts.append(f"From the {route[0]} to the {route[-1]}, by way of the {', the '.join(middle)}.")
        stages = [stage["title"] for stage in getattr(self.mission, "stages", None) or []] if self.mission else []
        if len(stages) > 1:
            parts.append("Step by step: " + ", then ".join(stages) + ".")
        allies = sorted(self.__dict__.get("allies", set()))
        if allies:
            parts.append("Friends made: " + ", ".join(self._called(name) for name in allies) + ".")
        twists = [t for t in self.__dict__.get("twist_threads", []) if t["resolved"]]
        if twists:
            parts.append("The turns of fate: " + "; ".join(f"{t['actor']} {t['text']}" for t in twists[:2]) + ".")
        return " ".join(parts) + (" " if parts else "")

    def _epilogue(self):
        """The closing text of a story that reaches the action limit with the director on."""
        mission = self.narrate("epilogue_mission_open", title=self.mission.title) if self.mission and not self.won else ""
        return self.narrate(
            "end_epilogue", turns=self.actions_taken, mission=mission, recap=self._recap(), places=len(set(self.path)),
            people=len(self.character_interactions), items=len(self.inventory),
        ).strip()

    def _check_end_conditions(self):
        if self.health_player.is_dead:
            return self._end("death", self.narrate("end_death"))

        if self.mission and not self.won:
            self._advance_stages()
        if self.mission and not self.won and (self._mission_completed or self._goal_met(self.mission)) and (
                getattr(self, "mission_stage", 0) >= len(getattr(self.mission, "stages", None) or [None]) - 1) and (
                self._climax_holds_win()):
            self._arm_climax()  # the quest's last step is a confrontation (climax): the win waits for it
        elif self.mission and not self.won and (self._mission_completed or self._goal_met(self.mission)) and (
                getattr(self, "mission_stage", 0) >= len(getattr(self.mission, "stages", None) or [None]) - 1):
            self.won = True
            complete = self.narrate("mission_complete", title=self.mission.title)
            self.history.append(complete)
            if self.interactive or self.verbose:
                print(f"  >>> {complete}")
            self._run_effects(self.mission.rewards)  # quest rewards
            if config.get("mission_settings.end_on_goal", True):
                ending = self.narrate("end_mission", title=self.mission.title)
                self._say("end_victory", title=self.mission.title, turns=self.actions_taken, recap=self._recap(),
                          places=len(set(self.path)), people=len(self.character_interactions))
                return self._end("mission", ending + " " + self.history[-1] if self.history else ending)

        if self._story_end:
            return self._end("story_end", self._story_end)
        if self.health_player.is_dead:  # e.g. a reward that costs health
            return self._end("death", self.narrate("end_death"))

        end_cfg = config.get("narrative_end", {})
        max_actions = self._max_actions()
        if self.actions_taken >= max_actions:
            if self._director() is not None:  # a finale was played: the story closes with an epilogue
                return self._end("epilogue", self._epilogue())
            return self._end("action_limit", self.narrate("end_action_limit", limit=max_actions))

        max_consecutive = end_cfg.get("max_consecutive_steps_in_location", 5)
        # A player directs their own way: lingering is their choice (the action limit still ends the story).
        if self.consecutive_steps_in_location >= max_consecutive and not self.__dict__.get("player_directed", False):
            return self._end("stalled", self.narrate("end_stalled", location=self.current.name))

        if self.current.name == "Dungeon" and end_cfg.get("end_on_dungeon", False):
            return self._end("dungeon", self.narrate("end_dungeon", location=self.current.name))

        return False

    def _end(self, kind, reason):
        """End the story. ``end_kind`` is what scoring uses; ``end_reason`` is story text."""
        self.ended = True
        self.end_kind = kind
        self.end_reason = reason
        self._remember_story()  # for the profile's next story (memory)
        if self._store is not None:
            self._store.save(self.profile, self.variables, finished=True)
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
        weights = config.get("scene.visibility") or {}
        if "social" in char.associated_tags:
            score += self.genome.bias_social * weights.get("npc_social_bias_weight", 1.0)
        if "threat" in char.associated_tags:
            score += self.genome.bias_threat * weights.get("npc_threat_bias_weight", 0.5)
        if "urban" in char.associated_tags:
            score += self.genome.bias_urban * weights.get("npc_urban_bias_weight", 0.25)
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

    # ---- NPC roles: who matters how much, how hard they are to find and to beat ----------------

    @staticmethod
    def _roles_cfg():
        return config.get("npc_roles") or {}

    def _mission_targets(self):
        """The NPCs the story's quest is about: removed by a goal, or whose actions (or the
        events they start) are a goal's action."""
        if not self.mission:
            return set()
        goals = [dict(self.mission.goal or {})] + [dict(stage["goal"]) for stage in getattr(self.mission, "stages", None) or []]
        names, actions = set(), set()
        for goal in goals:
            if goal.get("npc_removed"):
                names.add(str(goal["npc_removed"]))
            if goal.get("action"):
                actions.add(str(goal["action"]).split(") ", 1)[-1])
        for char in self.world.characters:
            for action in char.actions:
                event = self.world.events.get(action.triggers_event) if action.triggers_event else None
                if action.name.split(") ", 1)[-1] in actions or (
                        event is not None and any(a.name.split(") ", 1)[-1] in actions for a in event.actions)):
                    names.add(char.name)
        return names

    def _assign_roles(self):
        """{npc: role} for this story: an NPC's own `role`, else the first npc_roles.infer rule
        it matches (mission_target: the quest is about them; unique; tags_any; hostile)."""
        cfg = self._roles_cfg()
        if not cfg.get("enabled", False):
            return {}
        roles = cfg.get("roles") or {}
        targets = self._mission_targets()
        assigned = {}
        for char in self.world.characters:
            own = getattr(char, "role", None)
            if own in roles:
                assigned[char.name] = own
                continue
            tags = {str(t).lower() for t in char.associated_tags}
            for rule in cfg.get("infer") or []:
                if rule.get("mission_target") and char.name not in targets:
                    continue
                if "unique" in rule and bool(rule["unique"]) != bool(char.unique):
                    continue
                wanted = {str(t).lower() for t in rule.get("tags_any") or []}
                if wanted and not wanted & tags:
                    continue
                if rule.get("hostile") and not char.encounter_event:
                    continue
                if rule.get("role") in roles:
                    assigned[char.name] = rule["role"]
                    break
        return assigned

    def role_of(self, name):
        """(role, its npc_roles settings) of an NPC in this story; (None, {}) without one."""
        role = self.__dict__.get("npc_roles", {}).get(name)
        return role, dict((self._roles_cfg().get("roles") or {}).get(role) or {})

    def importance(self, name):
        """0 (a bystander) .. 3 (the story's nemesis): how much an NPC matters in this story."""
        return int(self.role_of(name)[1].get("importance", 0))

    def _knowledge(self):
        """What the player has learned: clues planted, quest steps done, allies made."""
        return len(self.setup_indices) + getattr(self, "mission_stage", 0) + len(self.__dict__.get("allies", ()))

    def _role_revealed(self, char):
        """Whether an NPC may appear in scenes yet: an important role stays out of sight until
        the story has run its reveal_turns turns, or the player has learned reveal_clues
        things (_knowledge), or a reveal or a lead names them."""
        role, settings = self.role_of(char.name)
        if not settings or char.name in self.__dict__.get("revealed_npcs", set()) or char.name in self.__dict__.get("met", {}):
            return True
        turns, clues = settings.get("reveal_turns"), settings.get("reveal_clues")
        if not turns and not clues:
            return True
        return bool((turns and self.actions_taken >= int(turns)) or (clues and self._knowledge() >= int(clues)))

    def _hidden_important(self):
        """Important NPCs (importance >= 1) not revealed yet, the most important first."""
        hidden = [c for c in self.world.characters if self.importance(c.name) >= 1 and not self._role_revealed(c)
                  and not self._is_removed(c.name) and self.is_enabled(c)]
        return sorted(hidden, key=lambda c: (-self.importance(c.name), c.name))

    def _name_hidden_npc(self, source_npc):
        """A lead about someone who matters (npc_roles.leads_name_hidden): the source names an
        important NPC still out of sight, who can be found from now on. The name, or None."""
        if not self._roles_cfg().get("leads_name_hidden", True):
            return None
        hidden = [c for c in self._hidden_important() if c.name != source_npc]
        if not hidden:
            return None
        char = hidden[0]
        self.revealed_npcs.add(char.name)
        home = self.npc_homes.get(char.name)
        source = self._called(source_npc)
        self._say("npc_named", npc=source[:1].upper() + source[1:], target=self._called(char.name),
                  role=self.role_of(char.name)[1].get("label", self.role_of(char.name)[0]),
                  location=home or "the city")
        self._record_clues([f"setup_lead_{char.name.lower().replace(' ', '_')}"], note=f"{source_npc} named {char.name}")
        self._beat("reveal", npc=char.name, via="lead")
        return char.name

    def _npc_can_appear_here(self, char, explicit=False):
        """Unique NPCs only appear at their home; generic ones wherever they fit.

        ``explicit`` is used for writer-driven reveals, which bypass tag fitting.
        """
        if self._is_removed(char.name) or not self.is_enabled(char):
            return False
        if not explicit and not self._role_revealed(char):
            return False  # someone who matters, not found yet (npc_roles)
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
        location = self.world.locations.get(location_name)
        if location is not None and getattr(location, "exit_locked", False):
            # exit_locked: nobody leaves until an outcome opens the exits (unlock_exit, an escape).
            return location_name not in self.unlocked_locations
        rule = config.get("location_constraints", {}).get(location_name, {})
        if not rule.get("requires_escape", False):
            return False
        if rule.get("lock_when", "sent") == "always":
            return location_name not in self.unlocked_locations
        return self.confined_in == location_name

    def _score_object_visibility(self, obj, location=None):
        location = location or self.current
        weights = config.get("scene.visibility") or {}
        score = semantic_tag_similarity(obj.associated_tags, location.tags) * weights.get("item_similarity_weight", 2.0)
        if "urban" in obj.associated_tags:
            score += self.genome.bias_urban * weights.get("item_urban_bias_weight", 0.4)
        if "maritime" in obj.associated_tags:
            score += self.genome.bias_maritime * weights.get("item_maritime_bias_weight", 0.4)
        if "combat" in obj.associated_tags:
            score += self.genome.bias_threat * weights.get("item_combat_bias_weight", 0.2)
        return score

    def _commit_score(self, target_name):
        """How strongly the genome wants to head down a newly found path."""
        target = self.world.locations[target_name]
        score = semantic_tag_similarity(self.current.tags, target.tags)
        score += self.genome.voyage_bias
        score += self.genome.discovery_bias * config.get("game_state.commit_discovery_weight", 0.4)
        score -= self.location_counts.get(target_name, 0) * config.get("game_state.commit_revisit_penalty", 0.6)
        # Paths toward the mission goal are more tempting as the climax nears.
        score += self.goal_route_score(target_name) * self._goal_drive()
        return score

    def _barred_places(self):
        """Places a failed attempt was meant to reach, for director.barred_turns: the
        engine does not carry the player there by itself (the player may still go)."""
        return {name for name, until in getattr(self, "_barred", {}).items() if self.actions_taken < until}

    def _should_commit_to_new_path(self, target_name, threshold=None):
        if not target_name or target_name not in self.world.locations or target_name in self._barred_places():
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
        phases = transition_cfg.get("story_phases") or {}
        phase_tags = set(
            phases.get("intro_tags", ["exploration", "urban", "maritime", "trade", "nature"])
            if progress < phases.get("intro_end", 0.3)
            else phases.get("middle_tags", ["threat", "social", "dark", "guarded", "discovery"])
            if progress < phases.get("climax_start", 0.75)
            else phases.get("climax_tags", ["palace", "vault", "luxury", "payoff", "success"])
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
        if location.name in self._payoff_sites() and self._open_clues():
            score += self.genome.setup_prob * rules.settings()["payoff_route_weight"]
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
        barred = self._barred_places()  # avoided unless nothing else is left
        if barred:
            preferred = self._best_transition_destination_core(candidate_names, intent_tags, character,
                                                               set(exclude or ()) | barred, allow_special)
            if preferred is not None:
                return preferred
        return self._best_transition_destination_core(candidate_names, intent_tags, character, exclude, allow_special)

    def _best_transition_destination_core(
        self,
        candidate_names=None,
        intent_tags=None,
        character=None,
        exclude=None,
        allow_special=False,
    ):
        exclude = set(exclude or ()) | {self.current.name}
        names = candidate_names or self.world.locations.keys()
        candidates = [
            self.world.locations[name]
            for name in names
            if name in self.world.locations
            and name not in exclude
            and (name != "Dungeon" or allow_special)
            and (allow_special or not self.sent_only(name))
            and self.is_enabled(self.world.locations[name])
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
        score += self._success_chance(outcome, action) * objective_cfg.get(
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
        open_clues = self._open_clues()
        if any(tag.startswith("setup_") for tag in tags) and self.actions_taken < 5 \
                and len(open_clues) < rules.settings()["max_open_clues"]:
            score += self.genome.setup_prob * objective_cfg.get("setup_weight", 1.5)
        if any(tag.startswith("payoff_") for tag in tags) and self.actions_taken >= 4 and open_clues:
            score += objective_cfg.get("payoff_weight", 2.0)

        # Outcomes that advance the mission (e.g. revealing the goal item).
        score += sum(
            1 for key, value in self._pending_goal().items()
            if key in {"location", "item", "npc_removed", "outcome_tag", "coins", "variables"}
            and self._advances(action, key, value, outcomes=[outcome])
        ) * config.get("action_selection.mission_goal_weight", 6.0) * (self._goal_drive() - self._goal_restraint())
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

    def _stale(self, outcome):
        """An outcome that has had its time: its ``until`` holds, or it plants a numbered
        clue (setup_N) whose payoff (payoff_N) the story has already told."""
        if getattr(outcome, "until", None) and self.holds(outcome.until):
            return True
        for tag in outcome.tags:
            tag = str(tag).lower()
            if tag.startswith("setup_") and tag[6:].isdigit() and f"payoff_{tag[6:]}" in self.payoff_indices:
                return True
        return False

    def _best_outcome(self, outcomes, action):
        """The outcome the storyteller prefers among ``outcomes`` (None if empty);
        stale outcomes only when nothing else is left."""
        if not outcomes:
            return None
        outcomes = [o for o in outcomes if not self._stale(o)] or outcomes
        return max(outcomes, key=lambda outcome: (self._outcome_objective_score(outcome, action), outcome.desc))

    def _resolve_outcome(self, action):
        """(attempt, told, check result, succeeded) of taking ``action``.

        The storyteller picks what is attempted, as before. A failure-branch
        outcome (story_rules.failure_tags, or on_check: fail) is never the
        attempt when a skill check decides: one tries to succeed, and a failure is
        what a failed check tells. A passed check tells a success outcome; a failed
        one tells the best failure outcome, or nothing (a generic failure).
        """
        failure_tags = set(rules.settings()["failure_tags"])
        success = [o for o in action.outcomes if not rules.is_failure(o, failure_tags)]
        failure = [o for o in action.outcomes if rules.is_failure(o, failure_tags)]
        check = self.combat.check_for(action)
        director = self._director()
        if check and director is not None and getattr(self, "_escape_attempts", 0) and self._escape_action(action):
            # Persistence: every failed escape teaches the prisoner something.
            check = (check[0], check[1] + int(director.get("escape_bonus", 15)) * self._escape_attempts)
        elif check and director is not None and self.__dict__.get("failed_attempts", {}).get(action.name):
            # So does every failed attempt at anything: trying again is not the same attempt.
            check = (check[0], check[1] + int(director.get("retry_bonus", 15)) * self.failed_attempts[action.name])
        if check:
            attempt = self._best_outcome([o for o in success if o.on_check == "pass"] or success or action.outcomes, action)
        else:
            attempt = self._best_outcome(action.outcomes, action)
            if attempt is None:
                return None, None, None, False
            check = self.combat.check_for(action, attempt)
            if check and success and rules.is_failure(attempt, failure_tags):
                attempt = self._best_outcome(success, action)
                check = self.combat.check_for(action, attempt)
        if attempt is None:
            return None, None, None, False

        result = None
        if check:
            tags = list(attempt.tags) + [t for o in action.outcomes for t in o.tags]
            check = (check[0], check[1] + self._check_bonus(action, check[0], tags))
            result = self.combat.roll(*check, inventory=self.inventory, step=self.actions_taken)
            self.history.append(self.narrate(
                "check_passed" if result.passed else "check_failed",
                stat=result.stat, roll=result.roll, target=result.target,
            ))
            succeeded = result.passed
        else:
            succeeded = attempt.success_probability(
                genome=self.genome, state=self, action=action,
            ) >= config.get("outcome_objective.success_threshold", 0.5)
        if succeeded:
            return attempt, attempt, result, True
        told = self._best_outcome([o for o in failure if o.on_check == "fail"] or failure, action)
        if told is None and rules.is_failure(attempt, failure_tags):
            told = attempt  # an action written with failure outcomes only
        return attempt, told, result, False

    def _success_chance(self, outcome, action):
        """Chance that an outcome's action succeeds: its skill check's pass chance, or
        the deterministic success probability of unchecked outcomes."""
        check = self.combat.check_for(action, outcome)
        if check:
            bonus = self._check_bonus(action, check[0], outcome.tags)
            return self.combat.pass_chance(check[0], check[1] + bonus, inventory=self.inventory, step=self.actions_taken)
        return outcome.success_probability(self.genome, self, action)

    def update_power(self):
        return self.health_player.calculate_power(self.stats.average(self.combat.item_bonuses(self.inventory)))

    def _goal_restraint(self, story_rules=None):
        """How strongly the storyteller holds back from the mission during the
        opening (story_rules.early_goal_avoidance, fading out by
        mission_settings.pursuit_start_step)."""
        story_rules = story_rules or rules.settings()
        start = config.get("mission_settings.pursuit_start_step", 12)
        if start <= 0:
            return 0.0
        return story_rules["early_goal_avoidance"] * max(0.0, 1.0 - self.actions_taken / start)

    def _useful_items(self):
        """Items worth picking up: they give stat bonuses, open other actions, or are the mission's goal."""
        cached = self.__dict__.get("_useful_item_cache")
        if cached is None:
            sources = [loc.actions for loc in self.world.locations.values()] + [c.actions for c in self.world.characters]
            sources += [o.actions for o in self.world.objects] + [e.actions for e in self.world.events.values()]
            needed = {a.required_object for actions in sources for a in actions if a.required_object}
            needed_tags = {a.required_tag for actions in sources for a in actions if a.required_tag}
            cached = self._useful_item_cache = {
                obj.name for obj in self.world.objects
                if obj.stat_bonus or obj.name in needed or obj.associated_tags & needed_tags
            }
        goal_item = (self._stage_goal()[0].get("item") if self.mission else None)
        return cached | ({goal_item} if goal_item else set())

    def _payoff_sites(self):
        """Locations where a clue can be paid off (an outcome tagged payoff_*)."""
        cached = self.__dict__.get("_payoff_site_cache")
        if cached is None:
            def pays(actions):
                return any(str(t).lower().startswith("payoff_") for a in actions for o in a.outcomes for t in o.tags)
            cached = {name for name, loc in self.world.locations.items() if pays(loc.actions)}
            for char in self.world.characters:
                event = self.world.events.get(char.encounter_event) if char.encounter_event else None
                if char.unique and char.name in self.npc_homes and (pays(char.actions) or (event and pays(event.actions))):
                    cached.add(self.npc_homes[char.name])
            for obj in self.world.objects:
                instance = self.item_instances.get(obj.name)
                if instance is not None and instance.location and pays(obj.actions):
                    cached.add(instance.location)
            self._payoff_site_cache = cached
        return cached

    def _rank_actions(self, actions):
        story_rules = rules.settings()
        open_clues = self._open_clues()
        clues_full = len(open_clues) >= story_rules["max_open_clues"]
        goal_pull = self._goal_drive() - self._goal_restraint(story_rules)
        director = self._director()
        escalation = self.escalation()
        if director is not None and self.in_finale():
            goal_pull = max(goal_pull, float(director.get("finale_goal_pull", 1.0)))
        useful_items = self._useful_items()
        previous = self.path[-2] if len(self.path) > 1 else None
        heading_step = self._heading_step()
        boredom = self._boredom()
        due = self._due_clues()
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
                weight += self.genome.threat_prob * config.get("action_selection.threat_weight", 1.0) * (
                    1.0 + (float(director.get("threat_weight", 0.0)) * escalation if director else 0.0))
            if "spawn" in all_o_tags:
                weight += self.genome.spawn_prob * config.get("action_selection.spawn_weight", 1.2)
            closing = escalation if director is not None and director.get("close_threads", True) else 0.0
            if any(t.startswith("setup_") for t in all_o_tags) and not clues_full and not self.in_finale():
                # New threads are worth less as the story nears its end: there is no time to close them.
                weight += self.genome.setup_prob * config.get("action_selection.setup_weight", 1.0) * (1.0 - closing)
            # An open clue pulls toward the action that pays it off, the more so near the end.
            if open_clues and any(str(t).lower().startswith("payoff_") for t in all_o_tags):
                weight += self.genome.setup_prob * story_rules["payoff_weight"] * min(len(open_clues), 2) * (1.0 + closing)
            # Items are worth picking up, useful ones more.
            if a.collects_object and a.collects_object not in self.inventory and self._item_obtainable(a.collects_object):
                weight += self.genome.discovery_bias * story_rules["collect_weight"] * (
                    1.5 if a.collects_object in useful_items else 1.0)
            if discovery_score:
                weight += discovery_score * self.genome.discovery_bias * config.get("action_selection.discovery_weight", 1.2)
            if movement_score:
                weight += movement_score * self.genome.voyage_bias * config.get("action_selection.movement_weight", 1.4)
            if recovery_score and self.health_player.is_low:
                weight += recovery_score * self.genome.recovery_bias * config.get("action_selection.recovery_weight", 1.2)
            if a.triggers_event:
                weight += self.genome.event_prob * config.get("action_selection.event_trigger_weight", 1.5)
            # Skill fit: prefer checks this character is likely to pass.
            check = self.combat.check_for(a) or next(
                (c for c in (self.combat.check_for(a, o) for o in a.outcomes) if c), None
            )
            if check:
                chance = self.combat.pass_chance(*check, inventory=self.inventory, step=self.actions_taken)
                weight += (chance - 0.5) * config.get("action_selection.skill_fit_weight", 2.0)

            # Novelty: every earlier use of the same choice makes it less appealing,
            # each one more than the last (story_rules.repetition_power).
            weight -= (
                self.choice_counts[self._novelty_key(a)] ** story_rules["repetition_power"]
                * self.genome.novelty_bias
                * config.get("action_selection.repetition_penalty", 1.0)
            )
            # Going straight back where one came from (A -> B -> A) is a loop.
            if a.source == "navigation" and previous and a.owner == previous:
                weight -= story_rules["backtrack_penalty"] * self.genome.novelty_bias
            # Helping someone with what they want (npc_goals.action_weight).
            if a.character_name and a.source not in ("conversation", "navigation"):
                char = next((c for c in self.world.characters if c.name == a.character_name), None)
                if char is not None and not char.encounter_event and any(
                        self._helps_goal(char, o, a) is not None for o in a.outcomes):
                    weight += float((config.get("npc_goals") or {}).get("action_weight", 0.5)) * (
                        0.5 + self.genome.bias_social)
            # A failed attempt is not retried at once (director.retry_cooldown turns): something
            # else happens first, and the next try is easier (retry_bonus).
            # Only when there is something else to do here: leaving and coming straight back
            # would be worse filler than trying again.
            if director is not None and a.name in self.__dict__.get("_last_failure", {}) and not self._escape_action(a):
                if self.actions_taken - self._last_failure[a.name] < int(director.get("retry_cooldown", 2)) and any(
                        other is not a and other.source not in ("navigation", "conversation") for other in actions):
                    weight -= float(director.get("retry_cooldown_penalty", 3.0))
            # Ping-pong: straight back to the place left on the last turn, after a turn of travel.
            if (director is not None and a.source == "navigation" and a.owner == previous
                    and (self.__dict__.get("turn_log") or [{}])[-1].get("source") == "navigation"):
                weight -= float(director.get("ping_pong_penalty", 2.5))
            # The director's rhythm: after turns with nothing new, what brings something new
            # pulls harder; a clue left open too long wants its payoff (and the way to it);
            # a place not visited yet pulls more the longer since the last new place.
            if director is not None:
                if boredom and self._brings_something_new(a):
                    weight += float(director.get("boredom_weight", 0.0)) * boredom
                if due and (any(str(t).lower().startswith("payoff_") for o in a.outcomes for t in o.tags)
                            or (a.source == "navigation" and a.owner in self._payoff_sites())):
                    weight += float(director.get("payoff_due_weight", 0.0)) * min(len(due), 2)
                if (a.source == "navigation" and self.location_counts.get(a.owner, 0) > 0
                        and not self._place_promise(a.owner)):
                    # A dead end: back to a place with nothing left to do, see or settle there.
                    weight -= float(director.get("dead_end_penalty", 0.0))
                if a.source == "navigation" and self.location_counts.get(a.owner, 0) == 0:
                    since = self.actions_taken - self.__dict__.get("last_new_place_turn", 0)
                    weight += float(director.get("explore_weight", 0.0)) * min(1.0, since / 5.0) * (
                        0.5 + self.genome.voyage_bias)
            # A far place the story set out for pulls toward the next step on the way.
            if a.source == "navigation" and heading_step and a.owner == heading_step:
                weight += config.get("action_selection.heading_weight", 1.5)
            # An open clue also pulls toward the place where it can be paid off.
            if a.source == "navigation" and open_clues and a.owner in self._payoff_sites():
                weight += self.genome.setup_prob * story_rules["payoff_route_weight"]
            # The mission pulls once the story arc reaches it, and holds back before.
            if goal_pull:
                weight += (
                    self._mission_affinity(a)
                    * config.get("action_selection.mission_goal_weight", 6.0)
                    * goal_pull
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
        # Near-ties (within tie_epsilon of the best of their group) are ordered by
        # story logic, not by name: the less used choice, then the one with more
        # outcomes not yet shown, then a per-story order.
        epsilon = story_rules["tie_epsilon"]
        if epsilon > 0 and len(ranked) > 1:
            groups = []
            for pair in ranked:
                if groups and groups[-1][0][0] - pair[0] <= epsilon:
                    groups[-1].append(pair)
                else:
                    groups.append([pair])
            ranked = [pair for group in groups for pair in sorted(group, key=lambda pair: (
                self.choice_counts[self._novelty_key(pair[1])],
                -sum(1 for o in pair[1].outcomes if not self.outcome_counts[o.desc]),
                self._story_order(pair[1].name),
            ))]
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

    def _only_arrives_here(self, a):
        """Whether an action's successes only bring the player where they already are
        ("the guard waves you through to the deck", offered on the deck)."""
        successes = [o for o in a.outcomes
                     if getattr(o, "on_check", None) != "fail" and "failure" not in {str(t).lower() for t in o.tags}]
        return bool(successes) and all(o.move_to == self.current.name for o in successes)

    def _out_of_reach(self, a):
        """Someone's offer to take you to a place this one does not border (a gate guard
        met in a cell cannot let you into the Palace): it is not offered here. Only for
        NPCs, who are met in many places; a place's own actions define its routes."""
        if a.source != "npc" or getattr(self, "routes", None) is None:
            return False
        places = {o.move_to for o in a.outcomes if o.move_to}
        places |= {o.reveal_location for o in a.outcomes if o.reveal_location and o.lead_to_known}
        places = {p for p in places if p in self.world.locations and p != self.current.name}
        return bool(places) and not any(self._adjacent(self.current.name, p) for p in places)

    def _escape_action(self, a):
        """An action that escapes the confinement of this location (an outcome with one of
        location_constraints.<location>.escape_tags, where requires_escape is set)."""
        if a.source == "location" and getattr(self.current, "exit_locked", False) and any(
                getattr(o, "unlock_exit", False) for o in a.outcomes):
            return True  # the way to open a locked exit
        constraint = (config.get("location_constraints") or {}).get(self.current.name) or {}
        if a.source != "location" or not constraint.get("requires_escape", False):
            return False
        escape = {str(tag).lower() for tag in constraint.get("escape_tags", ["escape"])}
        return any(escape & {str(tag).lower() for tag in o.tags} for o in a.outcomes)

    def _is_offered(self, a):
        if self._is_closed(a):
            return False
        if self._escape_action(a):
            # Only a player locked in here escapes (lock_when: sent or always), and may try
            # again until it works.
            return self._location_is_locked()
        return not (self._repeat_rule(a) == "per_visit" and a.name in self.visit_actions_performed)

    @staticmethod
    def _novelty_key(a):
        # Travelling counts per destination, so Harbor -> Market -> Harbor loops wear out.
        return ("travel", a.owner) if a.source == "navigation" else ("action", a.name)

    def _record_choice(self, a, succeeded, scope, visit=None):
        """Use the action up as its ``repeat`` says. With ``retry_on_failure`` a
        failed attempt uses nothing up: a ``once`` action stays until it succeeds,
        and a ``per_visit`` one is offered again in the same visit."""
        self.choice_counts[self._novelty_key(a)] += 1
        self.__dict__.setdefault("_last_used", {})[self._novelty_key(a)] = self.actions_taken  # games saved before
        rule, retry = self._repeat_rule(a), getattr(a, "retry_on_failure", False)
        if rule == "once" and (succeeded or not retry):
            self.spent_actions.add((scope, a.name))
        if rule == "per_visit" and retry and not succeeded and visit == self._visit_id():
            self.visit_actions_performed.discard(a.name)  # marked when it was taken (step)
        if succeeded and a.exclusive_group:
            # The choice has been made: the other options of the group disappear.
            self.closed_groups.add((scope, a.exclusive_group))

    def _outcome_open(self, o):
        """Whether an outcome can be told now: a payoff needs its setup planted first
        (requires_setup; with story_rules.gate_payoffs a payoff_* clue tag also needs an
        open clue, or with clue_matching: numbered its own setup_N), and a named thread
        already paid off is not paid again."""
        needed = getattr(o, "requires_setup", None)
        if needed and str(needed) not in self.__dict__.get("setups_planted", set()):
            return False
        paid = getattr(o, "payoff_id", None)
        if paid and str(paid) in self.__dict__.get("completed_payoffs", set()):
            return False
        story_rules = rules.settings()
        if story_rules.get("gate_payoffs", True):
            payoffs = [str(tag).lower() for tag in o.tags if str(tag).lower().startswith("payoff_")]
            if payoffs:
                if story_rules.get("clue_matching", "any") == "numbered":
                    # payoff_N pays off setup_N only: it waits for it.
                    if any(f"setup_{tag[len('payoff_'):]}" not in self.setup_indices for tag in payoffs):
                        return False
                elif not self._open_clues():
                    return False  # nothing planted is waiting to be paid off
        return True

    def _prepare_action(self, a):
        """Resolve item/coin requirements on a copied action; return None if unavailable.
        Outcomes that cannot be told yet (a payoff before its setup) are left out; an
        action left with none is not offered."""
        if a.outcomes and a.source not in ("navigation", "conversation"):
            open_outcomes = [o for o in a.outcomes if self._outcome_open(o)]
            if not open_outcomes:
                return None
            a.outcomes = open_outcomes
        # Check for required object/tag
        if a.required_tag:
            matching_items = [obj for obj in self.world.objects
                              if a.required_tag in obj.associated_tags and obj.name in self.inventory]
            if not matching_items:
                return None
            item = sorted(matching_items, key=lambda obj: (self._score_object_visibility(obj), obj.name), reverse=True)[0]
            a.active_item = item.name
            if "{" in a.name and "}" in a.name:
                # "Give him the {alcohol}" -> "Give him the Vintage Wine" (a name, not a description).
                a.name = re.sub(r'\{.*?\}', lambda _: item.name, a.name)

        if a.required_object:
            if a.required_object not in self.inventory:
                return None
            a.active_item = a.required_object

        if a.required_coins and a.required_coins > self.coins:
            return None
        if a.collects_object and (a.collects_object in self.inventory or not self._item_obtainable(a.collects_object)):
            return None
        return a

    def _event_blocked(self, event):
        """Why an authored event cannot start now (None: it can). _enter_event checks
        this for every way into an event: trigger actions, NPC encounters, chains,
        start_event effects and the random schedule."""
        if not config.get("authored_events.enabled", True):
            return "authored events are disabled"
        if not self.is_enabled(event):
            return "its enabled_if does not hold"
        past = [entry for entry in self.event_log if entry["event"] == event.name]
        if not past:
            return None
        if event.max_occurrences is not None and len(past) >= event.max_occurrences:
            return f"it already happened {len(past)} time(s) (max_occurrences)"
        here, visit = self.current.name, self.location_counts.get(self.current.name, 0)
        if (
            event.repeat == "once"
            or (event.repeat == "per_location" and any(entry["location"] == here for entry in past))
            or (event.repeat == "per_visit" and any(
                entry["location"] == here and entry.get("visit") == visit for entry in past))
        ):
            return f"it already happened (repeat: {event.repeat})"
        ended = past[-1].get("ended_step")
        if event.cooldown and (ended is None or self.actions_taken - ended < event.cooldown):
            return "its cooldown has not passed"
        return None

    def _event_available(self, event_name):
        """Whether an event may start now. Unknown events are left to _enter_event,
        which reports them."""
        event = self.world.events.get(event_name)
        return event is None or self._event_blocked(event) is None

    def _schedule_random_event(self):
        """Authored events with ``trigger: random`` may start by themselves. Once per
        turn, each one that can start rolls its ``chance`` (session RNG); among those
        that hit, ``weight`` picks the one that starts."""
        if self.ended or self.active_event or self._scheduled_turn == self.actions_taken:
            return False
        self._scheduled_turn = self.actions_taken
        due = [event for event in self.world.events.values()
               if event.trigger == "random" and self._event_blocked(event) is None]
        hits = [event for event in due if self.rng.random() < event.chance]
        if not hits:
            return False
        event = hits[0] if len(hits) == 1 else self.rng.choices(hits, weights=[e.weight for e in hits], k=1)[0]
        return self._enter_event(event.name, cause="random")

    def _action_enabled(self, a):
        """Whether an action's ``enabled_if`` holds and the event it triggers may start
        (its condition, repeat, max_occurrences and cooldown)."""
        if getattr(a, "until", None) and self.holds(a.until):
            return False  # its time is over
        if a.outcomes and all(self._stale(o) for o in a.outcomes):
            return False  # it could only tell what the story has moved past
        return self.is_enabled(a) and (not a.triggers_event or self._event_available(a.triggers_event))

    def _dialogue_line(self, char):
        """What the NPC says when talked with: the first dialogue line whose
        ``enabled_if`` holds (None: no line applies)."""
        return next((line for line in char.dialogue if self.is_enabled(line)), None)

    def _event_source_character(self):
        if not self.event_source_npc:
            return None
        return next((c for c in self.world.characters if c.name == self.event_source_npc), None)

    def _available_event_actions(self):
        """Only the active event's actions are offered; no travel, NPC, or item actions."""
        actions = []
        for template in self.active_event.actions:
            if not self._is_offered(template) or not self._action_enabled(template):
                continue
            if self._only_arrives_here(template):
                continue  # "slip away into the Sewers" while in the Sewers
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
                source="event_fallback",  # resolves the event as abandoned, neither success nor failure
                character_name=self.event_source_npc,
                outcomes=[Outcome(
                    desc=event_cfg.get("fallback_description", "You step back and let the moment pass."),
                    tags=["neutral"],
                )],
            ))
        return actions

    def _visit_id(self):
        return (self.current.name, self.location_counts.get(self.current.name, 0))

    def _called(self, name):
        """A character's or item's name as the story says it: "Aurora", "the Thief",
        "a Gate Guard", "an Ancient Map", "the Heavy Shield", "some Gold Coins"."""
        entity = next((c for c in self.world.characters if c.name == name), None)
        if entity is None:
            entity = next((o for o in self.world.objects if o.name == name), None)
        article = getattr(entity, "article", None)
        if article is None:
            if entity is not None and getattr(entity, "unique", False):
                article = "the"
            elif name.endswith("s") and not name.endswith("ss"):
                article = "some"
            else:
                article = "an" if name[:1].lower() in "aeiou" else "a"
        return f"{article} {name}".strip()

    def _announce_arrival(self):
        """Log the location, its NPCs, and its items once per visit."""
        if self.active_event or self._announced_visit == self._visit_id():
            return
        self._announced_visit = self._visit_id()
        desc, npc_lines, object_lines = self._describe_visible_scene()
        self.history.append(self.narrate("arrival", location=self.current.name, description=desc))
        for char_name, char_desc in npc_lines:
            self._meet(char_name, char_desc)
        for obj_name, obj_desc in object_lines:
            self.history.append(self.narrate("arrival_item", item=self._called(obj_name), description=obj_desc))

    def _refresh_scene(self):
        """Place NPCs and items when the player enters a location (or the scene emptied)."""
        # 1. Detect New Location Entry, Stale Inhabitants, or a variable change
        # (in worlds whose NPCs and items depend on the story's state: any change of it).
        if self.world.dynamic_conditions:
            epoch = self._condition_epoch()
            if epoch != self.__dict__.get("_scene_epoch"):
                self._scene_dirty = True
                self._scene_epoch = epoch
        if not self.current_inhabitants or self.current.name != self.last_location_name or self._scene_dirty:
            same_visit = self.current.name == self.last_location_name
            previously_visible = (set(self.visible_inhabitants), set(self.visible_objects)) if same_visit else (set(), set())
            self._scene_dirty = False
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

            # 3. Dynamic Object visibility (generic items by rank; unique items where their instance lies)
            self.location_objects, self.visible_objects = self._scene_objects(self.current)

            # A re-evaluation within the same visit keeps what was already discovered.
            allowed_npcs = {c.name for c in self.world.characters if self._npc_can_appear_here(c, explicit=True)}
            kept_npcs = previously_visible[0] & allowed_npcs - self.visible_inhabitants
            self.visible_inhabitants |= kept_npcs
            self.current_inhabitants += [c for c in self.world.characters if c.name in kept_npcs]
            self.visible_objects |= previously_visible[1] & {obj.name for obj in self.location_objects}

        self._announce_arrival()

    def get_available_actions(self):
        """Returns a list of valid actions for the current state with narrative visibility."""
        if self.active_event:
            return self._available_event_actions()

        self._refresh_scene()

        # A hostile NPC in the scene confronts the player before anything else; then
        # an authored random event may start by itself.
        if self._start_encounter() or self._schedule_random_event():
            return self._available_event_actions()

        # 2. Gather actions for VISIBLE entities only
        raw_actions = []
        raw_actions.extend(self.current.actions)
        
        # Filter inhabitants
        for char in self.current_inhabitants:
            if char.name in self.visible_inhabitants:
                raw_actions.extend(char.actions)
                offset = len(char.name) + self.character_interactions.get(char.name, 0)
                line = self._dialogue_line(char)
                if line is None and char.encounter_event:
                    continue  # a hostile NPC (an ambusher) does not chat, unless dialogue is written for it
                if line is not None:
                    # Authored dialogue chosen by condition; its effects apply when said.
                    texts = line.text if isinstance(line.text, list) else [line.text]
                    conversation_detail, line_effects = self._pick_text(texts, offset=offset), line.effects
                else:
                    conversation_detail = self._pick_text(char.descriptions, offset=offset) or self.narrate(
                        "talk_default", stable=char.name, npc=char.name)
                    if char.goals:
                        conversation_detail += self.narrate("talk_goal", stable=char.name, npc=char.name, goal=char.goals[0])
                    line_effects = None
                raw_actions.append(
                    Action(
                        name=f"({char.name}) " + self.narrate("talk_action", stable=char.name, npc=char.name),
                        character_name=char.name,
                        outcomes=[Outcome(
                            desc=conversation_detail,
                            tags=["social", "conversation"],
                            effects=line_effects,
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

        # 3. Process & Template Actions (not a way here while already here)
        filtered_actions = [copy.deepcopy(a) for a in raw_actions
                            if self._is_offered(a) and self._action_enabled(a) and not self._only_arrives_here(a)
                            and not self._out_of_reach(a)]

        final_actions = [
            a for a in (self._prepare_action(a) for a in filtered_actions) if a is not None
        ]

        # 4. Navigation
        navigation_targets = [] if self._location_is_locked() else sorted(
            name for name in self.current.connected
            if name in self.world.locations and self.is_enabled(self.world.locations[name])
            and self._adjacent(self.current.name, name) and not self.sent_only(name)
        )
        for loc_name in navigation_targets:
            target_loc = self.world.locations.get(loc_name)
            name = self.narrate("travel_new", stable=loc_name, location=loc_name)
            if self.location_counts.get(loc_name, 0) > 0:
                name = self.narrate("travel_return", stable=loc_name, location=loc_name)
            elif target_loc and target_loc.interact_prompts:
                name = self._pick_text(target_loc.interact_prompts, offset=len(loc_name))
            
            # A first journey somewhere says what one sees on the way (the place's distant view).
            view = None
            if self.location_counts.get(loc_name, 0) == 0 and target_loc is not None and target_loc.distant_descriptions:
                view = self._pick_text(target_loc.distant_descriptions, offset=len(loc_name))
            travel_text = (self.narrate("travel_first", stable=loc_name, location=loc_name, view=view) if view
                           else self.narrate("travel_result", stable=loc_name, location=loc_name))
            navigation_action = Action(
                name=name,
                outcomes=[Outcome(
                    desc=travel_text,
                    move_to=loc_name,
                )],
                repeat="always",
                source="navigation",
                owner=loc_name,
            )
            final_actions.append(navigation_action)
        final_actions.extend(self._fast_travel_options(navigation_targets))  # known ways, in one turn (travel)

        # The director does not offer what can only repeat itself (events and travel stay).
        director = self._director()
        if director is not None:
            window = int(director.get("loop_window", 6))
            fresh = [a for a in final_actions if not self._would_loop(a, window)]
            # A way to move on (travel, or an escape for a prisoner) is a real choice: with
            # one, hiding what would only repeat itself leaves enough to do.
            way_on = any(a.source == "navigation" or self._escape_action(a) for a in fresh)
            minimum = 1 if way_on else int(director.get("min_options", 2))
            if len(fresh) >= minimum:
                final_actions = fresh
        return final_actions

    def ranked_available_actions(self, limit=None, order="ranked"):
        """Return currently valid actions in the same order used by the simulator.

        ``limit`` keeps the storyteller's best; ``order="world"`` lists them in the
        scene's order (activities, people, items, travel) instead of best first, for
        players, so the first option is not always the storyteller's favourite."""
        actions = self._actions_now()
        ranked = [action for _, action in self._rank_actions(actions)]
        chosen = ranked if limit is None else ranked[:limit]
        if order == "world":
            position = {id(action): index for index, action in enumerate(actions)}
            chosen = sorted(chosen, key=lambda action: position.get(id(action), len(position)))
        return chosen

    # ---- the choices a player sees -------------------------------------------------------

    def player_options(self, limit=None, order=None):
        """The choices a player sees now (``player_options``): the most relevant of the
        storyteller's ranked options. What the moment is about comes first (a prisoner's
        way out, the quest's next step, the payoff of an open clue, a way on); then the
        rest by rank unless they rank among the lowest (``relevance``, more of them in a
        tense moment: ``focus_relevance``), a few ways on and a few conversations at
        most. Their number follows the moment: fewer when little is worth doing and in
        tense moments (``focus_limit``), never more than ``limit``. An event's choices
        are shown as they are."""
        cfg = config.get("player_options") or {}
        limit = max(1, int(limit if limit is not None else cfg.get("limit", 6)))
        order = order or cfg.get("order", "ranked")
        actions = self._actions_now()
        pairs = self._rank_actions(actions)
        if self.active_event or not cfg.get("relevance_filter", True):
            chosen = [action for _, action in pairs[:limit]]
        else:
            chosen = self._relevant_options(pairs, limit, cfg)
        if order == "world":
            position = {id(action): index for index, action in enumerate(actions)}
            chosen = sorted(chosen, key=lambda action: position.get(id(action), len(position)))
        return chosen

    @staticmethod
    def _option_kind(a):
        return "navigation" if a.source == "navigation" else "conversation" if a.source == "conversation" else a.source

    def tension(self):
        """0..1: how pressing the moment is. A prisoner, the finale and danger in the last
        turn (a threat, a fight, a failure, an escape) are tense (1), low health nearly
        (0.8); a quest step just completed focuses the story (0.5); otherwise it builds
        with escalation (up to 0.5)."""
        cfg = config.get("player_options") or {}
        danger = {str(tag).lower() for tag in cfg.get("tense_tags", ["threat", "combat", "guards", "escape", "failure"])}
        last = (getattr(self, "turn_log", None) or [{}])[-1]
        tension = 0.5 * self.escalation()
        if self._location_is_locked() or self.in_finale():
            tension = 1.0
        if self.health_player.is_low:
            tension = max(tension, 0.8)
        if {str(tag).lower() for tag in last.get("tags", [])} & danger or (last and not last.get("succeeded", True)):
            tension = 1.0  # danger a moment ago
        if self.tags_seen and "milestone" in self.tags_seen[-1]:
            tension = max(tension, 0.5)
        stakes = self.stakes()
        if stakes and stakes["alert"] >= stakes["warn_at"]:
            tension = max(tension, 0.7)  # the watch is looking for the player
        return tension

    def _relevant_options(self, pairs, limit, cfg):
        floor = min(limit, max(1, int(cfg.get("min", 3))))
        focus = max(floor, min(limit, int(cfg.get("focus_limit", 4))))
        tension = self.tension()
        cap = max(floor, min(limit, round(limit - tension * (limit - focus))))
        kind_caps = {"navigation": int(cfg.get("travel_limit", 3)), "conversation": int(cfg.get("talk_limit", 2))}
        chosen = []

        def take(a):
            if a is not None and all(a is not other for other in chosen):
                chosen.append(a)

        # What the moment is about comes first.
        if self._location_is_locked():
            for _, a in pairs:
                if self._escape_action(a):
                    take(a)
        if self.mission and not self.won:
            affinity = [(self._mission_affinity(a), -index, a) for index, (_, a) in enumerate(pairs)]
            best = max(affinity, key=lambda item: (item[0], item[1]), default=(0.0, 0, None))
            if best[0] > 0:
                take(best[2])
        if self._open_clues():
            take(next((a for _, a in pairs if any(
                str(tag).lower().startswith("payoff_") for o in a.outcomes for tag in o.tags)), None))
        take(next((a for _, a in pairs if a.source == "navigation"), None))
        # Backtracking: the way back where one came from stays a choice.
        previous = self.path[-2] if len(self.path) > 1 else None
        if previous and previous != self.current.name and cfg.get("way_back", True):
            take(next((a for _, a in pairs if a.source == "navigation" and a.owner == previous), None))
        # Then the rest by rank while relevant enough (beyond `min`: not in the lowest
        # `relevance` share of the options now, `focus_relevance` in a tense moment),
        # within the kind limits.
        scores = sorted(score for score, _ in pairs)
        calm, tense = (min(1.0, max(0.0, float(cfg.get(key, default))))
                       for key, default in (("relevance", 0.25), ("focus_relevance", 0.5)))
        share = calm + tension * (tense - calm)
        cutoff = scores[min(len(scores) - 1, int(share * len(scores)))] if share > 0 else float("-inf")
        counts = Counter(self._option_kind(a) for a in chosen)
        for score, a in pairs:
            if len(chosen) >= cap:
                break
            kind = self._option_kind(a)
            if any(a is other for other in chosen) or counts[kind] >= kind_caps.get(kind, limit):
                continue
            if len(chosen) >= floor and score < cutoff:
                continue
            chosen.append(a)
            counts[kind] += 1
        return chosen

    def _actions_now(self):
        """The options now; a player left with none is carried on (auto_transfer_when_stuck)."""
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
        return actions

    def _record_clues(self, tags, note=None):
        """Note when each clue (setup_*) and payoff (payoff_*) first appears; ``note`` (what
        the setup said) is kept for the callback its payoff tells."""
        for tag in tags:
            tag = str(tag).lower()
            if tag.startswith("setup_"):
                if tag not in self.setup_indices and note:
                    self.__dict__.setdefault("clue_notes", {})[tag] = {
                        "text": str(note), "place": self.current.name, "turn": self.actions_taken}
                self.setup_indices.setdefault(tag, self.actions_taken)
            elif tag.startswith("payoff_"):
                self.payoff_indices.setdefault(tag, self.actions_taken)

    _CALLBACK_STOP = {"there", "their", "which", "where", "while", "about", "after", "before", "under", "through",
                      "something", "someone", "every", "never", "always", "again"}

    def _related(self, note, payoff_text):
        """Whether a setup and a payoff belong together: told at the same place, or sharing
        a word of five letters or more (not a common one)."""
        if note["place"] == self.current.name:
            return True
        words = lambda text: {w for w in re.findall(r"[a-z]{5,}", text.lower()) if w not in self._CALLBACK_STOP}  # noqa: E731
        return bool(words(note["text"]) & words(payoff_text))

    def _tell_callbacks(self, closed, payoff_text=""):
        """A payoff that closes a clue it belongs with says what it pays off ([CALLBACK] ...),
        so the player sees the thread from the setup to the payoff."""
        notes = self.__dict__.get("clue_notes", {})
        for tag in sorted(closed, key=lambda t: self.setup_indices.get(t, 0)):
            note = notes.get(tag)
            if not note or tag.startswith("setup_twist") or not self._related(note, payoff_text):
                continue  # a twist's payoff tells its own story; unrelated threads are not tied together
            text = note["text"].strip().split(". ")[0].rstrip(".")
            text = text if len(text) <= 110 else text[:107].rstrip() + "..."
            self._say("callback", place=note["place"], text=text, turns=self.actions_taken - note["turn"])

    def _open_clues(self):
        """Clues planted and not yet paid off (story_rules.clue_matching)."""
        return rules.clue_pairs(self.setup_indices, self.payoff_indices, rules.settings()["clue_matching"])[1]

    def _story_order(self, name):
        """A per-story order for choices that tie on every story reason: it differs
        between stories (start, mission, stats) without rolling any dice."""
        salt = f"{self.start_location}|{self.mission.name if self.mission else ''}|{sorted(self.stats.points.items())}"
        return hashlib.sha1(f"{salt}|{self.actions_taken}|{name}".encode()).hexdigest()

    def _release_if_held_too_long(self):
        """The director does not let a prison end the story: a player still locked in
        after director.release_after turns there is let out (and the story goes on)."""
        director = self._director()
        if director is None or self.ended or self.active_event or not self._location_is_locked():
            return False
        if self.consecutive_steps_in_location < int(director.get("release_after", 5)):
            return False
        prison = self.current.name
        self.confined_in = None
        self.unlocked_locations.add(prison)
        self._escape_attempts = 0
        if self._auto_transfer_when_stuck(message_key="released"):
            return True
        self.history.append(self.narrate("released", origin=prison, destination=prison))
        self.consecutive_steps_in_location = 0
        return True

    def _move_on_if_stalled(self):
        """story_rules.on_stall: move: after narrative_end.max_consecutive_steps_in_location
        in one place the traveler moves on, instead of the story ending there."""
        limit = config.get("narrative_end.max_consecutive_steps_in_location", 5)
        if (
            rules.settings()["on_stall"] != "move" or self.ended or self.active_event
            or self.consecutive_steps_in_location < limit or self.health_player.is_dead
            or self.__dict__.get("player_directed", False)  # a player chooses when to move on
        ):
            return False
        return self._auto_transfer_when_stuck(message_key="stall_move")

    def _auto_transfer_when_stuck(self, exclude=None, message_key="auto_transfer"):
        """Move a stuck player to a useful location without consuming an action."""
        if self._location_is_locked():
            return False
        exclude = set(exclude or ())
        exclude.add(self.current.name)

        configured_targets = self.spawn_options(self.current.name)
        target = self._best_transition_destination(
            candidate_names=configured_targets or None,
            intent_tags=self.current.tags,
            exclude=exclude,
        )
        if target is None:
            return False

        origin = self.current.name
        self.current.connect(target)
        if not self._move_to(target.name, cause="auto"):
            return False

        message = self.narrate(message_key, origin=origin, destination=target.name)
        self.history.append(message)
        if self.interactive or self.verbose:
            print(f"  >>> {message}")
        return True

    def _maybe_trigger_unexpected_event(self):
        """A procedural twist: an omen, a passer-by and what they do, told to match what
        it does (a gain, a hurt, a discovery, a mystery that pays off later, a bargain to
        take or refuse). A mystery planted earlier comes due first: after
        unexpected_events.twist_payoff_delay turns (or in the finale) its payoff is told."""
        event_cfg = config.get("unexpected_events", {})
        if not event_cfg.get("enabled", True):
            return None
        cooldown = int(event_cfg.get("cooldown_steps", 2))
        if self.actions_taken - self._last_event_step < cooldown:
            return None
        due = self._due_twist(event_cfg)
        if due is not None:
            return self._pay_off_twist(due)
        if len(self.unexpected_events) >= int(event_cfg.get("max_per_story", 4)):
            return None

        base_chance = float(event_cfg.get("base_chance", 0.12))
        event_pressure = base_chance + self.genome.event_prob * float(
            event_cfg.get("genome_weight", 0.35)
        )
        event_pressure += self.genome.chaos_bias * float(event_cfg.get("chaos_weight", 0.2))
        event_pressure += self.consecutive_steps_in_location * event_cfg.get(
            "stagnation_pressure", 0.04
        )
        director = self._director()
        if director is not None:
            event_pressure *= 1.0 + float(director.get("event_pressure", 0.0)) * self.escalation()
            # Nothing new for a while: something should happen.
            event_pressure += float(director.get("boredom_event_pressure", 0.0)) * self._boredom()
        if event_pressure < event_cfg.get("objective_threshold", 0.24):
            return None

        actors = event_cfg.get("actors", ["a masked courier", "a frightened witness"])
        omens = event_cfg.get("omens", ["a bell rings backwards", "the lights suddenly fail"])
        generic_twists = event_cfg.get("twists", ["offers a dangerous bargain", "reveals a hidden route"])
        objective_index = (
            self.actions_taken
            + self.location_counts.get(self.current.name, 0)
            + sum(ord(character) for character in self.current.name)
        )
        # No part is told twice in a story while unused ones remain.
        used = self.__dict__.setdefault("_unexpected_parts", {"actor": set(), "omen": set(), "twist": set()})

        def fresh(options, start, kind):
            for offset in range(len(options)):
                choice = options[(start + offset) % len(options)]
                key = choice.get("text") if isinstance(choice, dict) else choice
                if key not in used[kind]:
                    used[kind].add(key)
                    return choice
            return options[start % len(options)]

        effects = event_cfg.get("effects", [{"tags": ["unexpected", "social"]}])
        objective = event_cfg.get("objective") or {}

        def effect_objective(effect):
            effect_tags = {tag.lower() for tag in effect.get("tags", [])}
            score = self.health_player.effective_change(effect.get("health_change", 0)) * (
                objective.get("low_health_healing_weight", 2.0) if self.health_player.is_low
                else objective.get("healing_weight", 0.5)
            )
            score += effect.get("coin_change", 0) * objective.get("coin_weight", 0.1)
            if effect.get("spawn"):
                score += self.genome.discovery_bias + self.genome.voyage_bias
            if "threat" in effect_tags:
                score += self.genome.threat_prob
                if self.health_player.is_low:
                    score -= objective.get("low_health_threat_penalty", 2.0)
            if "social" in effect_tags:
                score += self.genome.bias_social
            if "success" in effect_tags or "recovery" in effect_tags:
                score += self.genome.recovery_bias
            return score

        # Effects rotate (each earlier use lowers an effect's score by
        # effect_repeat_penalty), and coins come from a per-story budget
        # (coin_budget), so events do not keep handing out the same windfall.
        uses = self.__dict__.setdefault("_unexpected_effect_uses", Counter())
        budget = event_cfg.get("coin_budget")
        given = self.__dict__.get("_windfall_coins", 0)
        affordable = [
            (index, candidate) for index, candidate in enumerate(effects)
            if budget is None or candidate.get("coin_change", 0) <= 0 or given + candidate["coin_change"] <= budget
        ] or [(index, candidate) for index, candidate in enumerate(effects) if candidate.get("coin_change", 0) <= 0]
        if not affordable:
            return None
        repeat_penalty = float(event_cfg.get("effect_repeat_penalty", 1.0))
        index, effect = max(
            affordable,
            key=lambda pair: (effect_objective(pair[1]) - uses[pair[0]] * repeat_penalty, str(pair[1])),
        )
        uses[index] += 1

        # The twist is told to match what it does (the effect's own twists, else the generic ones).
        actor = fresh(actors, objective_index, "actor")
        omen = fresh(omens, objective_index + len(self.path), "omen")
        entry = fresh(effect.get("twists") or generic_twists, objective_index + len(self.tags_seen), "twist")
        twist = entry.get("text", "") if isinstance(entry, dict) else str(entry)
        twist = twist.replace("{place}", self.current.name).replace("{actor}", actor)
        if event_cfg.get("name_location", True):
            description = f"In the {self.current.name}, {omen}. {actor.capitalize()} {twist}."
        else:
            description = f"{omen.capitalize()}. {actor.capitalize()} {twist}."

        tags = list(effect.get("tags", ["unexpected"]))
        if "unexpected" not in tags:
            tags.append("unexpected")
        coin_change = int(effect.get("coin_change", 0))
        if coin_change > 0:
            self._windfall_coins = given + coin_change

        raw_health = int(effect.get("health_change", 0))
        health_change = self.health_player.change(-self._scaled_damage(-raw_health) if raw_health < 0 else raw_health)
        self.coins = max(0, self.coins + coin_change)  # a purse cannot go below empty
        self.update_power()
        self.tags_seen.append([t for t in tags if t != "setup_clue"])
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
                self._record_transition("discovery", self.current.name, new_location.name, "spawn")
                self.history.append(self.narrate("unexpected_discovery", location=new_location.name))

        # A mystery leaves a thread to pay off; a bargain is a choice to take or refuse.
        if effect.get("bargain"):
            self._offer_bargain(effect["bargain"], actor, description)
        elif isinstance(entry, dict) and entry.get("payoff"):
            self._plant_twist(actor, twist, entry["payoff"], entry.get("payoff_effect") or {}, planted=True)

        if self.interactive or self.verbose:
            print(f"  >>> {message}")
        return description

    # ---- twist threads: what a twist sets up, a later twist pays off ---------------------

    def _plant_twist(self, actor, text, payoff, payoff_effect, planted=True):
        threads = self.__dict__.setdefault("twist_threads", [])
        thread = {"id": len(threads) + 1, "turn": self.actions_taken, "actor": actor, "place": self.current.name,
                  "text": text, "payoff": payoff, "effect": dict(payoff_effect), "resolved": False}
        threads.append(thread)
        if planted:
            self.__dict__.setdefault("setups_planted", set()).add(f"twist{thread['id']}")
            self._record_clues([f"setup_twist{thread['id']}"], note=f"{actor} {text}")
        return thread

    def _due_twist(self, event_cfg):
        """An open twist thread whose payoff is due: planted unexpected_events.twist_payoff_delay
        turns ago, or any planted one once the finale has begun."""
        delay = int(event_cfg.get("twist_payoff_delay", 6))
        for thread in self.__dict__.get("twist_threads", []):
            if thread["resolved"] or f"twist{thread['id']}" not in self.__dict__.get("setups_planted", set()):
                continue
            if self.actions_taken - thread["turn"] >= delay or self.in_finale():
                return thread
        return None

    def _pay_off_twist(self, thread):
        """Tell a twist's payoff ([TWIST] ...) and apply what it does."""
        thread["resolved"] = True
        actor = thread["actor"]
        text = str(thread["payoff"]).format(actor=actor, actor_cap=actor[:1].upper() + actor[1:], place=thread["place"])
        effect = thread["effect"]
        raw_health = int(effect.get("health_change", 0))
        health_change = self.health_player.change(-self._scaled_damage(-raw_health) if raw_health < 0 else raw_health)
        coin_change = int(effect.get("coin_change", 0))
        self.coins = max(0, self.coins + coin_change)
        self.update_power()
        tags = [t for t in effect.get("tags", []) if not str(t).startswith("payoff_")] + ["unexpected"]
        self.tags_seen.append(tags)
        self.__dict__.setdefault("completed_payoffs", set()).add(f"twist{thread['id']}")
        self._record_clues([f"payoff_twist{thread['id']}"])
        self._last_event_step = self.actions_taken
        self._say("twist_payoff", text=text)
        if health_change or coin_change:
            self.history.append(self.narrate("unexpected_effect", health=health_change, coins=coin_change))
        return text

    def _offer_bargain(self, spec, actor, description):
        """A bargain as a choice (an event of the story's own): taking it pays now and plants a
        debt that comes due later; refusing it closes the matter."""
        from .world import Event

        actor_cap = actor[:1].upper() + actor[1:]
        fmt = lambda text: str(text).format(actor=actor, actor_cap=actor_cap)  # noqa: E731
        thread = self._plant_twist(actor, "a bargain", (spec.get("accept") or {}).get("payoff", "{actor_cap} comes to collect."),
                                   (spec.get("accept") or {}).get("payoff_effect") or {}, planted=False)
        accept, refuse = spec.get("accept") or {}, spec.get("refuse") or {}
        actions = [
            Action(fmt(accept.get("name", "Take the bargain")), [Outcome(
                fmt(accept.get("desc", "You shake on it.")), tags=list(accept.get("tags", ["social", "risk"])),
                coin_change=int(accept.get("coin_change", 0)), health_change=int(accept.get("health_change", 0)),
                setup_id=f"twist{thread['id']}")], source="event", owner="bargain", repeat="always"),
            Action(fmt(refuse.get("name", "Refuse the bargain")), [Outcome(
                fmt(refuse.get("desc", "You refuse.")), tags=list(refuse.get("tags", ["social"])))],
                source="event", owner="bargain", repeat="always"),
        ]
        name = f"_bargain_{thread['id']}"
        self.world.events[name] = Event(name, title=fmt(spec.get("title", "A dangerous bargain")),
                                        tags=["unexpected", "social"], descriptions=[description], actions=actions,
                                        repeat="once")
        if self._pending_event is None:
            self._pending_event = (name, None)  # offered once this turn's action is done

    def _enter_event(self, event_name, source_npc=None, cause="action"):
        """Start an authored Event anchored to the current location, if its rules
        allow it now (_event_blocked). Every way into an event comes through here;
        ``cause`` (action, encounter, chain, effect, random) is logged."""
        if not config.get("authored_events.enabled", True) or self.active_event is not None:
            return False
        event = self.world.events.get(event_name)
        if event is None:
            self.history.append(self.narrate("event_unknown", event=event_name))
            return False
        if self._event_blocked(event) is not None:
            return False

        self.active_event = event
        self.event_return_location = self.current.name
        self.event_source_npc = source_npc if source_npc and not self._is_removed(source_npc) else None
        self.event_description = self._pick_text(event.descriptions) or event.title
        self.event_log.append({
            "event": event.name,
            "location": self.current.name,
            "visit": self.location_counts.get(self.current.name, 0),
            "npc": self.event_source_npc,
            "step": self.actions_taken,
            "cause": cause,
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
        entry = self.event_log[-1]
        entry["action"] = action.name
        entry["succeeded"] = succeeded
        # How it was resolved, for goals and conditions (event_succeeded / event_failed).
        entry["result"] = "abandoned" if action.source == "event_fallback" else ("success" if succeeded else "failure")
        entry["ended_step"] = self.actions_taken
        if self.health_player.is_dead:
            return

        if succeeded and action.triggers_event and self._event_available(action.triggers_event):
            self.history.append(self.narrate("event_chain", title=event.title))
            self._enter_event(action.triggers_event, source_npc=source_npc, cause="chain")
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
                or not self._event_available(char.encounter_event)  # waits until it is enabled
            ):
                continue
            self.encounters.add(self._encounter_key(char))
            message = self.narrate("encounter", npc=char.name)
            self.history.append(message)
            if self.interactive or self.verbose:
                print(f"  >>> {message}")
            if self._enter_event(char.encounter_event, source_npc=char.name, cause="encounter"):
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
        role, settings = self.role_of(npc_name)
        if int(settings.get("importance", 0)) >= 1:
            # Beating someone who matters is a turning point (fitness: npc_triumphs).
            self.__dict__.setdefault("defeated", []).append({"npc": npc_name, "role": role, "turn": self.actions_taken})
            if int(settings.get("importance", 0)) >= 2:
                self._say("npc_triumph", npc=self._called(npc_name), role=settings.get("label", role))
                self.tags_seen.append(["success", "milestone"])
            reward = int(settings.get("defeat_coins", 0))
            if reward:
                self.coins += reward
            self._beat("triumph", npc=npc_name, importance=int(settings.get("importance", 0)))
            self._queue_consequence("defeated", npc_name)
        if self.interactive or self.verbose:
            print(f"  >>> {message}")

    def step(self, action_index=None):
        """Executes one simulation step; the turn is added to ``turn_log``."""
        history_start = len(self.history)
        self._turn = None
        result = self._step(action_index)
        if self._turn is not None:
            turn, self._turn = self._turn, None
            self.__dict__.setdefault("turn_log", []).append({  # games saved before turn_log existed
                **turn,
                "turn": self.actions_taken,
                "location_after": self.current.name,
                "event_after": self.active_event.title if self.active_event else None,
                "health": self.health,
                "coins": self.coins,
                "ended": bool(self.ended),
                "end_kind": self.end_kind,
                "history": [history_start, len(self.history)],  # this turn's lines of the story
            })
            self._update_rhythm(self.turn_log[-1])
        return result

    # ---- the director's sense of rhythm: what is new, what is due ------------------------

    def _update_rhythm(self, turn):
        """Keep the story's rhythm: how many turns in a row brought nothing new (calm_streak)
        and when the last new place was reached. A turn is new when it brings a place, an
        event, a person, an outcome not told before, a skill check, or a change of health or
        coins (as fitness's engagement counts it)."""
        seen = self.__dict__.setdefault("_rhythm_seen", {"places": {self.start_location}, "events": set(),
                                                         "people": set(), "outcomes": set()})
        log = self.turn_log
        previous = log[-2] if len(log) > 1 else None
        new = False
        if turn["location_after"] not in seen["places"]:
            seen["places"].add(turn["location_after"])
            self.last_new_place_turn = self.actions_taken
            new = True
        for event in (turn.get("event"), turn.get("event_after")):
            if event and event not in seen["events"]:
                seen["events"].add(event)
                new = True
        if turn.get("source") in ("npc", "conversation") and turn.get("owner") and turn["owner"] not in seen["people"]:
            seen["people"].add(turn["owner"])
            new = True
        if turn.get("outcome") and turn["outcome"] not in seen["outcomes"]:
            seen["outcomes"].add(turn["outcome"])
            new = True
        if turn.get("check") or (previous is not None and (
                turn["health"] != previous["health"] or turn["coins"] != previous["coins"])):
            new = True
        self.calm_streak = 0 if new else self.__dict__.get("calm_streak", 0) + 1

    def _boredom(self):
        """How far past director.boredom_turns the story has gone with nothing new (0: not bored)."""
        director = self._director()
        if director is None:
            return 0
        return max(0, self.__dict__.get("calm_streak", 0) - int(director.get("boredom_turns", 3)) + 1)

    def _due_clues(self):
        """Open clues planted at least director.payoff_delay turns ago: their payoff is due."""
        director = self._director()
        if director is None:
            return []
        delay = int(director.get("payoff_delay", 6))
        return [tag for tag in self._open_clues() if self.actions_taken - self.setup_indices.get(tag, self.actions_taken) >= delay]

    def _place_promise(self, name):
        """Whether a place still holds something: a step of the quest, the payoff of an open
        clue, an action whose outcomes have not all been told, or someone not yet met there."""
        if name in self._goal_sites() or (self._open_clues() and name in self._payoff_sites()):
            return True
        location = self.world.locations.get(name)
        if location is None:
            return False
        for action in location.actions:
            if self._repeat_rule(action) == "once" and self._is_closed(action, name):
                continue
            if any(o.desc and not self.outcome_counts[o.desc] for o in action.outcomes):
                return True
        met = self.__dict__.get("met", {})
        return any(home == name and npc not in met for npc, home in self.npc_homes.items())

    def _brings_something_new(self, a):
        """Whether an option promises something new: an event, a place not visited, a fight,
        a discovery, or an outcome not told yet (not small talk)."""
        if a.source == "conversation":
            return False
        if a.triggers_event:
            return True
        if a.source == "navigation":
            return self.location_counts.get(a.owner, 0) == 0
        for o in a.outcomes:
            tags = {str(t).lower() for t in o.tags}
            if o.spawn or o.reveal_location or getattr(o, "target_tags", None) or o.reveal_npc or o.reveal_object:
                return True
            if tags & {"threat", "combat", "unexpected"} or (o.desc and not self.outcome_counts[o.desc]):
                return True
        return False

    def _story_draw(self, *keys):
        """0..1, fixed for this story and these keys: the story's own randomness, which never
        rolls its dice (only skill checks do)."""
        salt = self.__dict__.get("_story_salt") or self.start_location  # games saved before
        digest = hashlib.sha256(f"{salt}|{'|'.join(map(str, keys))}".encode()).hexdigest()
        return int(digest[:13], 16) / float(16 ** 13)

    def _fail_forward(self, action, outcome):
        """After a failure that changed nothing but health or coins, start (with chance
        director.fail_forward, from the story's own randomness) a complication: an event
        tagged `complication` that can start here and shares a tag with the failure (a
        botched bribe brings the guards). Not when the player was moved, sent or caught up
        in an event already. Without a complication the story may show another way
        (story_beats._another_way, director.another_way)."""
        director = self._director()
        chance = float(director.get("fail_forward", 0.0)) if director else 0.0
        if (director is None or self.active_event or self._pending_event or self._location_is_locked()
                or (outcome is not None and (outcome.move_to or outcome.send_to or outcome.spawn
                                             or outcome.reveal_location or list(outcome.effects.writes)))
                or self.path[-1] != self.current.name or self.ended):
            return False
        failed = {str(t).lower() for t in (outcome.tags if outcome is not None else [])} | {
            str(t).lower() for o in action.outcomes for t in o.tags}
        candidates = sorted(name for name, event in self.world.events.items()
                            if "complication" in event.tags and (set(event.tags) - {"complication"}) & failed
                            and self._event_available(name)) if chance > 0 else []
        if not candidates or self._story_draw("fail_forward", self.actions_taken) >= chance:
            return self._another_way(action)  # no complication: another way forward, or a dead end
        self._pending_event = (candidates[0], action.character_name)
        return True

    def _storyteller_pick(self, actions):
        """The index of the option the storyteller takes: its favourite, or now and then
        (director.surprise x (1 + chaos_bias), drawn from the story's own randomness) another
        option nearly as good (within director.surprise_margin of the best, among its
        surprise_top_k), so that one start and one storyteller can tell different stories.
        A story is still the same for the same seed."""
        director = self._director()
        if director is None or len(actions) < 2:
            return 0
        chance = float(director.get("surprise", 0.0)) * (1.0 + self.genome.chaos_bias)
        if chance <= 0 or self._story_draw("surprise", self.actions_taken) >= chance:
            return 0
        pairs = self._rank_actions(actions)
        best = pairs[0][0]
        margin = float(director.get("surprise_margin", 1.0))
        top_k = max(2, int(director.get("surprise_top_k", 3)))
        last = (self.__dict__.get("turn_log") or [{}])[-1].get("action")
        previous = self.path[-2] if len(self.path) > 1 else None
        # A surprise is something else: not the last action again, not straight back where one came from.
        near = [a for score, a in pairs[1:top_k] if best - score <= margin and a.name != last
                and not (a.source == "navigation" and a.owner == previous)]
        if not near:
            return 0
        chosen = near[int(self._story_draw("pick", self.actions_taken) * len(near))]
        return next(index for index, a in enumerate(actions) if a is chosen)

    def turn_text(self, entry):
        """The story text of one ``turn_log`` entry."""
        start, end = entry["history"]
        return "\n".join(line.strip("\n") for line in self.history[start:end]).strip()

    def _step(self, action_index=None):
        if self.ended:
            return False

        actions = self.ranked_available_actions()
        if not actions:
            return False

        in_event = self.active_event is not None
        self._announce_finale()
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
                action_index = self._storyteller_pick(actions)  # a player's choice is taken as given
            if action_index < 0 or action_index >= len(actions):
                raise IndexError(f"Action index {action_index} is out of range.")
            action = actions[action_index]

        # Record beginning of story segment
        is_new_loc = self.current.name != self.last_location_name
        self.last_location_name = self.current.name
        self.history.append(self.narrate("action_taken", action=action.name))
        choice_scope = self._action_scope(action)  # before the action can move the player
        choice_visit = self._visit_id()
        if self._repeat_rule(action) == "per_visit":
            self.visit_actions_performed.add(action.name)
        
        # Outcomes and their transitions are selected by the objective function.
        # An action with its own skill_check rolls first and then picks among
        # the outcomes written for that result; otherwise the chosen outcome's
        # tags may call for a check (character_stats.tag_checks).
        location_before = self.current.name
        climax_turn = in_event and self.active_event.name == self._climax_state()["armed"]
        attempt, told, check_result, calculated_success = self._resolve_outcome(action)
        if attempt is None:
            return False
        self._turn = {
            "action": action.name,
            "source": action.source,
            "owner": action.owner,
            "location_before": location_before,
            "event": self.active_event.title if self.active_event else None,
            "check": vars(check_result).copy() if check_result is not None else None,
            "succeeded": bool(calculated_success),
            "outcome": told.desc if told is not None else None,
            # What the turn was about: the outcome told, and the event it happened in.
            # What else the player could have done (the best alternatives, for a reader of the story).
            "options": [option.name for option in actions[:6] if option is not action][:4],
            "tags": sorted({str(tag) for tag in (told.tags if told is not None else [])}
                           | {str(tag) for tag in (self.active_event.tags if self.active_event else [])}
                           | ({"failure"} if not calculated_success else set())),
        }
        if getattr(action, "fast_travel", None):
            self._turn["fast_travel"] = True  # not counted toward the action limit (travel)
            self.free_turns = self.__dict__.get("free_turns", 0) + 1
        self.actions_taken += 1
        shown = told or attempt
        is_conversation = "conversation" in {tag.lower() for tag in shown.tags}
        repeated_talk = is_conversation and self.outcome_counts[shown.desc] > 0  # told before: nothing new
        if (not is_conversation or repeated_talk) and not in_event:
            self.consecutive_steps_in_location += 1
        
        # Reset stay counter if we move
        if told is not None and told.move_to:
            self.consecutive_steps_in_location = 0

        if calculated_success:
            if self.interactive:
                print(f"\nRESULT: {told.desc}")
            self.succeeded_actions |= _action_keys(action.name)
            self.succeeded_outcome_tags.update(tag.lower() for tag in told.tags)
            self.apply(told, action=action)
        elif told is not None:
            # The attempt failed and the action has a failure branch: tell it.
            self.apply(told, action=action)
        else:
            if self.verbose:
                print(f"  FAILED: {action.name}")
            attempt_tags = {tag.lower() for tag in attempt.tags}
            guard_failure = self._is_guard_failure(action, attempt)
            dangerous = guard_failure or bool(attempt_tags & {"combat", "threat"})
            self.health_player.damage(self._scaled_damage(self.combat.failure_penalty(dangerous, self.inventory)))
            self.update_power()
            self.tags_seen.append("failure")
            
            # Narrative Consequence: Failure against guards leads to arrest
            if guard_failure:
                arrest_location = config.get("game_state.arrest_location", "Dungeon")
                arrest_msg = self.narrate("failure_arrest", action=action.name, location=arrest_location)
                self.history.append(arrest_msg)
                if self.interactive or self.verbose:
                    print(f"  >>> {arrest_msg}")
                # Arrested: sent there one way. Only the arrest is told, never the
                # text (or the effects) of the success that was attempted.
                self.apply(Outcome("", tags=sorted(attempt_tags | {"failure"}), send_to=arrest_location), action=action)
            else:
                fail_msg = self.narrate("failure", action=action.name)
                self.history.append(fail_msg)
                if self.interactive or self.verbose:
                    print(f"  >>> {fail_msg}")
                if self._finalize_if_ended():
                    return False

        self._record_choice(action, calculated_success, choice_scope, choice_visit)
        if calculated_success and action.source not in ("navigation", "conversation"):
            self.__dict__.setdefault("success_counts", Counter())[action.name] += 1
        self._update_alert(action, told if told is not None else attempt, calculated_success)
        if action.source == "navigation" and self.current.name != location_before \
                and self.location_counts.get(self.current.name, 0) == 1:
            self._first_trip(self.current.name)
        director = self._director()
        # Failing forward: a failed attempt that led nowhere (no move, discovery or event) may turn
        # into a complication: an authored event tagged `complication` whose tags fit the failure.
        if not calculated_success and not in_event and director is not None:
            self._fail_forward(action, told if told is not None else attempt)
        # Failed attempts are remembered (director.retry_bonus, retry_cooldown); a success clears them.
        attempts = self.__dict__.setdefault("failed_attempts", Counter())
        if calculated_success:
            attempts.pop(action.name, None)
        elif action.source not in ("navigation", "conversation"):
            attempts[action.name] += 1
            self.__dict__.setdefault("_last_failure", {})[action.name] = self.actions_taken
        if not calculated_success and director is not None:
            until = self.actions_taken + int(director.get("barred_turns", 5))
            barred = self.__dict__.setdefault("_barred", {})
            for outcome in action.outcomes:
                if outcome.move_to in self.world.locations and not rules.is_failure(outcome, set(rules.settings()["failure_tags"])):
                    barred[outcome.move_to] = until

        # Any choice inside an event ends it; a successful trigger action starts one.
        if in_event:
            self._finish_event(action, calculated_success)
            if climax_turn:
                self._resolve_climax(action, calculated_success)
        elif (
            calculated_success and action.triggers_event and self.health_player.is_alive
            and self._event_available(action.triggers_event)
        ):
            self._enter_event(action.triggers_event, source_npc=action.character_name)
        self._start_pending_event()  # a start_event effect of the outcome
        if calculated_success or not self._escape_action(action):
            pass
        elif self._location_is_locked():
            self._escape_attempts = getattr(self, "_escape_attempts", 0) + 1  # the next try is easier
        self._release_if_held_too_long()
        self._move_on_if_stalled()

        if self._finalize_if_ended():
            return False
        self._start_pending_event()  # a start_event in the mission's rewards
        # A hostile NPC at the destination confronts the player on arrival.
        if not self.active_event:
            self._tell_consequences()  # what the player did earlier shows now
            self._refresh_scene()
            self._start_encounter()
        return True

    def resolve_destination(self, move_to=None, move_to_tags=None):
        """Objective-driven selection of a target location among candidates."""
        if move_to and move_to in self.world.locations:
            return move_to

        if not move_to_tags:
            return None

        # Led by tags: on a map, the neighbour that fits them best.
        neighbours = self.neighbours(self.current.name)
        candidates = sorted(n for n in neighbours if not self.sent_only(n)) if neighbours is not None else None
        if candidates == []:
            return None
        target = self._best_transition_destination(candidate_names=candidates, intent_tags=move_to_tags)
        return target.name if target else None

    def discover_new_node(self, node_type=None, source_npc=None):
        """Genome-driven discovery of hidden inhabitants or objects, prioritized by source NPC's knowledge."""
        if not node_type:
            # Voyage bias increases the chance of discovering a location lead
            nodes = config.get("discovery_nodes") or {}
            node_scores = {
                "loc": nodes.get("location_base", 0.3) + self.genome.voyage_bias * nodes.get("location_voyage_weight", 0.4),
                "npc": nodes.get("npc_base", 0.2) + self.genome.bias_social * nodes.get("npc_social_weight", 0.3),
                "obj": nodes.get("item_base", 0.2) + self.genome.discovery_bias * nodes.get("item_discovery_weight", 0.3),
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
                        and l in self.world.locations and self.is_enabled(self.world.locations[l])
                        and not self.sent_only(l)
                    ]
                    # What borders this place first; a far place is told as the way toward it.
                    hidden_locs = [l for l in hidden_locs if self._adjacent(self.current.name, l)] or [
                        l for l in hidden_locs if self._way_toward(l) is not None]
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

    def _record_transition(self, kind, origin, target, resolution):
        """One move or discovery of the story (transition_records)."""
        self.__dict__.setdefault("transition_records", []).append({
            "turn": self.actions_taken, "kind": kind, "from": origin, "to": target, "resolution": resolution})

    def _move_to(self, target_name, one_way=False, cause=None):
        """Helper to transition to a new location and update history/counters.

        ``one_way`` (a ``send_to`` outcome) links the origin to the target but not
        back, and confines the player there when the target requires an escape.
        """
        if target_name == self.current.name and not one_way:
            return False  # already here: no move (and no second entry in the path)
        if target_name != self.current.name and self._location_is_locked():
            return False
        if target_name in self.world.locations and not self.is_enabled(self.world.locations[target_name]):
            return False  # switched off by a persistent variable
        if not one_way and target_name != self.current.name and target_name in self.world.locations \
                and not (self._adjacent(self.current.name, target_name) and not self.sent_only(target_name)):
            # The map holds: a far place is reached through the places between, one at a
            # time (being sent, e.g. an arrest, is the only jump).
            path = self._route_path(self.current.name, target_name)
            if not path:
                return False
            self._say("route_via", origin=self.current.name, destination=target_name, via=path[0])
            self._learn_way(path)
            self._set_heading(target_name)
            target_name = path[0]
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
        if getattr(self, "heading", None) == target_name:
            self.heading = None  # arrived where the story set out for
        if one_way:
            origin.connect_one_way(target)
            rule = config.get("location_constraints", {}).get(target_name, {})
            if rule.get("requires_escape", False):
                self.confined_in = target_name
                self.unlocked_locations.discard(target_name)  # every arrest needs a new escape
                self._escape_attempts = 0
        else:
            origin.connect(target)
        self.last_location_name = origin.name
        self.current = target
        self._record_transition("move", origin.name, target.name,
                                "sent" if one_way else cause or getattr(self, "_transition_cause", None) or "named")
        self.location_counts[self.current.name] = self.location_counts.get(self.current.name, 0) + 1
        self.path.append(self.current.name)
        self.consecutive_steps_in_location = 0
        self.visit_actions_performed = set()
        return True

    def apply(self, outcome, action=None):
        """Applies an action's outcome to the game state."""
        # Validated before anything changes: a bad effect list leaves the story as it was.
        outcome_effects = self._checked_effects(outcome.effects)
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
        open_before = set(self._open_clues())
        self._record_clues(outcome.tags, note=outcome.desc)
        # Named threads: a setup is planted; a payoff closes it (its setup's outcomes gate on it).
        if getattr(outcome, "setup_id", None):
            self.__dict__.setdefault("setups_planted", set()).add(str(outcome.setup_id))
            self._record_clues([f"setup_{outcome.setup_id}"], note=outcome.desc)
        if getattr(outcome, "payoff_id", None):
            self.__dict__.setdefault("completed_payoffs", set()).add(str(outcome.payoff_id))
            self._record_clues([f"payoff_{outcome.payoff_id}"])
        self._tell_callbacks(open_before - set(self._open_clues()), outcome.desc or "")
        if outcome.desc:  # an arrest after a failed attempt tells no outcome text
            self.outcome_counts[outcome.desc] += 1
            self.__dict__.setdefault("scenario_counts", Counter())[
                (origin_location, action.name if action else "", outcome.desc)] += 1
            self.history.append(self._told(outcome, action))  # variants, or said to be told again (variation)

        # 0. Apply Outcome Mechanical Changes. The effect list holds the outcome's
        # variable and stat changes (shorthand fields) and its explicit effects.
        self._commit_effects(outcome_effects, npc=action.character_name if action else None)
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
        if getattr(outcome, "unlock_exit", False) and origin_location not in self.unlocked_locations:
            self.unlocked_locations.add(origin_location)
            if self.confined_in == origin_location:
                self.confined_in = None
            self._say("exit_unlocked", location=origin_location)
        self._change_health(outcome.health_change, mitigate="combat" in lower_tags or "threat" in lower_tags)
        self.coins = max(0, self.coins + outcome.coin_change)  # a purse cannot go below empty
        self.update_power()
        if self.interactive or self.verbose:
            print(f"  >>> Health: {self.health_player.status()} | Coins: {self.coins} | Power: {self.power:.2f}")

        # 1. Reveal and spawn logic. A discovery leads where the outcome's text says
        # (story_rules.follow_outcome_text): "hidden in the Vault" opens the Vault.
        story_rules = rules.settings()
        named = rules.mentioned_location(outcome.desc, self.world.locations) if story_rules["follow_outcome_text"] else None
        if named == origin_location or (named and not self.is_enabled(self.world.locations[named])):
            named = None
        target_tags = list(getattr(outcome, "target_tags", ()) or ())
        if target_tags and not outcome.reveal_location:
            named = None  # led by tags, not by a name in the text
        discovery_known = bool(outcome.spawn and not outcome.reveal_location and named in self.current.connected)
        move_cause = "travel" if action is not None and action.source == "navigation" else "named"
        if (outcome.spawn or outcome.reveal_location or target_tags) and not discovery_known:
            target = outcome.reveal_location or (named if outcome.spawn else None)
            resolution = "named" if target else "tags" if target_tags else "spawn"
            named_target = target
            target = self._lead_toward(target)  # a far place opens the way toward it
            new_loc = None if named_target and target is None else self.world.spawn(
                self.current,
                force_target=target,
                state=self,
                allow_special=(target == "Dungeon"),
                target_tags=None if target else target_tags,
            )
            if new_loc and new_loc.name != self.current.name and not (
                story_rules["follow_outcome_text"] and new_loc.name in self.current.connected
            ):
                self.current.connect(new_loc)
                self._record_transition("discovery", self.current.name, new_loc.name, resolution)
                spawn_msg = self.narrate("spawn", location=new_loc.name)
                self.history.append(spawn_msg)
                if self.interactive or self.verbose:
                    print(f"  >>> {spawn_msg}")
                # Deterministic and genome-driven (no dice roll): follow the new
                # path only when it is compelling enough for this genome.
                if (
                    outcome.move_to is None
                    and not event_anchored
                    and not self.__dict__.get("player_directed", False)  # the player chooses where to go
                    and config.get("game_state.auto_travel_on_spawn", True)
                    and self._should_commit_to_new_path(
                        new_loc.name, config.get("game_state.auto_travel_on_spawn_threshold", 1.5),
                    )
                ):
                    outcome.move_to = new_loc.name
                    move_cause = resolution
                    travel_msg = self.narrate("voyage", location=new_loc.name)
                    self.history.append(travel_msg)
                    if self.interactive or self.verbose:
                        print(f"  >>> {travel_msg}")

        if outcome.reveal_npc:
            self._reveal_npc(outcome.reveal_npc)
        if outcome.reveal_object:
            self._reveal_item(outcome.reveal_object)

        if outcome.remove_npc:
            removed_name = action.character_name if outcome.remove_npc is True and action else outcome.remove_npc
            if isinstance(removed_name, str):
                self._remove_npc(removed_name)

        # 2. NPC-driven leads can unlock travel even if the action itself does not move.
        # With follow_outcome_text, an outcome that routes by itself gives no extra
        # lead, and one whose text names a place leads there (or nowhere new).
        hostile = action is not None and any(
            c.name == action.character_name and c.encounter_event for c in self.world.characters)
        # Someone who just ambushed you gives no directions.
        if action and action.character_name and not self._is_removed(action.character_name) and not hostile:
            c_name = action.character_name
            every = max(1, int(config.get("npc_leads.every_nth_interaction", 2)))
            routes_itself = bool(
                outcome.spawn or outcome.reveal_location or outcome.move_to or outcome.send_to
                or outcome.move_to_tags or outcome.lead_to_known
                or any(kind == "location" for kind, _ in outcome.effects.writes)
            )
            lead_due = self.character_interactions[c_name] % every == 0 or "success" in outcome.tags or "social" in outcome.tags
            if "failure" in lower_tags:
                lead_due = False  # someone who just refused or beat you gives no directions
            if story_rules["follow_outcome_text"] and (routes_itself or named):
                lead_due = bool(named) and not routes_itself and named not in self.current.connected
            if lead_due and not named and not routes_itself and self._name_hidden_npc(c_name):
                lead_due = False  # the lead was about someone who matters
            if lead_due:
                if story_rules["follow_outcome_text"] and named:
                    node_type, node_name = "loc", named
                else:
                    node_type, node_name = self.discover_new_node(source_npc=c_name)
                if node_type == "loc":
                    step = self._lead_toward(node_name)  # a far place: the way toward it
                    new_loc = None if node_name and step is None else self.world.spawn(
                        self.current, force_target=step, state=self)
                    if new_loc and new_loc.name != self.current.name:
                        self.current.connect(new_loc)
                        self._record_transition("discovery", self.current.name, new_loc.name, "lead")
                        lead_msg = self.narrate("lead_path", npc=c_name, location=new_loc.name)
                        self.history.append(lead_msg)
                        if self.interactive or self.verbose:
                            print(f"  >>> {lead_msg}")
                        if (
                            "conversation" not in lower_tags
                            and outcome.move_to is None
                            and not event_anchored
                            and not self.__dict__.get("player_directed", False)  # the player chooses where to go
                            and self._should_commit_to_new_path(new_loc.name)
                        ):
                            outcome.move_to = new_loc.name
                            move_cause = "lead"

        # 3. Handle Regular Movement (a one-way send_to wins over everything else)
        if outcome.send_to:
            if self._move_to(outcome.send_to, one_way=True):
                sent_msg = self.narrate("sent", location=outcome.send_to)
                self.history.append(sent_msg)
                if self.interactive or self.verbose:
                    print(f"  >>> {sent_msg}")

        elif outcome.move_to and getattr(action, "fast_travel", None):
            self._fast_travel(action.fast_travel)  # a known way walked in one turn
        elif outcome.move_to:
            target_name = self.resolve_destination(move_to=outcome.move_to)
            if target_name:
                self._move_to(target_name, cause=move_cause)

        elif outcome.move_to_tags:
            target_name = self.resolve_destination(move_to_tags=outcome.move_to_tags)
            if target_name:
                self._move_to(target_name, cause="tags")

        # 4. Handle Guided Movement (NPC Lead)
        # The outcome's own move wins: a lead does not take the player back where the turn started.
        if getattr(outcome, 'lead_to_known', False) and action and action.character_name and self.current.name == origin_location:
            char = next((c for c in self.world.characters if c.name == action.character_name), None)
            if char and char.known_locations:
                candidates = [
                    l for l in char.known_locations
                    if l in self.world.locations and l not in (self.current.name, origin_location)
                    and self.is_enabled(self.world.locations[l]) and self._way_toward(l) is not None
                ]
                # Someone leads you next door; a far place they know is reached through the places between.
                candidates = [l for l in candidates if self._adjacent(self.current.name, l)] or candidates
                if candidates:
                    scored = []
                    for loc_name in candidates:
                        loc = self.world.locations[loc_name]
                        score = (
                            config.get("npc_leads.base_score", 1.0)
                            + len(loc.tags & char.associated_tags) * config.get("npc_leads.shared_tag_weight", 0.5)
                            + semantic_tag_similarity(loc.tags, char.associated_tags)
                        )
                        if "social" in loc.tags: score += self.genome.bias_social
                        if "urban" in loc.tags: score += self.genome.bias_urban
                        scored.append((score, loc_name))
                    scored.sort(key=lambda x: x[0], reverse=True)
                    target = self._lead_toward(scored[0][1])  # the next place on the way to it
                    
                    self.history.append(self.narrate("lead_move", npc=char.name, location=target))
                    if self.interactive: print(f" >>> {char.name} leads the way to the {target}...")
                    self._move_to(target, cause="lead")

        # 5. Fleeing back the way one came (return_to_previous), unless the outcome moved already.
        if getattr(outcome, "return_to_previous", False) and self.current.name == origin_location:
            previous = self.path[-2] if len(self.path) > 1 else None
            if previous and previous != origin_location and self._move_to(previous, cause="back"):
                self._say("fled_back", location=previous)

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
                self._refund(action, outcome)  # a failed paid attempt returns part of the price
            
            # A failed attempt to take something (or an arrest for it) does not get it.
            if action.collects_object and not rules.is_failure(outcome, set(rules.settings()["failure_tags"])):
                self._give_item(action.collects_object, how="collected")
            if action.consumes_object and action.active_item:
                self._take_item(action.active_item, how="used")
        self._advance_npc_goals(outcome, action)
        if action is not None and action.character_name:
            self._check_secret(action.character_name)  # spoken with often enough, they let something slip

    # ---- NPC goals: what people want, and what helping them earns ----------------------------

    @staticmethod
    def _stem(word):
        word = str(word).lower()
        return word[:5] if len(word) > 5 else word.rstrip("s")

    def _goal_specs(self, char):
        specs = getattr(char, "goal_specs", None)
        if specs is None:  # NPCs built by hand or saved before goal specs
            from .builder import npc_goals

            specs = char.goal_specs = npc_goals(char.goals)
        return specs

    def _npc_goal_words(self, outcome, action):
        text = " ".join([action.name if action else "", outcome.desc or ""] + [str(t) for t in outcome.tags])
        return {self._stem(word) for word in re.findall(r"[a-zA-Z]+", text) if len(word) > 2}

    def _helps_goal(self, char, outcome, action):
        """Index of the goal of ``char`` this outcome serves, or None."""
        if char is None or char.name in self.__dict__.get("allies", set()):
            return None
        words = self._npc_goal_words(outcome, action)
        for index, goal in enumerate(self._goal_specs(char)):
            if {self._stem(keyword) for keyword in goal["keywords"]} & words:
                return index
        return None

    def _advance_npc_goals(self, outcome, action):
        """Helping someone with what they want (an action with them whose outcome serves one
        of their goals, not a failure, not small talk) counts; after npc_goals.progress_needed
        times (or the goal's own `needed`) they become an ally."""
        if action is None or not action.character_name or action.source == "conversation":
            return
        if rules.is_failure(outcome, set(rules.settings()["failure_tags"])) or "failure" in outcome.tags:
            return
        char = next((c for c in self.world.characters if c.name == action.character_name), None)
        if char is None or char.encounter_event:
            return
        index = self._helps_goal(char, outcome, action)
        if index is None:
            return
        progress = self.__dict__.setdefault("npc_goal_progress", {}).setdefault(char.name, {})
        progress[index] = progress.get(index, 0) + 1
        goal = self._goal_specs(char)[index]
        cfg = config.get("npc_goals") or {}
        if progress[index] >= int(goal.get("needed") or cfg.get("progress_needed", 2)):
            self._fulfil_npc_goal(char, goal)

    def _fulfil_npc_goal(self, char, goal):
        cfg = config.get("npc_goals") or {}
        self.__dict__.setdefault("allies", set()).add(char.name)
        self._say("npc_goal_done", npc=self._called(char.name), goal=goal["text"][:1].lower() + goal["text"][1:])
        self.tags_seen.append(["social", "success"])
        self._queue_consequence("ally", char.name)
        reward = goal.get("reward") or cfg.get("reward", "lead")
        if reward == "lead":
            known = [name for name in char.known_locations if name in self.world.locations and name != self.current.name
                     and name not in self.current.connected and self.is_enabled(self.world.locations[name])]
            if known:
                self._open_route(sorted(known, key=lambda name: (self.location_counts.get(name, 0), name))[0])
        elif reward == "item":
            here = [o.name for o in getattr(self, "location_objects", [])
                    if o.name in char.known_objects and o.name not in self.visible_objects]
            if here:
                self._reveal_item(here[0])

    def _check_bonus(self, action, stat=None, tags=()):
        """What helps a check besides the stat: Power in a fight (power_model.check_weight per
        point of Power gained since the story began: a weapon or full health helps, wounds
        hurt) and an ally's trust (npc_goals.ally_check_bonus)."""
        bonus = 0
        power_cfg = config.get("power_model") or {}
        fighting = stat in set(power_cfg.get("combat_stats", ["attack", "defence"])) or bool(
            {str(tag).lower() for tag in tags} & {"combat", "threat"})
        if fighting:
            start = self.__dict__.get("start_power", self.power)  # games saved before: no change
            bonus += int(round((self.power - start) * float(power_cfg.get("check_weight", 6))))
        if action is not None and action.character_name in self.__dict__.get("allies", set()):
            bonus += int((config.get("npc_goals") or {}).get("ally_check_bonus", 15))
        opponent = self._opponent(action)
        if opponent:
            # Someone who matters is harder to beat, a bystander easier (npc_roles.<role>.check_modifier).
            bonus += int(self.role_of(opponent)[1].get("check_modifier", 0))
        # What worked before is harder now (variation), and a watch on edge is harder to get past (stakes).
        bonus -= self._repeat_penalty(action) + self._alert_check_penalty(action, tags)
        return bonus

    def _opponent(self, action):
        """The NPC an action is taken against or with: its own NPC, or the one behind the event."""
        if action is None:
            return None
        if action.character_name:
            return action.character_name
        return self.event_source_npc if action.source == "event" and self.active_event is not None else None

    def check_hint(self, action):
        """The check an option involves, for the player to see before choosing: {stat,
        chance (0-100), bonus (Power, an ally, the NPC's role), risk (failure_cost: what
        failing costs, or None)}; None for an option without a check."""
        failure_tags = set(rules.settings()["failure_tags"])
        check = self.combat.check_for(action)
        outcome = None
        if not check:
            for candidate in action.outcomes:
                if not rules.is_failure(candidate, failure_tags) and self.combat.check_for(action, candidate):
                    outcome, check = candidate, self.combat.check_for(action, candidate)
                    break
        if not check:
            return None
        tags = [t for o in action.outcomes for t in o.tags]
        bonus = self._check_bonus(action, check[0], tags)
        chance = self.combat.pass_chance(check[0], check[1] + bonus, inventory=self.inventory, step=self.actions_taken)
        # What failing costs, seen before choosing (stakes.show_failure_costs).
        return {"stat": check[0], "chance": int(round(chance * 100)), "bonus": bonus, "risk": self.failure_cost(action)}

    def get_full_story(self):
        """Returns a string representation of the full story transcript."""
        return "\n".join(self.history)
