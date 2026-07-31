import random
import re
from utils.config_loader import config
from .actions import Action, Outcome
from utils.tag_similarity import semantic_tag_similarity

# ============================
# GAME STATE
# ============================

class GameState:
    def __init__(self, world, genome, start="Harbor", verbose=False, interactive=False, rng=None):
        self.world = world
        self.genome = genome
        self.verbose = verbose
        self.interactive = interactive
        self.rng = rng or random
        start_name = world.resolve_location_name(start)
        if not start_name:
            raise ValueError(f"Unknown starting location: {start}")
        self.current = world.locations[start_name]

        self.health = config.get("game_state.initial_health", 8)
        self.actions_taken = 0

        self.tags_seen = []
        self.history = []
        
        # Track location visits
        self.location_counts = {start_name: 1}
        self.path = [start_name] # Sequence of locations
        self.last_location_name = None # For tracking transitions
        self.previous_location_name = None
        self._scene_location_name = None
        self.current_inhabitants = [] # NPCs currently in the scene
        self.location_objects = []
        self.consecutive_steps_in_location = 0 # Track how long we've stayed

        self.setup_indices = {}
        self.payoff_indices = {}
        self.active_setups = set()
        self.completed_payoffs = set()
        
        self.character_interactions = {} # Track speaker visits
        self.npc_goal_progress = {}
        self.resolved_encounters = set()
        
        # Track which actions were performed during the current visit to a location
        self.visit_actions_performed = set()
        
        self.inventory = set()
        self.coins = config.get("game_state.initial_coins", 5)

        # Narrative Discovery State
        self.visible_inhabitants = set()
        self.visible_objects = set()
        self.discovered_locations = {start_name}
        self.transition_records = []
        self.scenario_events = []
        self.unlocked_exit_locations = set()
        self.ended = False
        self.end_reason = None
        self.power = 0.0
        self.update_power()

    def _pick_text(self, options, offset=0):
        if not options:
            return None
        index = (self.actions_taken + self.location_counts.get(self.current.name, 0) + offset) % len(options)
        return options[index]

    def _describe_visible_scene(self):
        """Builds a readable scene description for the current interactive turn."""
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
            goals = "; ".join(char.goals) if char.goals else "No explicit goal"
            npc_lines.append((char.name, f"{char_desc} Goal: {goals}."))

        object_lines = []
        for obj in self.location_objects:
            if obj.name in self.inventory or obj.name not in self.visible_objects:
                continue
            obj_desc = self._pick_text(obj.descriptions, offset=len(obj.name)) if obj.descriptions else f"You notice a {obj.name}."
            object_lines.append((obj.name, obj_desc))

        return location_desc, npc_lines, object_lines

    @staticmethod
    def _goal_terms(goals):
        stop_words = {
            "a", "an", "and", "the", "to", "of", "for", "with", "from",
            "in", "on", "at", "their", "his", "her",
        }
        terms = set()
        for goal in goals or []:
            terms.update(
                token for token in re.findall(r"[a-z0-9_]+", str(goal).lower())
                if token not in stop_words and len(token) > 2
            )
        return terms

    def _npc_goal_alignment(self, char, extra_terms=None):
        goal_terms = self._goal_terms(char.goals)
        context_terms = {str(tag).lower() for tag in self.current.tags}
        context_terms.update(extra_terms or [])
        if not goal_terms:
            return 0.0
        direct = len(goal_terms & context_terms)
        fuzzy = sum(
            1
            for goal in goal_terms
            for term in context_terms
            if goal in term or term in goal
        )
        return direct + (0.25 * fuzzy)

    def has_setup(self, setup_id):
        return str(setup_id) in self.active_setups

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

    def _check_end_conditions(self):
        if self.health <= 0:
            self.ended = True
            self.end_reason = "The traveler can no longer continue."
            return True

        end_cfg = config.get("narrative_end", {})
        max_actions = end_cfg.get("max_actions", 12)
        if self.actions_taken >= max_actions:
            self.ended = True
            self.end_reason = f"The story reached its action limit of {max_actions} steps."
            return True

        max_consecutive = end_cfg.get("max_consecutive_steps_in_location", 5)
        if self.consecutive_steps_in_location >= max_consecutive:
            self.ended = True
            self.end_reason = f"The story stalled in {self.current.name} for too long."
            return True

        if self.current.name == "Dungeon" and end_cfg.get("end_on_dungeon", False):
            self.ended = True
            self.end_reason = "The traveler was thrown into the Dungeon."
            return True

        return False

    def _finalize_if_ended(self):
        if self._check_end_conditions():
            end_msg = f"[END] {self.end_reason}"
            self.history.append(end_msg)
            if self.interactive or self.verbose:
                print(f"  >>> {end_msg}")
            return True
        return False

    def _score_character_visibility(self, char):
        score = semantic_tag_similarity(char.associated_tags, self.current.tags) * 2.0
        score += self._npc_goal_alignment(char) * config.get("npc_goals.visibility_weight", 0.4)
        if "social" in char.associated_tags:
            score += self.genome.bias_social
        if "threat" in char.associated_tags:
            score += self.genome.bias_threat * 0.5
        if "urban" in char.associated_tags:
            score += self.genome.bias_urban * 0.25
        return score

    def _score_object_visibility(self, obj):
        score = semantic_tag_similarity(obj.associated_tags, self.current.tags) * 2.0
        if "urban" in obj.associated_tags:
            score += self.genome.bias_urban * 0.4
        if "maritime" in obj.associated_tags:
            score += self.genome.bias_maritime * 0.4
        if "combat" in obj.associated_tags:
            score += self.genome.bias_threat * 0.2
        return score

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

    def _calculate_power(self):
        power_cfg = config.get("power_model", {})
        initial_health = max(1, config.get("game_state.initial_health", 8))
        health_ratio = max(0.0, self.health / initial_health)
        attack_bonus, defense_bonus = self._inventory_combat_profile()

        inventory_power = attack_bonus * power_cfg.get("attack_weight", 1.0)
        inventory_power += defense_bonus * power_cfg.get("defense_weight", 0.8)

        base_power = power_cfg.get("base_power", 0.0)
        power = base_power
        power += health_ratio * power_cfg.get("health_weight", 1.5)
        power += inventory_power
        if self.health <= config.get("game_state.low_health_threshold", 4):
            power -= power_cfg.get("low_health_penalty", 0.5)

        minimum = power_cfg.get("min_power", 0.0)
        maximum = power_cfg.get("max_power", 5.0)
        return max(minimum, min(maximum, power))

    def update_power(self):
        self.power = self._calculate_power()
        return self.power

    def power_breakdown(self):
        attack, defense = self._inventory_combat_profile()
        return {
            "power": self.power,
            "health": self.health,
            "attack": attack,
            "defense": defense,
        }

    def _action_check_summary(self, action):
        outcomes = action.available_outcomes(self)
        if not outcomes:
            return None
        outcome = outcomes[0]
        check_name = outcome.check_name(action)
        if not check_name:
            return None
        probability = outcome.success_probability(
            genome=self.genome,
            state=self,
            action=action,
        )
        return f"{check_name} check, {probability:.0%} success"

    def _rank_actions(self, actions):
        ranked = []
        for a in actions:
            all_o_tags = []
            discovery_score = 0
            movement_score = 0
            recovery_score = 0
            for o in a.outcomes:
                all_o_tags.extend(o.tags)
                if (
                    o.spawn
                    or o.reveal_location
                    or o.target_tags
                    or o.reveal_npc
                    or o.reveal_object
                ):
                    discovery_score += 1
                if o.move_to or o.move_to_tags or o.lead_to_known:
                    movement_score += 1
                if o.health_change > 0 or o.coin_change > 0:
                    recovery_score += 1

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
            if recovery_score and self.health <= config.get("game_state.low_health_threshold", 4):
                weight += recovery_score * self.genome.recovery_bias * config.get("action_selection.recovery_weight", 1.2)

            if a.character_name and a.npc_goals:
                char = next(
                    (candidate for candidate in self.world.characters if candidate.name == a.character_name),
                    None,
                )
                if char:
                    outcome_terms = {
                        str(tag).lower()
                        for outcome in a.available_outcomes(self)
                        for tag in outcome.tags
                    }
                    weight += self._npc_goal_alignment(
                        char,
                        outcome_terms,
                    ) * config.get("npc_goals.action_weight", 0.5)

            is_movement = any(
                o.move_to
                or o.move_to_tags
                or o.spawn
                or o.lead_to_known
                or o.reveal_location
                or o.target_tags
                for o in a.outcomes
            )
            if is_movement:
                stay_penalty_power = config.get("action_selection.stay_penalty_power", 2.0)
                weight *= (1.0 + (self.genome.voyage_bias * self.consecutive_steps_in_location) ** stay_penalty_power)

            ranked.append((weight, a))

        ranked.sort(key=lambda pair: (pair[0], pair[1].name), reverse=True)
        return ranked


    def get_available_actions(self):
        """Returns a list of valid actions for the current state with narrative visibility."""
        # 1. Detect New Location Entry or Stale Inhabitants
        if self.current.name != self._scene_location_name:
            self._scene_location_name = self.current.name
            
            # Determine ALL possible NPCs for this visit
            all_possible = [char for char in self.world.characters if char.associated_tags & self.current.tags]
            if all_possible:
                scored = sorted(
                    all_possible,
                    key=lambda char: (self._score_character_visibility(char), char.name),
                    reverse=True,
                )
                count = min(len(scored), config.get("scene.max_visible_npcs", 3))
                self.current_inhabitants = scored[:count]
            else:
                self.current_inhabitants = []

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

        # 2. Gather actions for VISIBLE entities only
        raw_actions = []
        raw_actions.extend(self.current.actions)
        
        # Filter inhabitants
        for char in self.current_inhabitants:
            if char.name in self.visible_inhabitants:
                raw_actions.extend(char.actions)
        
        # Filter objects
        for obj in self.location_objects:
            if obj.name in self.visible_objects:
                raw_actions.extend(obj.actions)

        # 3. Process & Template Actions
        import copy
        filtered_actions = [
            copy.deepcopy(a) for a in raw_actions 
            if (
                (a.name not in self.visit_actions_performed or a.name.startswith("Approach"))
                and (not a.mandatory or (a.source_name or a.name) not in self.resolved_encounters)
                and a.available_outcomes(self)
            )
        ]

        final_actions = []
        for a in filtered_actions:
            # Check for required object/tag
            if a.required_tag:
                objects_dict = {obj.name: obj for obj in self.world.objects}
                matching_items = [obj for name, obj in objects_dict.items() 
                                 if a.required_tag in obj.associated_tags and name in self.inventory]
                if not matching_items: continue
                item = sorted(matching_items, key=lambda obj: (self._score_object_visibility(obj), obj.name), reverse=True)[0]
                a.active_item = item.name
                if "{" in a.name and "}" in a.name:
                    a.name = re.sub(r'\{.*?\}', item.name, a.name)
            
            if a.required_object:
                if a.required_object not in self.inventory: continue
                a.active_item = a.required_object 
            
            if a.required_coins > self.coins: continue
            if a.collects_object and a.collects_object in self.inventory: continue
                
            final_actions.append(a)

        mandatory_actions = [action for action in final_actions if action.mandatory]
        if mandatory_actions:
            return mandatory_actions

        # 4. Navigation
        exits_locked = (
            self.current.exit_locked
            and self.current.name not in self.unlocked_exit_locations
        )
        if not exits_locked:
            for loc_name in sorted(self.current.connected):
                if loc_name == self.current.name or loc_name not in self.world.locations:
                    continue
                navigation_action = Action(
                    name=f"Move to {loc_name}",
                    outcomes=[Outcome(desc=f"You travel to {loc_name}.", move_to=loc_name)],
                    source_type="navigation",
                    source_name=loc_name,
                )
                final_actions.append(navigation_action)
        
        return final_actions

    def step(self):
        """Executes one simulation step."""
        if self.ended:
            return False

        actions = self.get_available_actions()
        if not actions:
            self.ended = True
            if self.current.exit_locked and self.current.name not in self.unlocked_exit_locations:
                self.end_reason = f"No unresolved action can open an exit from {self.current.name}."
            else:
                self.end_reason = f"No actions or connected routes remain in {self.current.name}."
            self.history.append(f"[END] {self.end_reason}")
            return False

        # Record beginning of story segment
        is_new_loc = self.current.name != self.last_location_name
        desc, npc_lines, object_lines = self._describe_visible_scene()

        if is_new_loc:
            if self.interactive or self.verbose:
                print(f"\n--- The traveler is at: {self.current.name} (Health: {self.health}) ---")
                print(f"Location: {self.current.name}")
                print(f"Description: {desc}")
            
            self.history.append(f"\nYou are in the {self.current.name}. {desc}")
            
            # Describe present NPCs
            for char_name, char_desc in npc_lines:
                self.history.append(f"Met a {char_name}: {char_desc}")
                if self.interactive:
                    print(f"  (NPC) {char_name}: {char_desc}")
            
            # Describe present Objects
            for obj_name, obj_desc in object_lines:
                self.history.append(f"Nearby you see: {obj_desc}")
                if self.interactive:
                    print(f"  (Item) {obj_name}: {obj_desc}")
        else:
            self.history.append(f"\nContinuing your activities in the {self.current.name}...")
            if self.interactive:
                print(f"\n--- The traveler remains at: {self.current.name} (Health: {self.health}) ---")
                print(f"Location: {self.current.name}")
                print(f"Description: {desc}")
                for char_name, char_desc in npc_lines:
                    print(f"  (NPC) {char_name}: {char_desc}")
                for obj_name, obj_desc in object_lines:
                    print(f"  (Item) {obj_name}: {obj_desc}")

        if self.interactive:
            # Show all player choices by default so NPC and navigation actions cannot be hidden.
            options = [action for _, action in self._rank_actions(actions)]
            max_choices = int(config.get("scene.max_interactive_choices", 0) or 0)
            if max_choices > 0:
                options = options[:max_choices]
            
            status = self.power_breakdown()
            print(
                "Status: "
                f"Health {status['health']} | Coins {self.coins} | "
                f"Power {status['power']:.2f} "
                f"(combat: attack {status['attack']:.2f}, defense {status['defense']:.2f})"
            )
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
                
                check_summary = self._action_check_summary(opt)
                check_suffix = f" [{check_summary}]" if check_summary else ""
                mandatory_prefix = "[MANDATORY] " if opt.mandatory else ""
                print(f" [{i+1}] {mandatory_prefix}{prefix}{opt.name}{check_suffix}")
            
            choice = -1
            while choice < 0 or choice >= len(options):
                try:
                    val = input("Enter choice (number): ")
                    choice = int(val) - 1
                except ValueError:
                    print("Invalid input. Please enter a number.")
            
            action = options[choice]
        else:
            ranked_actions = self._rank_actions(actions)
            action = ranked_actions[0][1]

        # Record beginning of story segment
        is_new_loc = self.current.name != self.last_location_name
        self.last_location_name = self.current.name
        self.history.append(f"> {action.name}")
        
        # AI decides outcome based on Genome (Luck)
        outcome = action.choose_outcome(self.genome, state=self)
        if outcome is None:
            self.history.append(f"[BLOCKED] No currently valid outcome for {action.name}.")
            return False
        self.actions_taken += 1
        self.consecutive_steps_in_location += 1 # Increment stay counter
        
        # Reset stay counter if we move
        if outcome.move_to:
            self.consecutive_steps_in_location = 0

        check_summary = self._action_check_summary(action)
        if check_summary:
            check_msg = f"[CHECK] {action.name}: {check_summary}"
            self.history.append(check_msg)
            if self.interactive or self.verbose:
                print(f"  >>> {check_msg}")

        if outcome.success(genome=self.genome, state=self, action=action):
            if self.interactive:
                print(f"\nRESULT: {outcome.desc}")
            self.apply(outcome, action=action)
        else:
            if self.verbose:
                print(f"  FAILED: {action.name}")
            attack_bonus, defense_bonus = self._inventory_combat_profile()
            failure_penalty = 1
            if self._is_guard_failure(action, outcome) or "combat" in {tag.lower() for tag in outcome.tags} or "threat" in {tag.lower() for tag in outcome.tags}:
                failure_penalty = max(
                    0,
                    failure_penalty - int(defense_bonus * config.get("combat_resolution.failure_defense_mitigation", 0.5)),
                )
            self.health -= failure_penalty
            self.tags_seen.append("failure")
            
            # Narrative Consequence: Failure against guards leads to arrest
            if self._is_guard_failure(action, outcome):
                arrest_msg = "[FAILURE] The traveler lost the encounter and was dragged to the Dungeon."
                self.history.append(arrest_msg)
                if self.interactive or self.verbose:
                    print(f"  >>> {arrest_msg}")
                outcome.move_to = "Dungeon"
                outcome.reveal_location = "Dungeon"
                self.apply(outcome, action=action)
            else:
                fail_msg = f"[FAILURE] {action.name} failed."
                self.history.append(fail_msg)
                if self.interactive or self.verbose:
                    print(f"  >>> {fail_msg}")
                if self._finalize_if_ended():
                    return False

        if self._finalize_if_ended():
            return False
        return True

    def resolve_destination(self, move_to=None, move_to_tags=None):
        """AI-driven selection of a target location among candidates."""
        if move_to:
            resolved_name = self.world.resolve_location_name(move_to)
            if resolved_name:
                return resolved_name

        if not move_to_tags:
            return None

        candidate = self.world.spawn(
            self.current,
            target_tags=move_to_tags,
            genome=self.genome,
            state=self,
            allow_special=("prison" in {str(tag).lower() for tag in move_to_tags}),
        )
        return candidate.name if candidate else None

    def _location_intent_tags(self, outcome):
        intent_tags = {str(tag).lower() for tag in outcome.target_tags}
        if intent_tags:
            return intent_tags

        # Backward compatibility: a named reveal becomes a tag hint. The name is
        # not treated as a fixed edge unless force_named_target is explicitly set.
        if outcome.reveal_location:
            resolved = self.world.resolve_location_name(outcome.reveal_location)
            if resolved:
                intent_tags.update(
                    str(tag).lower()
                    for tag in self.world.locations[resolved].tags
                )

        if not intent_tags:
            intent_tags.update(
                str(tag).lower() for tag in outcome.tags
                if str(tag).lower() not in {
                    "spawn", "success", "failure", "neutral", "setup_clue"
                }
                and not str(tag).lower().startswith(("setup_", "payoff_"))
            )
        return intent_tags

    def _record_transition(self, origin, destination, intent_tags, resolution):
        origin_loc = self.world.locations.get(origin)
        destination_loc = self.world.locations.get(destination)
        if not origin_loc or not destination_loc:
            return
        self.transition_records.append({
            "origin": origin,
            "destination": destination,
            "intent_tags": sorted(intent_tags),
            "resolution": resolution,
            "tag_similarity": semantic_tag_similarity(
                intent_tags or origin_loc.tags,
                destination_loc.tags,
            ),
        })

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
                    hidden_locs = [l for l in char_obj.known_locations if l not in self.current.connected]
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

    def _move_to(self, target_name):
        """Helper to transition to a new location and update history/counters."""
        resolved_target = self.world.resolve_location_name(target_name)
        if not resolved_target or resolved_target == self.current.name:
            return False

        origin = self.current.name
        self.world.connect(origin, resolved_target)
        self.discovered_locations.add(resolved_target)
        self.previous_location_name = origin
        self.last_location_name = origin
        self.current = self.world.locations[resolved_target]
        self.location_counts[self.current.name] = self.location_counts.get(self.current.name, 0) + 1
        self.path.append(self.current.name)
        self.consecutive_steps_in_location = 0
        self.visit_actions_performed = set()
        self._scene_location_name = None
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
        
        self.tags_seen.append(list(situational_tags))
        self.scenario_events.append({
            "scenario_key": repr(self.current.scenario_key),
            "parameters": dict(self.current.parameters),
            "action": action.name if action else None,
            "tags": sorted(str(tag).lower() for tag in outcome.tags),
        })
        self.history.append(f"RESULT: {outcome.desc}")
        self.history.append(f"[LOCATION] Current location: {origin_location}")

        if outcome.setup_id:
            setup_id = str(outcome.setup_id)
            self.active_setups.add(setup_id)
            self.setup_indices.setdefault(f"setup_{setup_id}", self.actions_taken)
            self.history.append(f"[SETUP] Established: {setup_id}")

        if outcome.payoff_id:
            payoff_id = str(outcome.payoff_id)
            if payoff_id in self.active_setups:
                self.completed_payoffs.add(payoff_id)
                self.payoff_indices.setdefault(f"payoff_{payoff_id}", self.actions_taken)
                self.history.append(f"[PAYOFF] Resolved setup: {payoff_id}")

        if action and action.character_name:
            char = next(
                (candidate for candidate in self.world.characters if candidate.name == action.character_name),
                None,
            )
            if char:
                outcome_terms = {str(tag).lower() for tag in outcome.tags}
                goal_gain = self._npc_goal_alignment(char, outcome_terms)
                if goal_gain > 0:
                    self.npc_goal_progress[char.name] = (
                        self.npc_goal_progress.get(char.name, 0.0) + goal_gain
                    )
                    self.history.append(
                        f"[NPC GOAL] {char.name} progress: "
                        f"{self.npc_goal_progress[char.name]:.2f}"
                    )

        # 0. Apply Outcome Mechanical Changes
        health_delta = outcome.health_change
        lower_tags = {tag.lower() for tag in outcome.tags}
        attack_bonus, defense_bonus = self._inventory_combat_profile()
        if health_delta < 0 and ("combat" in lower_tags or "threat" in lower_tags):
            mitigation = int(
                round(
                    attack_bonus * config.get("combat_resolution.attack_damage_mitigation", 0.15)
                    + defense_bonus * config.get("combat_resolution.defense_damage_mitigation", 0.5)
                )
            )
            health_delta = min(0, health_delta + mitigation)

        self.health += health_delta
        self.coins += outcome.coin_change
        self.update_power()
        if self.interactive or self.verbose:
            print(
                f"  >>> Health: {self.health} | Coins: {self.coins} | "
                f"Power (combat): {self.power:.2f}"
            )

        # 1. Reveal/spawn adds a bidirectional graph edge but never moves the player.
        if outcome.spawn or outcome.reveal_location or outcome.target_tags:
            intent_tags = self._location_intent_tags(outcome)
            policy = config.get(
                "world_evolution.named_transition_policy",
                "tag_hint",
            )
            use_named_target = (
                outcome.force_named_target
                or policy == "force"
            )
            target = (
                outcome.reveal_location
                if use_named_target
                else None
            )
            new_loc = self.world.spawn(
                self.current,
                force_target=target,
                target_tags=intent_tags,
                genome=self.genome,
                state=self,
                allow_special=(
                    target == "Dungeon"
                    or outcome.reveal_location == "Dungeon"
                    or "prison" in intent_tags
                ),
            )
            if new_loc:
                self.world.connect(self.current.name, new_loc.name)
                self.discovered_locations.add(new_loc.name)
                self._record_transition(
                    origin_location,
                    new_loc.name,
                    intent_tags,
                    "named" if use_named_target else "tags",
                )
                spawn_msg = f"[SPAWN] New path discovered: {new_loc.name}"
                self.history.append(spawn_msg)
                if self.interactive or self.verbose:
                    print(f"  >>> {spawn_msg}")

        if outcome.unlock_exit:
            self.unlocked_exit_locations.add(origin_location)
            unlock_msg = f"[EXIT] Routes out of {origin_location} are now available."
            self.history.append(unlock_msg)
            if self.interactive or self.verbose:
                print(f"  >>> {unlock_msg}")

        if outcome.reveal_npc:
            self.visible_inhabitants.add(outcome.reveal_npc)
            revealed_char = next(
                (char for char in self.world.characters if char.name == outcome.reveal_npc),
                None,
            )
            if revealed_char and all(
                char.name != revealed_char.name for char in self.current_inhabitants
            ):
                self.current_inhabitants.append(revealed_char)
            msg = f"[DISCOVERY] You noticed {outcome.reveal_npc} in the scene."
            self.history.append(msg)
            if self.interactive or self.verbose:
                print(f"  >>> {msg}")

        if outcome.reveal_object:
            self.visible_objects.add(outcome.reveal_object)
            msg = f"[DISCOVERY] You spotted the {outcome.reveal_object} nearby."
            self.history.append(msg)
            if self.interactive or self.verbose:
                print(f"  >>> {msg}")

        # 2. NPC leads reveal a connected location; movement remains a player choice.
        if (
            action
            and action.character_name
            and outcome.lead_to_known
            and not outcome.reveal_location
            and not outcome.target_tags
        ):
            c_name = action.character_name
            node_type, node_name = self.discover_new_node(
                node_type="loc",
                source_npc=c_name,
            )
            if node_type == "loc":
                new_loc = self.world.spawn(
                    self.current,
                    target_tags=(
                        self.world.locations[node_name].tags
                        if node_name in self.world.locations
                        else action.npc_goals
                    ),
                    genome=self.genome,
                    state=self,
                )
                if new_loc:
                    self.world.connect(self.current.name, new_loc.name)
                    self.discovered_locations.add(new_loc.name)
                    self._record_transition(
                        origin_location,
                        new_loc.name,
                        self.world.locations[new_loc.name].tags,
                        "tags",
                    )
                    lead_msg = f"[LEAD] {c_name} revealed the way to: {new_loc.name}"
                    self.history.append(lead_msg)
                    if self.interactive or self.verbose:
                        print(f"  >>> {lead_msg}")

        # 3. Only explicit move_to/move_to_tags effects force movement.
        moved = False
        if outcome.move_to:
            target_name = self.resolve_destination(move_to=outcome.move_to)
            if target_name:
                moved = self._move_to(target_name)
                if moved:
                    self._record_transition(
                        origin_location,
                        target_name,
                        self.world.locations[target_name].tags,
                        (
                            "navigation"
                            if action and action.source_type == "navigation"
                            else "forced_named"
                        ),
                    )

        elif outcome.move_to_tags:
            target_name = self.resolve_destination(move_to_tags=outcome.move_to_tags)
            if target_name:
                moved = self._move_to(target_name)
                if moved:
                    self._record_transition(
                        origin_location,
                        target_name,
                        outcome.move_to_tags,
                        "tags",
                    )

        # Mandatory encounters are cleared after one selected resolution.
        if action and (action.mandatory or action.encounter):
            self.resolved_encounters.add(action.source_name or action.name)

        # Encounter scenes may explicitly return to the node from which they were entered.
        if (
            action
            and action.return_to_previous
            and not moved
            and self.previous_location_name
            and self.previous_location_name != self.current.name
        ):
            return_target = self.previous_location_name
            self._move_to(return_target)
            moved = True
            return_msg = f"[ENCOUNTER] Returned to {return_target}."
            self.history.append(return_msg)
            if self.interactive or self.verbose:
                print(f"  >>> {return_msg}")

        destination_location = self.current.name
        if destination_location != origin_location:
            location_msg = f"[LOCATION] Transferred from {origin_location} to {destination_location}"
        else:
            location_msg = f"[LOCATION] Still at {origin_location}"
        self.history.append(location_msg)
        if self.interactive or self.verbose:
            print(f"  >>> Current location: {origin_location}")
            if destination_location != origin_location:
                print(f"  >>> Transferred to: {destination_location}")
            else:
                print(f"  >>> Staying in: {origin_location}")
        
        # 5. Resource & Inventory updates
        if action:
            if action.source_type != "navigation":
                self.visit_actions_performed.add(action.name)

            if action.required_coins > 0:
                self.coins -= action.required_coins
                self.update_power()
                coin_msg = f"[RESOURCES] Paid: {action.required_coins} coins (Balance: {self.coins})"
                self.history.append(coin_msg)
                if self.interactive or self.verbose: print(f"  >>> {coin_msg}")
            
            if action.collects_object:
                self.inventory.add(action.collects_object)
                self.update_power()
                item_msg = f"You collected the {action.collects_object}."
                self.history.append(item_msg)
                if self.interactive: print(f"  >>> {item_msg} (Inventory: {list(self.inventory)} | Power: {self.power:.2f})")
                    
            if action.consumes_object and action.active_item:
                if action.active_item in self.inventory:
                    self.inventory.remove(action.active_item)
                    self.update_power()
                    item_msg = f"You have utilized the {action.active_item}."
                    self.history.append(item_msg)
                    if self.interactive: print(f"  >>> {item_msg} (Remaining: {list(self.inventory)} | Power: {self.power:.2f})")

    def get_full_story(self):
        """Returns a string representation of the full story transcript."""
        return "\n".join(self.history)
