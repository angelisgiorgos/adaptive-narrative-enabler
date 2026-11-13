import random
from collections import defaultdict
from utils.config_loader import config
from utils.tag_similarity import semantic_tag_similarity

from .conditions import Condition, ConditionError, Conditional, goal_condition
from .effects import EffectList

# ============================
# WORLD GRAPH / CHARACTERS
# ============================

class DialogueLine(Conditional):
    """Something an NPC says when the player talks with them. The first line of
    the NPC's ``dialogue`` whose ``enabled_if`` holds is said; its ``effects``
    (effects.py) apply when it is said. ``text`` may list variants."""
    def __init__(self, text, enabled_if=None, effects=None):
        self.text = text
        self.enabled_if = enabled_if
        self.effects = EffectList.of(effects)


class Character(Conditional):
    """An NPC. A unique NPC lives in a single location for the whole game;
    a generic NPC (e.g. a guard) may appear in every location it fits.

    ``encounter_event`` names an Event that starts by itself when the NPC is
    present in the scene (e.g. a bandit ambush), once per game, or on every
    visit to a location when ``encounter_repeat`` is set. ``dialogue`` lists
    DialogueLines chosen by condition when the player talks with the NPC."""
    role = None     # class defaults: games saved before these fields load unchanged
    secret = None

    def __init__(self, name, associated_tags, goals=None, 
                 descriptions=None, actions=None, 
                 known_locations=None, known_objects=None, unique=False,
                 encounter_event=None, encounter_repeat=False, enabled_if=None, dialogue=None, article=None,
                 role=None, secret=None):
        self.name = name
        # The NPC's part in the story (npc_roles.roles), None: inferred from tags; and a
        # secret the story can reveal ({text, talks, clues, tags, effects}).
        self.role = role
        self.secret = dict(secret) if secret else None
        # How the story names the NPC: "" for a proper name (Aurora), "the" / "a" / "an";
        # None: "the" for a unique NPC, "a"/"an" for a generic one.
        self.article = article
        self.enabled_if = enabled_if  # a condition (conditions.py)
        self.dialogue = list(dialogue or [])
        self.unique = bool(unique)
        self.encounter_event = encounter_event or None
        self.encounter_repeat = bool(encounter_repeat)
        self.associated_tags = set(associated_tags)
        self.goals = goals or []
        self.descriptions = descriptions or []
        self.actions = actions or []
        self.known_locations = known_locations or []
        self.known_objects = known_objects or []

class Object(Conditional):
    """An item definition.

    A generic item (the default) is shown wherever it fits, so each location
    can hold its own copy. A ``unique`` item exists once per story: GameState
    gives it one ItemInstance, placed at ``location`` (or the location it fits
    best), shown only where that instance lies, and gone for good once used.
    """
    def __init__(self, name, associated_tags, interact_prompts=None, 
                 goals=None, descriptions=None, actions=None, collectible=False,
                 enabled_if=None, stat_bonus=None, unique=False, location=None):
        self.name = name
        self.unique = bool(unique)
        self.location = location or None  # where the unique item's instance is placed
        self.stat_bonus = dict(stat_bonus or {})  # stat -> bonus while the item is held
        self.enabled_if = enabled_if
        self.associated_tags = set(associated_tags)
        self.interact_prompts = interact_prompts or []
        self.goals = goals or []
        self.descriptions = descriptions or []
        self.actions = actions or []
        self.collectible = collectible

class ItemInstance:
    """The one copy of a unique item in a story, apart from its definition.

    ``state`` is where it is: ``placed`` (lying at ``location``), ``held`` (in
    the inventory), ``used`` (consumed: gone for good) or ``unplaced`` (no
    location fits; only an action or an effect can hand it over).
    """
    def __init__(self, item, location):
        self.item = item
        self.location = location
        self.state = "placed" if location else "unplaced"

    @property
    def available(self):
        """Whether it can still be collected or received (not held, not used)."""
        return self.state in ("placed", "unplaced")

    def __repr__(self):
        return f"ItemInstance({self.item!r}, {self.state}" + (f" at {self.location!r})" if self.location else ")")


