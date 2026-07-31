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
            print("Initializing shared Gemma/vLLM runtime for augmentation...")
            self._llm = LLMNarrativeEvaluator()
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
4. **PARAMETERIZED LOCATIONS**: Every location must include `template_id` and a `parameters` object. Reuse a template for variants, but never repeat the same template with the same parameters.
5. **TAG-DRIVEN LINKS**: Outcomes that reveal a route must use `target_tags`; do not create `linked_location`, `reveal_location`, or a predefined transition unless it is an unavoidable forced move.
6. **FORMAT**:
{{
  "locations": {{
    "Alchemy Lab": {{
      "template_id": "specialist_workspace",
      "parameters": {{"discipline": "alchemy", "district": "citadel"}},
      "tags": ["urban", "research"],
      "actions": [
        {{
          "name": "Study the restricted shelves",
          "outcomes": [
            {{
              "desc": "A clue points toward a secluded garden.",
              "tags": ["discovery", "nature"],
              "spawn": true,
              "target_tags": ["nature", "secluded"]
            }}
          ]
        }}
      ]
    }}
  }},
  "objects": [
    {{ "name": "Rare Herb", "associated_tags": ["nature", "research"] }}
  ],
  "characters": [
    {{ 
      "name": "Alchemist",
      "associated_tags": ["urban", "social", "discovery"],
      "known_locations": ["Alchemy Lab", "Secret Garden"],
      "known_objects": ["Rare Herb"],
      "actions": [...]
    }}
  ]
}}

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
        raw_locs = self._deduplicate_parameterized_locations(
            current_world.get("locations", {}),
            raw_locs,
        )

        if not raw_locs and not raw_objs and not raw_chars:
            print("  WARNING: Augmented data is empty. Skipping file update to prevent corruption.")
            return None

        # Deep merge
        final_world = copy.deepcopy(current_world)
        final_world['locations'] = {**current_world.get('locations', {}), **raw_locs}
        final_world['objects'] = current_world.get('objects', []) + raw_objs
        final_world['characters'] = current_world.get('characters', []) + raw_chars

        # 4. Save and return
        print("Successfully merged augmented world data.")
        filepath = self.save_augmentation(final_world)
        if update_config:
            print("Updating config in memory with augmented world.")
            config.set_world_definition(final_world)
        
        return final_world, filepath

    @staticmethod
    def _scenario_key(name, data):
        template_id = data.get("template_id") or "_".join(
            str(name).strip().casefold().split()
        )
        parameters = tuple(sorted(
            (str(key), repr(value))
            for key, value in (data.get("parameters") or {}).items()
        ))
        return str(template_id), parameters

    def _deduplicate_parameterized_locations(self, existing, generated):
        """Drop duplicate names and duplicate template/parameter instances."""
        if not isinstance(generated, dict):
            return {}

        seen_names = {
            " ".join(str(name).strip().casefold().split())
            for name in existing
        }
        seen_scenarios = {
            self._scenario_key(name, data)
            for name, data in existing.items()
        }
        accepted = {}
        for name, data in generated.items():
            if not isinstance(data, dict):
                continue
            normalized_name = " ".join(str(name).strip().casefold().split())
            scenario_key = self._scenario_key(name, data)
            if normalized_name in seen_names or scenario_key in seen_scenarios:
                print(f"Skipping duplicate generated scenario: {name}")
                continue
            if not data.get("template_id") or not isinstance(data.get("parameters"), dict):
                print(f"Skipping non-parameterized generated location: {name}")
                continue
            seen_names.add(normalized_name)
            seen_scenarios.add(scenario_key)
            accepted[name] = data
        return accepted

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
    augmenter.augment()
