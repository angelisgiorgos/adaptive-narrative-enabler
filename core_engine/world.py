import random
from collections import defaultdict
from utils.config_loader import config
from utils.tag_similarity import semantic_tag_similarity

# ============================
# WORLD GRAPH / CHARACTERS
# ============================

class Character:
    """An NPC. A unique NPC lives in a single location for the whole game;
    a generic NPC (e.g. a guard) may appear in every location it fits.

    ``encounter_event`` names an Event that starts by itself when the NPC is
    present in the scene (e.g. a bandit ambush), once per game, or on every
    visit to a location when ``encounter_repeat`` is set."""
    def __init__(self, name, associated_tags, goals=None, 
                 descriptions=None, actions=None, 
                 known_locations=None, known_objects=None, unique=False,
                 encounter_event=None, encounter_repeat=False):
        self.name = name
        self.unique = bool(unique)
        self.encounter_event = encounter_event or None
        self.encounter_repeat = bool(encounter_repeat)
        self.associated_tags = set(associated_tags)
        self.goals = goals or []
        self.descriptions = descriptions or []
        self.actions = actions or []
        self.known_locations = known_locations or []
        self.known_objects = known_objects or []

class Object:
    def __init__(self, name, associated_tags, interact_prompts=None, 
                 goals=None, descriptions=None, actions=None, collectible=False):
        self.name = name
        self.associated_tags = set(associated_tags)
        self.interact_prompts = interact_prompts or []
        self.goals = goals or []
        self.descriptions = descriptions or []
        self.actions = actions or []
        self.collectible = collectible

class Location:
    def __init__(self, name, tags, interact_prompts=None, goals=None, 
                 distant_descriptions=None, entered_descriptions=None):
        self.name = name
        self.tags = set(tags)
        self.interact_prompts = interact_prompts or []
        self.goals = goals or []
        self.distant_descriptions = distant_descriptions or []
        self.entered_descriptions = entered_descriptions or []
        self.actions = []
        self.connected = set()

    def connect(self, other):
        self.connected.add(other.name)
        other.connected.add(self.name)

    def connect_one_way(self, other):
        """A route from here to ``other`` only; ``other`` does not lead back."""
        self.connected.add(other.name)


class Event:
    """A self-contained situation entered by taking an action that triggers it.

    While an event is active its actions replace every location, NPC, item and
    travel action. NPCs and items never spawn inside an event. When the player
    picks one of its actions the event ends and the player is back at the
    location where it began, unless that action's outcome moved them elsewhere.
    """
    def __init__(self, name, title=None, tags=None, descriptions=None, actions=None):
        self.name = name
        self.title = title or name
        self.tags = set(tags or [])
        self.descriptions = descriptions or []
        self.actions = actions or []


class Mission:
    """A goal the player must achieve to win. One mission is chosen per story.

    Every condition in ``goal`` must hold for the mission to be complete.
    """
    GOAL_KEYS = ("action", "location", "item", "event", "npc_removed", "outcome_tag", "coins")

    def __init__(self, name, goal, title=None, description="", weight=1.0):
        unknown = sorted(set(goal or {}) - set(self.GOAL_KEYS))
        if not goal or unknown:
            raise ValueError(
                f"Mission '{name}' needs a goal using only: {', '.join(self.GOAL_KEYS)}"
                + (f" (unknown: {', '.join(unknown)})" if unknown else "")
            )
        self.name = name
        self.goal = dict(goal)
        self.title = title or name
        self.description = description
        self.weight = float(weight)


class WorldGraph:
    def __init__(self):
        self.locations = {}
        self.characters = []
        self.objects = []
        self.events = {}
        self.missions = {}
        self.spawn_rules = defaultdict(list)

    def add_location(self, loc):
        self.locations[loc.name] = loc

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
        intro_tags = {"exploration", "urban", "nature", "spawn", "maritime", "port", "trade"}
        middle_tags = {"threat", "guards", "social", "sleazy", "prison", "dark", "guarded"}
        climax_tags = {"palace", "vault", "grand", "success", "payoff", "luxury"}
        loc_tags = {tag.lower() for tag in loc.tags}

        if progress < 0.25:
            return sum(1 for tag in loc_tags if tag in intro_tags) * 1.5
        if progress < 0.75:
            return sum(1 for tag in loc_tags if tag in middle_tags) * 1.75
        return sum(1 for tag in loc_tags if tag in climax_tags) * 2.0

    def _score_spawn_candidate(self, current, loc, character=None, state=None):
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

    def spawn(self, current, character=None, force_target=None, state=None, allow_special=False):
        if force_target:
            if force_target in self.locations:
                if force_target == "Dungeon" and not allow_special:
                    return None
                return self.locations[force_target]
            return None

        # 1. Primary Spawn Rules
        options = []
        if character:
            options = list(self.locations.keys())
        else:
            options = self.spawn_rules.get(current.name, [])

        # 2. FALLBACK: Tag Similarity (Ensures graph is never disconnected)
        if not options or all(o in current.connected for o in options):
            # Find locations that share at least 1 tag but aren't the current one
            fallback_options = []
            for name, loc in self.locations.items():
                if name == current.name: continue
                if name in current.connected: continue
                if name == "Dungeon" and not allow_special:
                    continue

                similarity = semantic_tag_similarity(current.tags, loc.tags)
                if similarity > 0.2:
                    fallback_options.append((similarity, name))
            
            if fallback_options:
                # Sort by overlap DESC
                fallback_options.sort(key=lambda x: x[0], reverse=True)
                options = [o[1] for o in fallback_options[:5]] # Top 5 similar

        if not options:
            return None

        # Filter out the current location itself and connected if possible
        available = [o for o in options if o != current.name and (o != "Dungeon" or allow_special)]
        if not available:
            return None

        scored = []
        for loc_name in available:
            loc = self.locations[loc_name]
            score = self._score_spawn_candidate(current, loc, character=character, state=state)
            scored.append((score, loc_name))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        new_name = scored[0][1]

        if new_name not in self.locations:
            return None

        new_loc = self.locations[new_name]
        return new_loc
