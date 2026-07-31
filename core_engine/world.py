import random
from collections import defaultdict
from utils.config_loader import config
from utils.tag_similarity import semantic_tag_similarity

# ============================
# WORLD GRAPH / CHARACTERS
# ============================

class Character:
    def __init__(self, name, associated_tags, goals=None, 
                 descriptions=None, actions=None, 
                 known_locations=None, known_objects=None):
        self.name = name
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
                 distant_descriptions=None, entered_descriptions=None,
                 exit_locked=False, template_id=None, parameters=None):
        self.name = name
        self.tags = set(tags)
        self.interact_prompts = interact_prompts or []
        self.goals = goals or []
        self.distant_descriptions = distant_descriptions or []
        self.entered_descriptions = entered_descriptions or []
        self.actions = []
        self.connected = set()
        self.exit_locked = bool(exit_locked)
        self.template_id = template_id or self._default_template_id(name)
        self.parameters = dict(parameters or {})

    @staticmethod
    def _default_template_id(name):
        return "_".join(str(name).strip().casefold().split())

    @property
    def scenario_key(self):
        """Identity of a parameterized scenario instance.

        The same template may be reused with different parameters, but the exact
        template/parameter combination is one world node.
        """
        normalized_parameters = tuple(
            sorted((str(key), repr(value)) for key, value in self.parameters.items())
        )
        return self.template_id, normalized_parameters

    def connect(self, other):
        if other.name != self.name:
            self.connected.add(other.name)


class WorldGraph:
    def __init__(self):
        self.locations = {}
        self._location_names = {}
        self._scenario_keys = {}
        self.characters = []
        self.objects = []
        self.spawn_rules = defaultdict(list)

    def add_location(self, loc):
        normalized = self._normalize_location_name(loc.name)
        if normalized in self._location_names:
            existing = self._location_names[normalized]
            raise ValueError(
                f"Duplicate location identity: {loc.name!r} conflicts with {existing!r}."
            )
        if loc.scenario_key in self._scenario_keys:
            existing = self._scenario_keys[loc.scenario_key]
            raise ValueError(
                "Duplicate parameterized scenario: "
                f"{loc.name!r} has the same template and parameters as {existing!r}."
            )
        self.locations[loc.name] = loc
        self._location_names[normalized] = loc.name
        self._scenario_keys[loc.scenario_key] = loc.name

    @staticmethod
    def _normalize_location_name(name):
        return " ".join(str(name).strip().casefold().split())

    def resolve_location_name(self, name):
        if name in self.locations:
            return name
        return self._location_names.get(self._normalize_location_name(name))

    def connect(self, first, second, bidirectional=True):
        first_name = self.resolve_location_name(first)
        second_name = self.resolve_location_name(second)
        if not first_name or not second_name or first_name == second_name:
            return False

        self.locations[first_name].connect(self.locations[second_name])
        if bidirectional:
            self.locations[second_name].connect(self.locations[first_name])
        return True

    def add_character(self, char):
        self.characters.append(char)

    def add_object(self, obj):
        self.objects.append(obj)

    def allow_spawn(self, parent, child):
        parent_name = self.resolve_location_name(parent)
        child_name = self.resolve_location_name(child)
        if (
            parent_name
            and child_name
            and parent_name != child_name
            and child_name not in self.spawn_rules[parent_name]
        ):
            self.spawn_rules[parent_name].append(child_name)

    def _story_progress(self, state):
        if state is None:
            return 0.0
        max_actions = max(1, config.get("narrative_end.max_actions", config.get("fitness.story_length.target", 12)))
        return min(1.0, state.actions_taken / max_actions)

    def _score_spawn_candidate(
        self,
        current,
        loc,
        character=None,
        state=None,
        genome=None,
        target_tags=None,
    ):
        evolution_cfg = config.get("world_evolution", {})
        context_similarity = semantic_tag_similarity(current.tags, loc.tags)
        intent_similarity = (
            semantic_tag_similarity(target_tags, loc.tags)
            if target_tags else context_similarity
        )

        coherence = getattr(genome, "tag_coherence", 0.5)
        novelty = getattr(genome, "tag_novelty", 0.5)
        extension = getattr(genome, "extension_bias", 0.5)
        score = intent_similarity * evolution_cfg.get("intent_weight", 4.0)
        score += context_similarity * coherence * evolution_cfg.get("coherence_weight", 1.5)
        score += (1.0 - context_similarity) * novelty * evolution_cfg.get("novelty_weight", 1.25)

        if genome and loc.tags:
            affinity = sum(genome.tag_preference(tag) for tag in loc.tags) / len(loc.tags)
            score += affinity * evolution_cfg.get("genome_tag_weight", 2.0)

        if character:
            score += semantic_tag_similarity(
                character.associated_tags,
                loc.tags,
            ) * evolution_cfg.get("character_alignment_weight", 1.5)

        if state is not None:
            prior_visits = state.location_counts.get(loc.name, 0)
            score -= prior_visits * extension * evolution_cfg.get("visited_node_penalty", 2.0)
            if loc.name in current.connected:
                score -= extension * evolution_cfg.get("connected_node_penalty", 3.0)
            if state.path and state.path[-1] == loc.name:
                score -= evolution_cfg.get("immediate_repeat_penalty", 10.0)

        # Authored transition lists are only a weak prior. Tag intent remains the
        # primary mechanism for selecting how the world expands.
        if loc.name in self.spawn_rules.get(current.name, []):
            score += evolution_cfg.get("authored_transition_weight", 0.15)

        return score

    def spawn(
        self,
        current,
        character=None,
        force_target=None,
        target_tags=None,
        genome=None,
        state=None,
        allow_special=False,
    ):
        if force_target:
            target_name = self.resolve_location_name(force_target)
            if target_name:
                if target_name == "Dungeon" and not allow_special:
                    return None
                if target_name == current.name:
                    return None
                return self.locations[target_name]
            return None

        # Every configured location is a candidate. Tags, genome parameters, and
        # novelty determine the next extension; static transition lists do not.
        available = [
            name for name in self.locations
            if name != current.name and (name != "Dungeon" or allow_special)
        ]
        unconnected = [
            name for name in available
            if name not in current.connected
        ]
        if unconnected:
            available = unconnected
        if not available:
            return None

        scored = []
        for loc_name in available:
            loc = self.locations[loc_name]
            score = self._score_spawn_candidate(
                current,
                loc,
                character=character,
                state=state,
                genome=genome,
                target_tags=target_tags,
            )
            scored.append((score, loc_name))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        new_name = scored[0][1]

        if new_name not in self.locations:
            return None

        new_loc = self.locations[new_name]
        return new_loc