class Location(Conditional):
    """A place in the world.

    ``start_eligible`` lets a story begin here; ``start_weight`` is its relative
    chance among the eligible locations (see core_engine/starts.py)."""
    exit_locked = False   # class defaults: games saved before these fields load unchanged
    template = None
    parameters = {}

    def __init__(self, name, tags, interact_prompts=None, goals=None,
                 distant_descriptions=None, entered_descriptions=None, enabled_if=None,
                 start_eligible=False, start_weight=1.0, exit_locked=False, template=None, parameters=None):
        self.name = name
        # Its exits stay locked until an outcome with `unlock_exit` (or an escape) opens them.
        self.exit_locked = bool(exit_locked)
        # The scenario template it was made from and its parameters (builder.expand_templates).
        self.template = template
        self.parameters = dict(parameters or {})
        self.enabled_if = enabled_if
        self.start_eligible = bool(start_eligible)
        self.start_weight = float(start_weight)
        self.tags = set(tags)
        self.interact_prompts = interact_prompts or []
        self.goals = goals or []
        self.distant_descriptions = distant_descriptions or []
        self.entered_descriptions = entered_descriptions or []
        self.actions = []
        self.connected = set()

    def connect(self, other):
        """A route both ways (a place never leads to itself); True when made."""
        if other is None or other.name == self.name:
            return False
        self.connected.add(other.name)
        other.connected.add(self.name)
        return True

    def connect_one_way(self, other):
        """A route from here to ``other`` only; ``other`` does not lead back."""
        if other is None or other.name == self.name:
            return False
        self.connected.add(other.name)
        return True


class Event(Conditional):
    """A self-contained situation entered by taking an action that triggers it.

    While an event is active its actions replace every location, NPC, item and
    travel action. NPCs and items never spawn inside an event. When the player
    picks one of its actions the event ends and the player is back at the
    location where it began, unless that action's outcome moved them elsewhere.

    When it may start (GameState._event_blocked, enforced for every way in):
      enabled_if       a condition that must hold
      repeat           once (unique in the story), per_location, per_visit or always
      max_occurrences  at most this many times per story (None: no limit)
      cooldown         turns after it ended before it can start again
    How it starts: ``trigger: action`` (something in the story starts it: a
    trigger action, an NPC encounter, a chain or a start_event effect), or
    ``trigger: random``, which may also start it by itself: each turn it can
    start, it does with probability ``chance``; ``weight`` picks between random
    events that hit in the same turn.
    """
    REPEAT = ("once", "per_location", "per_visit", "always")
    TRIGGERS = ("action", "random")

    def __init__(self, name, title=None, tags=None, descriptions=None, actions=None, enabled_if=None,
                 trigger="action", chance=None, weight=1.0, cooldown=0, max_occurrences=None, repeat="always"):
        self.name = name
        self.enabled_if = enabled_if
        self.trigger = trigger or "action"
        self.chance = chance
        self.weight = float(weight)
        self.cooldown = int(cooldown or 0)
        self.max_occurrences = max_occurrences
        self.repeat = repeat or "always"
        if self.trigger not in self.TRIGGERS or self.repeat not in self.REPEAT:
            raise ValueError(f"Event '{name}': trigger must be one of {', '.join(self.TRIGGERS)} "
                             f"and repeat one of {', '.join(self.REPEAT)}.")
        self.title = title or name
        self.tags = set(tags or [])
        self.descriptions = descriptions or []
        self.actions = actions or []


class Mission(Conditional):
    """A goal the player must achieve to win. One mission is chosen per story.

    Every condition in ``goal`` must hold for the mission to be complete. The
    goal keys are shorthand for conditions (conditions.goal_condition), and
    ``condition`` takes any condition tree. ``rewards`` (effects.py) apply
    when the mission is completed.
    """
    GOAL_KEYS = ("action", "location", "item", "event", "event_succeeded", "npc_removed", "outcome_tag", "coins",
                 "variables", "condition")

    def __init__(self, name, goal, title=None, description="", weight=1.0, enabled_if=None, rewards=None, stages=None):
        # Stages: a quest in steps, each {title, description, goal, rewards}, completed in
        # order; the last one completes the mission (its goal is the mission's goal).
        self.stages = []
        for number, stage in enumerate(stages or [], start=1):
            stage_goal = dict((stage or {}).get("goal") or {})
            bad = sorted(set(stage_goal) - set(self.GOAL_KEYS))
            if not stage_goal or bad:
                raise ValueError(f"Mission '{name}' stage {number} needs a goal using only: {', '.join(self.GOAL_KEYS)}")
            try:
                stage_conditions = {key: Condition(goal_condition(key, value)) for key, value in stage_goal.items()}
            except ConditionError as exc:
                raise ValueError(f"Mission '{name}' stage {number}: {exc}") from exc
            self.stages.append({"title": stage.get("title") or f"Stage {number}", "description": stage.get("description", ""),
                                "goal": stage_goal, "goal_conditions": stage_conditions,
                                "rewards": EffectList.of(stage.get("rewards"))})
        if self.stages and not goal:
            goal = self.stages[-1]["goal"]
        unknown = sorted(set(goal or {}) - set(self.GOAL_KEYS))
        if not goal or unknown:
            raise ValueError(
                f"Mission '{name}' needs a goal using only: {', '.join(self.GOAL_KEYS)}"
                + (f" (unknown: {', '.join(unknown)})" if unknown else "")
            )
        self.name = name
        self.goal = dict(goal)
        try:
            self.goal_conditions = {key: Condition(goal_condition(key, value)) for key, value in self.goal.items()}
        except ConditionError as exc:
            raise ValueError(f"Mission '{name}': {exc}") from exc
        self.title = title or name
        self.description = description
        self.weight = float(weight)
        self.enabled_if = enabled_if  # offered only when this condition holds
        self.rewards = EffectList.of(rewards)


