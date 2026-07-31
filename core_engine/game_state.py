import random
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
        self.current = world.locations[start]

        self.health = config.get("game_state.initial_health", 8)
        self.actions_taken = 0

        self.tags_seen = []
        self.history = []
        
        # Track location visits
        self.location_counts = {start: 1}
        self.path = [start] # Sequence of locations
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

    def _should_commit_to_new_path(self, target_name):
        if not target_name or target_name not in self.world.locations:
            return False
        target = self.world.locations[target_name]
        score = semantic_tag_similarity(self.current.tags, target.tags)
        score += self.genome.voyage_bias
        score += self.genome.discovery_bias * 0.4
        score -= self.location_counts.get(target_name, 0) * 0.6
        threshold = config.get("game_state.auto_travel_commit_threshold", 1.2)
        return score >= threshold

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

    def _rank_actions(self, actions):
        ranked = []
        for a in actions:
            all_o_tags = []
            discovery_score = 0
            movement_score = 0
            recovery_score = 0
            for o in a.outcomes:
                all_o_tags.extend(o.tags)
                if o.spawn or o.reveal_location or o.reveal_npc or o.reveal_object:
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

            is_movement = any(o.move_to or o.move_to_tags or o.spawn or o.lead_to_known or o.reveal_location for o in a.outcomes)
            if is_movement:
                stay_penalty_power = config.get("action_selection.stay_penalty_power", 2.0)
                weight *= (1.0 + (self.genome.voyage_bias * self.consecutive_steps_in_location) ** stay_penalty_power)

            ranked.append((weight, a))

        ranked.sort(key=lambda pair: (pair[0], pair[1].name), reverse=True)
        return ranked


    def get_available_actions(self):
        """Returns a list of valid actions for the current state with narrative visibility."""
        # 1. Detect New Location Entry or Stale Inhabitants
        if not self.current_inhabitants or self.current.name != self.last_location_name:
            self.last_location_name = self.current.name
            
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
            if a.name not in self.visit_actions_performed or a.name.startswith("Move to") or a.name.startswith("Approach")
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
                    item_desc = self._pick_text(item.descriptions, offset=len(item.name)) if item.descriptions else item.name
                    import re
                    a.name = re.sub(r'\{.*?\}', item_desc, a.name)
            
            if a.required_object:
                if a.required_object not in self.inventory: continue
                a.active_item = a.required_object 
            
            if a.required_coins > self.coins: continue
            if a.collects_object and a.collects_object in self.inventory: continue
                
            final_actions.append(a)

        # 4. Navigation
        for loc_name in sorted(self.current.connected):
            target_loc = self.world.locations.get(loc_name)
            name = f"Move to {loc_name}"
            if target_loc and target_loc.interact_prompts:
                name = self._pick_text(target_loc.interact_prompts, offset=len(loc_name))
            
            navigation_action = Action(
                name=name,
                outcomes=[Outcome(desc=f"You travel to {loc_name}.", move_to=loc_name)]
            )
            final_actions.append(navigation_action)
        
        return final_actions

    def step(self):
        """Executes one simulation step."""
        if self.ended:
            return False

        actions = self.get_available_actions()
        if not actions:
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
            # Present the most narratively relevant choices first.
            options = [action for _, action in self._rank_actions(actions)[:min(5, len(actions))]]
            
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
            ranked_actions = self._rank_actions(actions)
            action = ranked_actions[0][1]

        # Record beginning of story segment
        is_new_loc = self.current.name != self.last_location_name
        self.last_location_name = self.current.name
        self.history.append(f"> {action.name}")
        
        # AI decides outcome based on Genome (Luck)
        outcome = action.choose_outcome(self.genome, state=self)
        self.actions_taken += 1
        self.consecutive_steps_in_location += 1 # Increment stay counter
        
        # Reset stay counter if we move
        if outcome.move_to:
            self.consecutive_steps_in_location = 0

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
        if move_to and move_to in self.world.locations:
            return move_to

        if not move_to_tags:
            return None

        # Score every location in the world to find the best match for the outcome's intent
        best_target = None
        max_score = -1
        
        candidates = list(self.world.locations.values())
        
        for loc in candidates:
            if loc.name == self.current.name: continue
            
            # 1. Tag Overlap Score
            overlap = len(set(move_to_tags) & loc.tags)
            if overlap == 0: continue
            
            # 2. Genome Bias Score
            bias_score = 0.1
            if "urban" in loc.tags: bias_score += self.genome.bias_urban
            if "maritime" in loc.tags: bias_score += self.genome.bias_maritime
            if "social" in loc.tags: bias_score += self.genome.bias_social
            if "threat" in loc.tags: bias_score += self.genome.bias_threat
            if "stealth" in loc.tags: bias_score += self.genome.bias_stealth
            if "luxury" in loc.tags: bias_score += self.genome.bias_luxury
            
            total_score = overlap * bias_score
            if total_score > max_score:
                max_score = total_score
                best_target = loc.name
                
        return best_target

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
        if target_name not in self.world.locations:
            forced_loc = self.world.spawn(
                self.current,
                force_target=target_name,
                state=self,
                allow_special=(target_name == "Dungeon"),
            )
            if forced_loc:
                spawn_msg = f"[SPAWN] A situational path opens to: {forced_loc.name}"
                self.history.append(spawn_msg)
                if self.interactive: print(f"  >>> {spawn_msg}")
                self.current.connect(forced_loc)
            else:
                return False

        self.last_location_name = self.current.name
        self.current = self.world.locations[target_name]
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
        
        self.tags_seen.append(list(situational_tags))
        self.history.append(f"RESULT: {outcome.desc}")
        self.history.append(f"[LOCATION] Current location: {origin_location}")

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
            print(f"  >>> Health: {self.health} | Coins: {self.coins} | Power: {self.power:.2f}")

        # 1. Reveal and spawn logic
        if outcome.spawn or outcome.reveal_location:
            target = outcome.reveal_location if outcome.reveal_location else None
            new_loc = self.world.spawn(
                self.current,
                force_target=target,
                state=self,
                allow_special=(target == "Dungeon"),
            )
            if new_loc:
                self.current.connect(new_loc)
                spawn_msg = f"[SPAWN] New path discovered: {new_loc.name}"
                self.history.append(spawn_msg)
                if self.interactive or self.verbose:
                    print(f"  >>> {spawn_msg}")
                if outcome.move_to is None and self._should_commit_to_new_path(new_loc.name):
                    outcome.move_to = new_loc.name
                    travel_msg = f"[VOYAGE] The opening draws you toward {new_loc.name}."
                    self.history.append(travel_msg)
                    if self.interactive or self.verbose:
                        print(f"  >>> {travel_msg}")

        if outcome.reveal_npc:
            self.visible_inhabitants.add(outcome.reveal_npc)
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

        # 2. NPC-driven leads can unlock travel even if the action itself does not move
        if action and action.character_name:
            c_name = action.character_name
            if self.character_interactions[c_name] % 2 == 0 or "success" in outcome.tags or "social" in outcome.tags:
                node_type, node_name = self.discover_new_node(source_npc=c_name)
                if node_type == "loc":
                    new_loc = self.world.spawn(self.current, force_target=node_name, state=self)
                    if new_loc:
                        self.current.connect(new_loc)
                        lead_msg = f"[LEAD] {c_name} told you about the way to: {new_loc.name}"
                        self.history.append(lead_msg)
                        if self.interactive or self.verbose:
                            print(f"  >>> {lead_msg}")
                        if outcome.move_to is None and self._should_commit_to_new_path(new_loc.name):
                            outcome.move_to = new_loc.name

        # 3. Handle Regular Movement
        if outcome.move_to:
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
                    
                    self.history.append(f"\n >>> [LEAD] {char.name} led you to: {target}")
                    if self.interactive: print(f" >>> {char.name} leads the way to the {target}...")
                    self._move_to(target)

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
            if not action.name.startswith("Move to"):
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
