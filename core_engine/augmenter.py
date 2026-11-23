import os
import json
import yaml
import datetime
import copy
from utils.config_loader import config
from evaluator.llm_eval import LLMNarrativeEvaluator

class WorldAugmenter:
    def __init__(self):
        self.min_locations = config.get("augmentation.min_locations", 10)
        self.min_items = config.get("augmentation.min_items", 5)
        self.min_npcs = config.get("augmentation.min_npcs", 5)
        self.min_outcomes = config.get("augmentation.min_outcomes", 30)
        self.save_to_source = config.get("augmentation.save_to_source", False)
        self.output_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), 
            config.get("augmentation.output_dir", "augmentations")
        )
        
        # Lazy initialization of LLM because it's heavy
        self._llm = None

    @property
    def llm(self):
        if self._llm is None:
            # The augmentation's own model (augmentation.llm: Qwen3.5-4B on vLLM), not the judge's.
            self._llm = LLMNarrativeEvaluator(role="augmentation")
            print(f"World augmentation with {self._llm.model_name} (vLLM)...")
        return self._llm

    def get_current_metrics(self):
        locations = config.get("world_definition.locations", {})
        objects = config.get("world_definition.objects", [])
        characters = config.get("world_definition.characters", [])
        
        num_locations = len(locations)
        num_items = len(objects)
        num_npcs = len(characters)
        
        num_outcomes = 0
        for loc_data in locations.values():
            for action in loc_data.get("actions", []):
                num_outcomes += len(action.get("outcomes", []))
        
        return num_locations, num_items, num_npcs, num_outcomes

    def is_sufficient(self):
        num_loc, num_item, num_npc, num_out = self.get_current_metrics()
        print(f"Current World Metrics: Locations={num_loc}/{self.min_locations}, Items={num_item}/{self.min_items}, NPCs={num_npc}/{self.min_npcs}, Outcomes={num_out}/{self.min_outcomes}")
        return (num_loc >= self.min_locations and 
                num_item >= self.min_items and 
                num_npc >= self.min_npcs and 
                num_out >= self.min_outcomes)

    def augment(self, update_config=True):
        print("World is insufficient. Starting LLM augmentation...")
        current_world = config.get("world_definition", {})
        proposal = self.propose()
        if proposal is None:
            return None
        # Deep merge (a deep copy: the world in memory is not changed in place)
        final_world = copy.deepcopy(current_world)
        final_world['locations'] = {**current_world.get('locations', {}), **proposal["locations"]}
        final_world['objects'] = current_world.get('objects', []) + proposal["objects"]
        final_world['characters'] = current_world.get('characters', []) + proposal["characters"]

        # 4. Save and return
        print("Successfully merged augmented world data.")
        filepath = self.save_augmentation(final_world)
        if update_config:
            print("Updating config in memory with augmented world.")
            config.set_world_definition(final_world)
        return final_world, filepath

    @staticmethod
    def example_action(world):
        """A real action of this world, cut to what the engine needs (name; outcomes with desc and
        tags; target_tags or reveal_location where it has them): the shape a proposal must follow."""
        for place in (world.get("locations") or {}).values():
            for action in (place or {}).get("actions") or []:
                outcomes = [{key: o[key] for key in ("desc", "tags", "target_tags", "reveal_location") if key in o}
                            for o in (action.get("outcomes") or [])[:2] if isinstance(o, dict) and o.get("desc")]
                if action.get("name") and outcomes:
                    return {"name": action["name"], "outcomes": outcomes}
        return {"name": "Listen to the sailors", "outcomes": [
            {"desc": "They speak of a cove beyond the cliffs.", "tags": ["social", "discovery"],
             "target_tags": ["nature", "secluded"]},
            {"desc": "Talk of the weather, nothing more.", "tags": ["neutral"]}]}

    @staticmethod
    def normalize_actions(actions):
        """A proposal's actions in the engine's shape, from what small models commonly write instead:
        a string, {action, outcome} or {name, text}, snake_case names, target_tags on the action. Only
        fields the engine knows are kept; an action left without a name or an outcome is dropped."""
        from .content_rules import ACTION_FIELDS, OUTCOME_FIELDS

        def readable(name):
            name = str(name or "").strip()
            return name.replace("_", " ").capitalize() if "_" in name and " " not in name else name

        fixed = []
        for action in actions if isinstance(actions, list) else []:
            if isinstance(action, str):
                action = {"name": action}
            if not isinstance(action, dict):
                continue
            name = readable(action.get("name") or action.get("action") or action.get("title"))
            outcomes = action.get("outcomes")
            if not isinstance(outcomes, list) or not outcomes:
                text = action.get("outcome") or action.get("result") or action.get("text") or action.get("desc")
                outcomes = [text] if text else []
            shaped = []
            for outcome in outcomes:
                outcome = {"desc": outcome} if isinstance(outcome, str) else outcome
                if not isinstance(outcome, dict):
                    continue
                outcome = {key: value for key, value in outcome.items() if key in OUTCOME_FIELDS}
                outcome.setdefault("desc", "")
                if outcome["desc"]:
                    outcome.setdefault("tags", [])
                    shaped.append(outcome)
            if shaped and action.get("target_tags") and not any("target_tags" in o for o in shaped):
                shaped[0]["target_tags"] = list(action["target_tags"]) if isinstance(action["target_tags"], list) \
                    else [action["target_tags"]]
            if name and shaped:
                kept = {key: value for key, value in action.items()
                        if key in ACTION_FIELDS and key not in ("name", "outcomes")}
                fixed.append({"name": name, **kept, "outcomes": shaped})
        return fixed

    def propose(self):
        """The augmentation model's (augmentation.llm) proposal of new places, people and items for the active world, nothing written:
        {locations: {name: entry}, objects: [entry], characters: [entry]} (only places that are new
        scenarios), or None when it proposed nothing usable."""
        current_world = config.get("world_definition", {})
        existing_locs = list(current_world.get('locations', {}).keys())
        
        # Extract all existing tags to help the LLM create connections
        all_tags = set()
        for loc in current_world.get('locations', {}).values():
            all_tags.update(loc.get('tags', []))
        tag_cloud = ", ".join(sorted(list(all_tags)))

        prompt = f"""
### TASK: World Augmentation (Narrative Hubs)
### OBJECTIVE: Add NEW locations, items, and NPCs to the world.

### CURRENT WORLD (Patterns to follow):
- LOCATIONS: {", ".join(existing_locs)}
- TAG CLOUD: {tag_cloud}

### REQUIREMENTS (NON-NEGOTIABLE):
1. **NPCS AS HUB**: Every NEW character MUST have exactly one `known_locations` (list of location names) and one `known_objects` (list of object names) key.
2. **TAG REUSE**: Give NPCs broad tags (e.g. "urban", "guarded") so they appear in multiple locations. Give new locations at least 2 tags from the TAG CLOUD.
3. **ONLY NEW NODES**: Return ONLY new characters, items, and locations. Do NOT re-define Harbor or other existing items.
4. **PARAMETERIZED LOCATIONS**: Give every new location a `template_id` (its kind of place) and `parameters` (what makes this one differ). Variants of one template are welcome; the same template with the same parameters twice is rejected.
5. **TAG-LED DISCOVERY**: An outcome that opens a new path should say what kind of place it leads to with `target_tags` (e.g. ["nature", "secluded"]) rather than naming a place.
6. **ACTIONS**: Every action is an object with a readable `name` (what the player does, e.g. "Search the shelves")
   and a list of `outcomes`; every outcome has a `desc` (one or two sentences, what happens) and `tags`
   (from the TAG CLOUD, plus "success", "failure", "discovery" or "neutral"). An outcome may add `target_tags`
   (the kind of place it opens a path to) or `reveal_location` (an existing location's name). Give each new
   location and character 2 actions with 2 outcomes each. An action of this world, exactly in that shape:
{json.dumps(self.example_action(current_world), ensure_ascii=False)}
7. **FORMAT**:
{{
  "locations": {{
    "Alchemy Lab": {{ "tags": ["urban", "research"], "descriptions": ["Shelves of bubbling flasks."],
                     "template_id": "laboratory", "parameters": {{"focus": "potions"}}, "actions": [ACTION, ACTION] }}
  }},
  "objects": [
    {{ "name": "Rare Herb", "associated_tags": ["nature", "research"], "descriptions": ["A bitter, silver leaf."] }}
  ],
  "characters": [
    {{
      "name": "Alchemist",
      "associated_tags": ["urban", "social", "discovery"],
      "descriptions": ["A soot-stained woman with quick hands."],
      "known_locations": ["Alchemy Lab", "Secret Garden"],
      "known_objects": ["Rare Herb"],
      "actions": [ACTION, ACTION]
    }}
  ]
}}
(ACTION: an action object as in 6.)

### YOUR RESPONSE (JSON ONLY):
"""
        
        # 1. Call LLM
        raw_response = self.llm.generate(prompt)
        if hasattr(self, 'verbose') and self.verbose:
            print(f"LLM Response length: {len(raw_response) if raw_response else 0}")
            
        if not raw_response: return None
        
        # 2. Extract JSON
        new_data = self.llm.extract_json(raw_response)
        if not new_data:
            print("  CRITICAL: Failed to parse LLM response as JSON.")
            return None
            
        # 3. MERGE Logic (Additive)
        # Handle cases where LLM includes "new_locations" instead of "locations"
        raw_locs = new_data.get('locations', new_data.get('new_locations', {}))
        raw_objs = new_data.get('objects', new_data.get('new_objects', []))
        raw_chars = new_data.get('characters', new_data.get('new_characters', []))

        raw_locs = self._new_scenarios(current_world.get("locations", {}), raw_locs)
        for entry in [*raw_locs.values(), *(raw_chars if isinstance(raw_chars, list) else [])]:
            if isinstance(entry, dict) and "actions" in entry:  # the engine's action shape (normalize_actions)
                entry["actions"] = self.normalize_actions(entry["actions"])

        if not raw_locs and not raw_objs and not raw_chars:
            print("  WARNING: Augmented data is empty. Skipping file update to prevent corruption.")
            return None
        names = {entry.get("name") for entry in current_world.get("objects", []) + current_world.get("characters", [])
                 if isinstance(entry, dict)}
        return {"locations": raw_locs,
                "objects": [o for o in raw_objs if isinstance(o, dict) and o.get("name") and o["name"] not in names],
                "characters": [c for c in raw_chars if isinstance(c, dict) and c.get("name") and c["name"] not in names]}

    @staticmethod
    def _new_scenarios(existing, generated):
        """The generated locations that are new scenarios: not a place already there (whatever
        its case or spacing), not the same template with the same parameters, not the same
        content (builder.scenario_key), and not twice within the generation."""
        from core_engine.builder import scenario_key
        from core_engine.world import WorldGraph

        if not isinstance(generated, dict):
            return {}
        names = {WorldGraph.place_key(name) for name in existing}
        scenarios = {scenario_key(name, data) for name, data in (existing or {}).items()}
        accepted = {}
        for name, data in generated.items():
            if not isinstance(data, dict):
                continue
            key, scenario = WorldGraph.place_key(name), scenario_key(name, data)
            if key in names or scenario in scenarios:
                print(f"  Skipping a generated location that repeats one already there: {name}")
                continue
            names.add(key)
            scenarios.add(scenario)
            accepted[name] = data
        return accepted

    def save_proposal(self, proposal):
        """Keep a proposal (propose()) for review, nothing else written: augmentations/run_<time>/proposal.yaml
        holds only the new locations, objects (items) and characters (NPCs), the form
        ``python main.py --apply-augmentation`` and the studio's Grow the world add. Returns its path."""
        run_dir = os.path.join(self.output_dir, f"run_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}")
        os.makedirs(run_dir, exist_ok=True)
        path = os.path.join(run_dir, "proposal.yaml")
        with open(path, "w", encoding="utf-8") as stream:
            yaml.safe_dump({key: proposal.get(key) or ({} if key == "locations" else [])
                            for key in ("locations", "objects", "characters")}, stream, sort_keys=False,
                           allow_unicode=True)
        return path

    def save_augmentation(self, data):
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = os.path.join(self.output_dir, f"run_{timestamp}")
        
        if not os.path.exists(run_dir):
            os.makedirs(run_dir)
            
        # 1. Save full monolithic file for debugging
        filename = "world_full.yaml"
        filepath = os.path.join(run_dir, filename)
        with open(filepath, "w") as f:
            yaml.dump(data, f, sort_keys=False, default_flow_style=False)
            
        print(f"SUCCESS: Full augmented data saved to: {filepath}")

        # 2. Save modular files within the run directory
        print(f"Saving modular config files to {run_dir}...")
        self.apply_to_modular_config(data, run_dir)

        # 3. Optionally update the main project source files
        if self.save_to_source:
            project_root = os.path.dirname(os.path.dirname(__file__))
            config_dir = os.path.join(project_root, "config")
            print(f"Updating main project config at {config_dir}...")
            self.apply_to_modular_config(data, config_dir)
            
        return filepath

    def apply_to_modular_config(self, data, target_dir):
        if not os.path.exists(target_dir):
            os.makedirs(target_dir)

        # Safety check: Do not overwrite if data is empty or missing core keys
        new_locs = data.get("locations", {})
        new_objs = data.get("objects", [])
        new_chars = data.get("characters", [])

        if not new_locs and not new_objs and not new_chars:
            print("WARNING: Augmented data is empty. Skipping file update to prevent corruption.")
            return

        # 1. Locations
        if new_locs:
            loc_path = os.path.join(target_dir, "locations.yaml")
            with open(loc_path, "w") as f:
                yaml.dump({"world_definition": {"locations": new_locs}}, f, sort_keys=False)
            print(f"Written: {loc_path}")

        # 2. Items
        if new_objs:
            items_path = os.path.join(target_dir, "items.yaml")
            with open(items_path, "w") as f:
                yaml.dump({"world_definition": {"objects": new_objs}}, f, sort_keys=False)
            print(f"Written: {items_path}")

        # 3. NPCs
        if new_chars:
            npcs_path = os.path.join(target_dir, "npcs.yaml")
            with open(npcs_path, "w") as f:
                yaml.dump({"world_definition": {"characters": new_chars}}, f, sort_keys=False)
            print(f"Written: {npcs_path}")

if __name__ == "__main__":
    augmenter = WorldAugmenter()
    found = augmenter.propose()  # review first: a proposal is kept, the configuration is not changed
    if found:
        print(f"Proposal kept for review: {augmenter.save_proposal(found)}\n"
              "Add it with: python main.py --apply-augmentation <that file>")