class WorldGraph:
    def __init__(self):
        self.locations = {}
        self.characters = []
        self.objects = []
        self.events = {}
        self.missions = {}
        self.spawn_rules = defaultdict(list)
        self.routes = None  # authored map (world_definition.routes): place -> set of neighbours

    @staticmethod
    def place_key(name):
        """A place name as compared for duplicates: case and spacing do not count."""
        return " ".join(str(name).split()).casefold()

    def add_location(self, loc):
        """Add a place; one with the name of a place already there (whatever its case or
        spacing) is refused with ValueError."""
        key = self.place_key(loc.name)
        clash = next((name for name in self.locations if self.place_key(name) == key), None)
        if clash is not None:
            raise ValueError(f"Location '{loc.name}' duplicates '{clash}'.")
        self.locations[loc.name] = loc

    def connect(self, first, second):
        """Connect two places by name, both ways; False for a place and itself or an unknown name."""
        a, b = self.locations.get(first), self.locations.get(second)
        return bool(a and b) and a.connect(b)

    @property
    def dynamic_conditions(self):
        """Whether a location, NPC or item is switched on by more than persistent
        variables (items held, places visited, ...), so that what is enabled can
        change with every turn, not only when a variable changes."""
        key = (Conditional.revision, len(self.locations), len(self.characters), len(self.objects))
        cached = self.__dict__.get("_dynamic_conditions")
        if cached is None or cached[0] != key:
            dynamic = any(
                entity.condition is not None and entity.condition.dynamic
                for entity in (*self.locations.values(), *self.characters, *self.objects)
            )
            cached = self._dynamic_conditions = (key, dynamic)
        return cached[1]

    def start_locations(self):
        """Locations a story may start at (``start_eligible``), in config order."""
        return [loc for loc in self.locations.values() if loc.start_eligible]

    def add_character(self, char):
        self.characters.append(char)

    def add_object(self, obj):
        self.objects.append(obj)

    def add_event(self, event):
        self.events[event.name] = event

    def add_mission(self, mission):
        self.missions[mission.name] = mission

    def allow_spawn(self, parent, child):
        self.spawn_rules[parent].append(child)

    def _story_progress(self, state):
        if state is None:
            return 0.0
        max_actions = max(100, config.get("narrative_end.max_actions", 100))
        return min(1.0, state.actions_taken / max_actions)

    def _phase_alignment_score(self, loc, state):
        progress = self._story_progress(state)
        phases = config.get("spawn_selection.story_phases") or {}
        intro_tags = set(phases.get("intro_tags", ["exploration", "urban", "nature", "spawn", "maritime", "port", "trade"]))
        middle_tags = set(phases.get("middle_tags", ["threat", "guards", "social", "sleazy", "prison", "dark", "guarded"]))
        climax_tags = set(phases.get("climax_tags", ["palace", "vault", "grand", "success", "payoff", "luxury"]))
        loc_tags = {tag.lower() for tag in loc.tags}

        if progress < phases.get("intro_end", 0.25):
            return sum(1 for tag in loc_tags if tag in intro_tags) * phases.get("intro_weight", 1.5)
        if progress < phases.get("climax_start", 0.75):
            return sum(1 for tag in loc_tags if tag in middle_tags) * phases.get("middle_weight", 1.75)
        return sum(1 for tag in loc_tags if tag in climax_tags) * phases.get("climax_weight", 2.0)

    def _score_spawn_candidate(self, current, loc, character=None, state=None, target_tags=None):
        if target_tags:
            # What the discovery asked for comes first; the usual scoring breaks ties.
            wanted = {str(tag).lower() for tag in target_tags}
            fit = semantic_tag_similarity(wanted, loc.tags) + len(wanted & {t.lower() for t in loc.tags})
            return fit * config.get("spawn_selection.target_tag_weight", 10.0) + \
                self._score_spawn_candidate(current, loc, character=character, state=state)
        similarity = semantic_tag_similarity(current.tags, loc.tags)
        contrast = 1.0 - similarity
        score = contrast * config.get("spawn_selection.contrast_weight", 2.5)
        score += self._phase_alignment_score(loc, state)

        if character:
            score += semantic_tag_similarity(character.associated_tags, loc.tags) * config.get("spawn_selection.character_alignment_weight", 2.0)

        if state is not None:
            # Discovered paths lean toward the mission goal.
            score += state._goal_route_pull(loc.name)
            prior_visits = state.location_counts.get(loc.name, 0)
            score -= prior_visits * config.get("spawn_selection.repeat_visit_penalty", 3.0)
            if state.path and state.path[-1] == loc.name:
                score -= config.get("spawn_selection.immediate_repeat_penalty", 10.0)

        return score

    def spawn(self, current, character=None, force_target=None, state=None, allow_special=False, target_tags=None):
        """The place a discovery from ``current`` opens (None: none). ``force_target`` names
        it; ``target_tags`` (a discovery led by tags) chooses the neighbour that fits them
        best, among those not yet known; otherwise the spawn rules and scoring decide."""
        if force_target:
            if force_target in self.locations:
                if force_target == "Dungeon" and not allow_special:
                    return None
                if state is not None and not state.is_enabled(self.locations[force_target]):
                    return None
                return self.locations[force_target]
            return None

        # 1. Primary Spawn Rules
        # With a map (the story's routes), a new path only opens to a neighbour, and
        # never to a place one can only be sent to (an arrest's cell).
        neighbours = state.neighbours(current.name) if state is not None and hasattr(state, "neighbours") else None
        sent_only = state.sent_only if state is not None and hasattr(state, "sent_only") else (lambda name: False)
        options = []
        if target_tags:
            # Led by tags: any place next door (any place, without a map) may answer them.
            pool = neighbours if neighbours is not None else set(self.locations)
            options = sorted(o for o in pool if o != current.name and o not in current.connected)
        elif character:
            options = list(self.locations.keys())
        elif state is not None and hasattr(state, "spawn_options"):
            options = state.spawn_options(current.name)  # the story's own geography
        else:
            options = self.spawn_rules.get(current.name, [])
        if neighbours is not None:
            options = [o for o in options if o in neighbours and not sent_only(o)]
        if state is not None:
            # Locations switched off by persistent variables cannot be discovered.
            options = [o for o in options if o in self.locations and state.is_enabled(self.locations[o])]

        # 2. FALLBACK: Tag Similarity (Ensures graph is never disconnected)
        if not options or all(o in current.connected for o in options):
            # Find locations that share at least 1 tag but aren't the current one
            fallback_options = []
            for name, loc in self.locations.items():
                if name == current.name: continue
                if name in current.connected: continue
                if neighbours is not None and (name not in neighbours or sent_only(name)):
                    continue  # the map has no way there from here
                if name == "Dungeon" and not allow_special:
                    continue
                if state is not None and not state.is_enabled(loc):
                    continue

                similarity = semantic_tag_similarity(current.tags, loc.tags)
                if similarity > config.get("spawn_selection.fallback_similarity_threshold", 0.2):
                    fallback_options.append((similarity, name))
            
            if fallback_options:
                # Sort by overlap DESC
                fallback_options.sort(key=lambda x: x[0], reverse=True)
                options = [o[1] for o in fallback_options[:int(config.get("spawn_selection.fallback_candidates", 5))]]

        if not options:
            return None

        # Filter out the current location itself and connected if possible
        available = [o for o in options if o != current.name and (o != "Dungeon" or allow_special)]
        if not available:
            return None

        scored = []
        for loc_name in available:
            loc = self.locations[loc_name]
            score = self._score_spawn_candidate(current, loc, character=character, state=state, target_tags=target_tags)
            scored.append((score, loc_name))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        new_name = scored[0][1]

        if new_name not in self.locations:
            return None

        new_loc = self.locations[new_name]
        return new_loc
