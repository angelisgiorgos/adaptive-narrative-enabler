import os
import re
import yaml
from utils.llm_suggester import LLMSuggester


class ConfigEditor:
    FILE_OPTIONS = {
        "1": {"label": "hyperparameters", "path": "config/hyperparameters.yaml", "kind": "hyperparameters"},
        "2": {"label": "items", "path": "config/items.yaml", "kind": "items"},
        "3": {"label": "locations", "path": "config/locations.yaml", "kind": "locations"},
        "4": {"label": "npcs", "path": "config/npcs.yaml", "kind": "npcs"},
    }

    COMMON_OUTCOME_TAGS = [
        "neutral",
        "success",
        "failure",
        "threat",
        "social",
        "stealth",
        "combat",
        "guards",
        "spawn",
        "setup_clue",
    ]

    TAG_HINTS = {
        "neutral": "Calm or steady outcome with no major dramatic swing.",
        "success": "A positive outcome that rewards the player.",
        "failure": "A setback, failed attempt, or partial loss.",
        "threat": "Danger, pressure, or harm to the player.",
        "social": "Conversation, negotiation, or character interaction.",
        "stealth": "Quiet movement, secrecy, or covert progress.",
        "combat": "Physical conflict or direct confrontation.",
        "guards": "Security, law, or restricted-area pressure.",
        "spawn": "Unlocks or reveals a new path or place.",
        "setup_clue": "Plants a clue that can pay off later in the story.",
    }

    INTERACTION_POINT_FIELDS = [
        ("id", "text"),
        ("label", "text"),
        ("prompt", "text"),
        ("linked_action", "text"),
        ("linked_outcome", "text"),
        ("linked_location", "text"),
        ("linked_npc", "text"),
        ("linked_item", "text"),
        ("notes", "text"),
    ]

    def __init__(self, project_root):
        self.project_root = project_root
        self._suggester = None

    def run(self):
        print("\n" + "=" * 60)
        print("Interactive Story Config Editor")
        print("=" * 60)
        print("You can browse the current story, inspect actions, edit tags,")
        print("or still use JSON-style paths when you want precise control.")
        print("Example paths:")
        print("  world_definition.locations.Harbor.actions[0].outcomes[0].desc")
        print("  world_definition.characters[0].name")
        print("  evolution.pop_size")
        print("=" * 60)

        while True:
            file_key = self._choose_file()
            if file_key is None:
                return False

            file_info = self.FILE_OPTIONS[file_key]
            file_path = os.path.join(self.project_root, file_info["path"])
            changed = self._edit_file(file_path, file_info)
            if changed:
                print(f"Saved changes to {file_info['path']}")

            next_action = input(
                "\nPress Enter to keep editing, type 'run' to start the story, or 'quit' to exit: "
            ).strip().lower()
            if next_action == "run":
                return True
            if next_action == "quit":
                return False

    def browse_story(self):
        print("\n" + "=" * 60)
        print("Current Story Setup")
        print("=" * 60)
        for file_info in self.FILE_OPTIONS.values():
            data = self._load_yaml(os.path.join(self.project_root, file_info["path"]))
            print(f"\n[{file_info['label'].upper()}]")
            self._print_existing_configurations(data, file_info["kind"])
        print("\n" + "=" * 60)

    def _choose_file(self):
        print("\nChoose a file to edit:")
        for key, info in self.FILE_OPTIONS.items():
            print(f"  {key}. {info['label']:<15} -> {info['path']}")
        print("  q. quit")

        while True:
            choice = input("Selection: ").strip().lower()
            if choice in self.FILE_OPTIONS:
                return choice
            if choice in {"q", "quit"}:
                return None
            print("Please choose 1, 2, 3, 4, or q.")

    def _edit_file(self, file_path, file_info):
        data = self._load_yaml(file_path)
        changed = False

        while True:
            print(f"\nEditing: {os.path.relpath(file_path, self.project_root)}")
            print("Actions: show, edit, set, append, delete, list, back")
            action = input("Action: ").strip().lower()

            if action == "back":
                if changed:
                    self._save_yaml(file_path, data)
                return changed

            if action == "list":
                self._print_preview(data)
                continue

            if action == "show":
                self._guided_show(data, file_info["kind"])
                continue

            if action == "edit":
                if self._guided_edit(data, file_info["kind"]):
                    changed = True
                continue

            if action == "delete":
                if self._guided_delete(data, file_info["kind"]):
                    changed = True
                continue

            path = input("JSON-style path: ").strip()
            if not path:
                print("A path is required.")
                continue

            if action == "set":
                raw_value = self._read_value("Value")
                try:
                    self._set_path(data, path, raw_value)
                    changed = True
                    print("Value updated.")
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    print(f"Could not update path: {exc}")
                continue

            if action == "append":
                raw_value = self._read_value("Value to append")
                try:
                    self._append_path(data, path, raw_value)
                    changed = True
                    print("Value appended.")
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    print(f"Could not append value: {exc}")
                continue

            print("Unknown action. Use show, edit, set, append, delete, list, or back.")

    def _guided_show(self, data, kind):
        self._print_existing_configurations(data, kind)
        if kind == "hyperparameters":
            path = input("\nOptional JSON-style path to inspect in detail (Enter to skip): ").strip()
            if not path:
                return
            ok, value = self._get_path(data, path)
            if ok:
                print(yaml.safe_dump(value, sort_keys=False, allow_unicode=False).strip())
            else:
                print(value)
            return

        collection = self._get_collection(data, kind)
        if not collection:
            print("No existing story configuration found in this file yet.")
            return

        entry_name = self._choose_entry_name(data, kind, allow_cancel=True)
        if entry_name is None:
            return
        entry = self._get_entry(data, kind, entry_name)
        self._print_entry_details(kind, entry_name, entry)

    def _guided_edit(self, data, kind):
        if kind == "hyperparameters":
            self._print_existing_configurations(data, kind)
            path = input("JSON-style path to edit: ").strip()
            if not path:
                print("Edit cancelled.")
                return False
            raw_value = self._read_value("Value")
            self._set_path(data, path, raw_value)
            print("Value updated.")
            return True

        self._print_existing_configurations(data, kind)
        entry_name = self._choose_entry_name(data, kind, allow_cancel=True)
        if entry_name is None:
            return False

        entry = self._get_entry(data, kind, entry_name)
        return self._edit_entry(kind, entry_name, entry)

    def _guided_delete(self, data, kind):
        self._print_existing_configurations(data, kind)

        if kind == "hyperparameters":
            path = input("JSON-style path to delete: ").strip()
            if not path:
                print("Delete cancelled.")
                return False
            ok, value = self._get_path(data, path)
            if ok:
                print("Current value:")
                print(yaml.safe_dump(value, sort_keys=False, allow_unicode=False).strip())
            else:
                print(value)
                return False
            confirm = input("Type 'delete' to confirm: ").strip().lower()
            if confirm != "delete":
                print("Delete cancelled.")
                return False
            self._delete_path(data, path)
            print("Path deleted.")
            return True

        print("\nDelete options:")
        print("  1. Delete a whole story entry")
        print("  2. Delete an action from an entry")
        print("  3. Delete an outcome from an action")
        print("  4. Delete by JSON-style path")
        print("  b. back")
        choice = input("Selection: ").strip().lower()

        if choice in {"b", "back", ""}:
            return False
        if choice == "1":
            return self._delete_entry(data, kind)
        if choice == "2":
            return self._delete_action(data, kind)
        if choice == "3":
            return self._delete_outcome(data, kind)
        if choice == "4":
            path = input("JSON-style path to delete: ").strip()
            if not path:
                return False
            ok, value = self._get_path(data, path)
            if ok:
                print("Current value:")
                print(yaml.safe_dump(value, sort_keys=False, allow_unicode=False).strip())
                confirm = input("Type 'delete' to confirm: ").strip().lower()
                if confirm == "delete":
                    self._delete_path(data, path)
                    print("Path deleted.")
                    return True
            else:
                print(value)
            return False

        print("Unknown delete option.")
        return False

    def _print_existing_configurations(self, data, kind):
        if kind == "hyperparameters":
            print("\nExisting configuration sections:")
            for key, value in data.items():
                value_type = type(value).__name__
                print(f"  - {key} ({value_type})")
            return

        collection = self._get_collection(data, kind)
        if not collection:
            print("\nNo configurations found.")
            return

        print("\nExisting story configurations:")
        if kind == "locations":
            for name, entry in collection.items():
                action_count = len(entry.get("actions", []))
                tags = ", ".join(entry.get("tags", [])) or "no tags"
                print(f"  - {name}: {action_count} action(s), tags: {tags}")
        else:
            for entry in collection:
                name = entry.get("name", "Unnamed")
                action_count = len(entry.get("actions", []))
                tag_key = "associated_tags"
                tags = ", ".join(entry.get(tag_key, [])) or "no tags"
                npc_type = f", {self._unique_label(entry)}" if kind == "npcs" else ""
                print(f"  - {name}: {action_count} action(s), tags: {tags}{npc_type}")

    def _print_entry_details(self, kind, entry_name, entry):
        print("\n" + "=" * 60)
        print(f"{entry_name}")
        print("=" * 60)

        if kind == "locations":
            print(f"Location tags: {', '.join(entry.get('tags', [])) or 'none'}")
            print(f"Goals: {', '.join(entry.get('goals', [])) or 'none'}")
            print("Interaction prompts:")
            for prompt in entry.get("interact_prompts", []):
                print(f"  - {prompt}")
            print("Distant descriptions:")
            for desc in entry.get("distant_descriptions", []) or entry.get("descriptions", []):
                print(f"  - {desc}")
            print("Entering text:")
            for desc in entry.get("entered_descriptions", []) or entry.get("entering_text", []):
                print(f"  - {desc}")
            interaction_points = entry.get("interaction_points", [])
            if interaction_points:
                print("Interaction points:")
                for point in interaction_points:
                    print(f"  - {self._describe_interaction_point(point)}")
        else:
            print(f"Associated tags: {', '.join(entry.get('associated_tags', [])) or 'none'}")
            if kind == "npcs":
                print(f"NPC type: {self._unique_label(entry)}")
                print(f"Encounter event: {self._encounter_label(entry)}")
            print(f"Goals: {', '.join(entry.get('goals', [])) or 'none'}")
            descriptions = entry.get("descriptions", [])
            if descriptions:
                print("Descriptions:")
                for desc in descriptions:
                    print(f"  - {desc}")

        actions = entry.get("actions", [])
        if not actions:
            print("\nNo actions configured.")
            return

        print("\nActions and what they do:")
        for index, action in enumerate(actions, start=1):
            print(f"  {index}. {self._describe_action(action)}")
            for out_index, outcome in enumerate(action.get("outcomes", []), start=1):
                print(f"     Outcome {out_index}: {self._describe_outcome(outcome)}")

    def _describe_action(self, action):
        requirements = []
        if action.get("id"):
            requirements.append(f"id '{action['id']}'")
        if action.get("required_tag"):
            requirements.append(f"needs an item tagged '{action['required_tag']}'")
        if action.get("required_object"):
            requirements.append(f"needs the object '{action['required_object']}'")
        if action.get("required_coins"):
            requirements.append(f"costs {action['required_coins']} coin(s)")
        if action.get("collects_object"):
            requirements.append(f"collects '{action['collects_object']}'")
        if action.get("consumes_object"):
            requirements.append("consumes the used object")
        if action.get("repeat"):
            requirements.append(f"offered {action['repeat'].replace('_', ' ')}")
        if action.get("exclusive_group"):
            requirements.append(f"one choice of group '{action['exclusive_group']}'")

        req_text = f" Requirements: {', '.join(requirements)}." if requirements else ""
        outcome_count = len(action.get("outcomes", []))
        return f"{action.get('name', 'Unnamed action')} -> offers {outcome_count} possible outcome(s).{req_text}"

    def _describe_outcome(self, outcome):
        effects = []
        if outcome.get("id"):
            effects.append(f"id {outcome['id']}")
        tags = outcome.get("tags", [])
        if tags:
            tag_explanations = [f"{tag} ({self._tag_hint(tag)})" for tag in tags]
            effects.append("tags: " + ", ".join(tag_explanations))
        if outcome.get("health_change"):
            effects.append(f"health {outcome['health_change']:+}")
        if outcome.get("coin_change"):
            effects.append(f"coins {outcome['coin_change']:+}")
        if outcome.get("move_to"):
            effects.append(f"moves to {outcome['move_to']}")
        if outcome.get("reveal_location"):
            effects.append(f"reveals location {outcome['reveal_location']}")
        if outcome.get("linked_location") and outcome.get("linked_location") != outcome.get("reveal_location"):
            effects.append(f"linked location {outcome['linked_location']}")
        if outcome.get("reveal_npc"):
            effects.append(f"reveals NPC {outcome['reveal_npc']}")
        if outcome.get("linked_npc") and outcome.get("linked_npc") != outcome.get("reveal_npc"):
            effects.append(f"linked NPC {outcome['linked_npc']}")
        if outcome.get("reveal_object"):
            effects.append(f"reveals item {outcome['reveal_object']}")
        if outcome.get("linked_item") and outcome.get("linked_item") != outcome.get("reveal_object"):
            effects.append(f"linked item {outcome['linked_item']}")
        if outcome.get("spawn"):
            effects.append("can open a new path")
        if outcome.get("lead_to_known"):
            effects.append("can lead the traveler to a known place")

        effect_text = "; ".join(effects) if effects else "no extra gameplay effects"
        return f"{outcome.get('desc', 'No description')} [{effect_text}]"

    def _tag_hint(self, tag):
        if tag.startswith("setup_"):
            return "plants a setup beat for a later payoff"
        if tag.startswith("payoff_"):
            return "acts as a payoff beat for an earlier setup"
        return self.TAG_HINTS.get(tag, "custom story tone or gameplay signal")

    def _describe_interaction_point(self, point):
        parts = []
        title = point.get("label") or point.get("prompt") or point.get("id") or "Unnamed point"
        parts.append(title)
        if point.get("id"):
            parts.append(f"id={point['id']}")
        if point.get("prompt"):
            parts.append(f"prompt='{point['prompt']}'")
        for field in ("linked_action", "linked_outcome", "linked_location", "linked_npc", "linked_item"):
            if point.get(field):
                parts.append(f"{field}={point[field]}")
        if point.get("notes"):
            parts.append(point["notes"])
        return " | ".join(parts)

    def _edit_entry(self, kind, entry_name, entry):
        changed = False
        while True:
            print(f"\nEditing entry: {entry_name}")
            print("  1. Show this entry")
            print("  2. Edit core text")
            print("  3. Edit entry tags")
            print("  4. Edit actions")
            if kind == "locations":
                print("  5. Edit interaction points")
            if kind == "npcs":
                print(f"  5. Unique NPC (currently: {self._unique_label(entry)})")
                print(f"  6. Encounter event (currently: {self._encounter_label(entry)})")
            print("  b. back")
            choice = input("Selection: ").strip().lower()

            if choice in {"b", "back", ""}:
                return changed
            if choice == "1":
                self._print_entry_details(kind, entry_name, entry)
                continue
            if choice == "2":
                if self._edit_core_text(kind, entry):
                    changed = True
                continue
            if choice == "3":
                tag_key = "tags" if kind == "locations" else "associated_tags"
                if self._edit_simple_list(
                    entry,
                    tag_key,
                    f"{entry_name} tags",
                    suggestion_context=self._build_suggestion_context(kind, entry, tag_key),
                ):
                    changed = True
                continue
            if choice == "4":
                if self._edit_actions(entry):
                    changed = True
                continue
            if kind == "locations" and choice == "5":
                if self._edit_interaction_points(entry):
                    changed = True
                continue
            if kind == "npcs" and choice == "5":
                unique = self._ask_yes_no(
                    "Unique NPC? (yes: lives in one location for the whole game; "
                    "no: generic, may appear wherever it fits, e.g. guards)",
                    default=bool(entry.get("unique", False)),
                )
                if entry.get("unique") != unique:
                    entry["unique"] = unique
                    changed = True
                continue
            if kind == "npcs" and choice == "6":
                if self._edit_encounter(entry):
                    changed = True
                continue
            prompt = {
                "locations": "Please choose 1, 2, 3, 4, 5, or b.",
                "npcs": "Please choose 1, 2, 3, 4, 5, 6, or b.",
            }.get(kind, "Please choose 1, 2, 3, 4, or b.")
            print(prompt)

    def _edit_core_text(self, kind, entry):
        changed = False
        fields = []
        if kind == "locations":
            fields = [
                ("goals", "Goals"),
                ("interact_prompts", "Interaction prompts"),
                ("distant_descriptions", "Distant descriptions"),
                ("entered_descriptions", "Entered descriptions"),
            ]
        elif kind == "npcs":
            fields = [
                ("goals", "Goals"),
                ("descriptions", "Descriptions"),
                ("known_locations", "Known locations"),
                ("known_objects", "Known objects"),
            ]
        else:
            fields = [
                ("goals", "Goals"),
                ("descriptions", "Descriptions"),
                ("interact_prompts", "Interaction prompts"),
            ]

        while True:
            print("\nCore text fields:")
            for index, (_, label) in enumerate(fields, start=1):
                print(f"  {index}. {label}")
            print("  b. back")
            choice = input("Selection: ").strip().lower()
            if choice in {"b", "back", ""}:
                return changed
            if choice.isdigit() and 1 <= int(choice) <= len(fields):
                field_name, label = fields[int(choice) - 1]
                if self._edit_simple_list(
                    entry,
                    field_name,
                    label,
                    suggestion_context=self._build_suggestion_context(kind, entry, field_name),
                ):
                    changed = True
                continue
            print("Please choose one of the listed fields.")

    def _edit_simple_list(self, entry, field_name, label, suggestion_context=None):
        entry.setdefault(field_name, [])
        changed = False

        while True:
            values = entry.get(field_name, [])
            print(f"\n{label}:")
            for index, value in enumerate(values, start=1):
                print(f"  {index}. {value}")
            if not values:
                print("  (empty)")

            print("Options: add, replace, delete, suggest, back")
            choice = input("Selection: ").strip().lower()
            if choice in {"back", "b", ""}:
                return changed
            if choice == "add":
                value = input("New value: ").strip()
                if value:
                    values.append(value)
                    changed = True
                continue
            if choice == "replace":
                index = self._choose_index(values, "Item number to replace")
                if index is None:
                    continue
                value = input("New value: ").strip()
                if value:
                    values[index] = value
                    changed = True
                continue
            if choice == "delete":
                index = self._choose_index(values, "Item number to delete")
                if index is None:
                    continue
                removed = values.pop(index)
                print(f"Removed: {removed}")
                changed = True
                continue
            if choice == "suggest":
                suggestions = self._generate_field_suggestions(label, suggestion_context or {})
                if not suggestions:
                    continue
                if self._apply_suggestions_to_list(values, suggestions):
                    changed = True
                continue
            print("Use add, replace, delete, suggest, or back.")

    def _build_suggestion_context(self, kind, entry, field_name):
        full_case = self._load_full_case_context()
        entry_name = entry.get("name")
        if kind == "locations":
            entry_name = next(
                (
                    name for name, loc in full_case.get("locations", {}).get("world_definition", {}).get("locations", {}).items()
                    if loc == entry
                ),
                entry.get("name"),
            )
        context = {
            "selection": {
                "kind": kind,
                "field_name": field_name,
                "entry_name": entry_name,
            },
            "current_entry": {
                "name": entry_name,
                "tags": entry.get("tags", []) if kind == "locations" else entry.get("associated_tags", []),
                "goals": entry.get("goals", []),
                "existing_values": entry.get(field_name, []),
                "interact_prompts": entry.get("interact_prompts", []),
                "distant_descriptions": entry.get("distant_descriptions", []),
                "entered_descriptions": entry.get("entered_descriptions", []),
                "descriptions": entry.get("descriptions", []),
                "action_names": [action.get("name") for action in entry.get("actions", [])],
                "actions": entry.get("actions", []),
            },
            "whole_case": full_case,
        }
        if kind == "locations":
            context["current_entry"]["interaction_points"] = entry.get("interaction_points", [])
        if kind == "npcs":
            context["current_entry"]["known_locations"] = entry.get("known_locations", [])
            context["current_entry"]["known_objects"] = entry.get("known_objects", [])
        if kind == "items":
            context["current_entry"]["collectible"] = entry.get("collectible", False)
        return context

    def _build_action_suggestion_context(self, action, field_name):
        return {
            "selection": {
                "kind": "action",
                "field_name": field_name,
                "entry_name": action.get("name"),
            },
            "current_entry": {
                "name": action.get("name"),
                "id": action.get("id"),
                "existing_values": [action.get(field_name)] if action.get(field_name) else [],
                "required_tag": action.get("required_tag"),
                "required_object": action.get("required_object"),
                "collects_object": action.get("collects_object"),
                "outcomes": action.get("outcomes", []),
            },
            "whole_case": self._load_full_case_context(),
        }

    def _build_outcome_suggestion_context(self, outcome, field_name):
        return {
            "selection": {
                "kind": "outcome",
                "field_name": field_name,
                "entry_name": outcome.get("id") or outcome.get("desc"),
            },
            "current_entry": {
                "id": outcome.get("id"),
                "description": outcome.get("desc"),
                "tags": outcome.get("tags", []),
                "existing_values": outcome.get(field_name, []) if isinstance(outcome.get(field_name), list) else [outcome.get(field_name)] if outcome.get(field_name) else [],
                "effects": {
                    key: outcome.get(key)
                    for key in (
                        "health_change",
                        "coin_change",
                        "success_prob",
                        "spawn",
                        "move_to",
                        "move_to_tags",
                        "reveal_npc",
                        "reveal_object",
                        "reveal_location",
                    )
                    if key in outcome
                },
            },
            "whole_case": self._load_full_case_context(),
        }

    def _build_interaction_point_suggestion_context(self, point, field_name):
        return {
            "selection": {
                "kind": "interaction_point",
                "field_name": field_name,
                "entry_name": point.get("label") or point.get("id"),
            },
            "current_entry": {
                "id": point.get("id"),
                "label": point.get("label"),
                "prompt": point.get("prompt"),
                "existing_values": [point.get(field_name)] if point.get(field_name) else [],
                "linked_action": point.get("linked_action"),
                "linked_outcome": point.get("linked_outcome"),
                "linked_location": point.get("linked_location"),
                "linked_npc": point.get("linked_npc"),
                "linked_item": point.get("linked_item"),
                "notes": point.get("notes"),
            },
            "whole_case": self._load_full_case_context(),
        }

    def _load_full_case_context(self):
        files = {}
        for info in self.FILE_OPTIONS.values():
            files[info["label"]] = self._load_yaml(os.path.join(self.project_root, info["path"]))
        return files

    def _generate_field_suggestions(self, label, context):
        suggester = self._get_suggester()
        prompt_label = self._suggestion_label(label)
        print("Generating suggestions with local LLM...")
        suggestions = suggester.suggest_list(prompt_label, context)
        if not suggestions:
            print("No suggestions available.")
            if suggester.last_error():
                print(f"Reason: {suggester.last_error()}")
            return None

        print("\nSuggested values:")
        for index, suggestion in enumerate(suggestions, start=1):
            print(f"  {index}. {suggestion}")
        return suggestions

    def _suggestion_label(self, label):
        lowered = label.lower()
        if "outcome" in lowered and "tag" in lowered:
            return "Outcome tags"
        if "tag" in lowered:
            return "Tags"
        return label

    def _choose_suggestion(self, suggestions):
        print("Choose a suggestion number, or press Enter to cancel.")
        choice = input("Selection: ").strip()
        if not choice:
            return None
        if choice.isdigit():
            index = int(choice) - 1
            if 0 <= index < len(suggestions):
                return suggestions[index]
        print("No suggestion selected.")
        return None

    def _apply_suggestions_to_list(self, values, suggestions):
        print("Type numbers separated by commas to add suggestions, or press Enter to cancel.")
        choice = input("Selection: ").strip()
        if not choice:
            return False

        changed = False
        for token in choice.split(","):
            token = token.strip()
            if not token.isdigit():
                continue
            index = int(token) - 1
            if 0 <= index < len(suggestions):
                suggestion = suggestions[index]
                if suggestion not in values:
                    values.append(suggestion)
                    changed = True
        if changed:
            print("Selected suggestions added.")
        else:
            print("No suggestions were added.")
        return changed

    def _get_suggester(self):
        if self._suggester is None:
            self._suggester = LLMSuggester()
        return self._suggester

    def _edit_actions(self, entry):
        entry.setdefault("actions", [])
        changed = False

        while True:
            actions = entry.get("actions", [])
            print("\nExisting actions:")
            for index, action in enumerate(actions, start=1):
                print(f"  {index}. {self._describe_action(action)}")
            if not actions:
                print("  (no actions yet)")

            print("Options: select, add, suggest, delete, back")
            choice = input("Selection: ").strip().lower()
            if choice in {"back", "b", ""}:
                return changed
            if choice == "add":
                new_action = {
                    "id": self._slugify(input("Action id (Enter for auto): ").strip() or input("Action name: ").strip()),
                    "name": input("Action display name: ").strip(),
                    "outcomes": [],
                }
                if not new_action["name"]:
                    new_action["name"] = new_action["id"].replace("_", " ").title()
                entry["actions"].append(new_action)
                print("Action added. Opening editor for it now.")
                self._edit_single_action(new_action)
                changed = True
                continue
            if choice == "suggest":
                suggestions = self._generate_field_suggestions(
                    "Action names",
                    {
                        "selection": {"kind": "action", "field_name": "name"},
                        "current_entry": entry,
                        "existing_values": [action.get("name") for action in actions],
                        "whole_case": self._load_full_case_context(),
                    },
                )
                if not suggestions:
                    continue
                for suggestion in suggestions:
                    if self._ask_yes_no(f"Add suggested action '{suggestion}'?", False):
                        new_action = {
                            "id": self._slugify(suggestion),
                            "name": suggestion,
                            "outcomes": [],
                        }
                        entry["actions"].append(new_action)
                        self._edit_single_action(new_action)
                        changed = True
                continue
            if choice == "delete":
                index = self._choose_index(actions, "Action number to delete")
                if index is None:
                    continue
                print(yaml.safe_dump(actions[index], sort_keys=False, allow_unicode=False).strip())
                confirm = input("Type 'delete' to confirm: ").strip().lower()
                if confirm == "delete":
                    removed = actions.pop(index)
                    print(f"Deleted action: {removed.get('name', 'Unnamed action')}")
                    changed = True
                continue
            if choice == "select":
                index = self._choose_index(actions, "Action number to edit")
                if index is None:
                    continue
                if self._edit_single_action(actions[index]):
                    changed = True
                continue
            print("Use select, add, suggest, delete, or back.")

    def _edit_single_action(self, action):
        changed = False
        while True:
            print(f"\nAction: {action.get('name', 'Unnamed action')}")
            print("  1. Rename action")
            print("  2. Edit action id")
            print("  3. Edit requirements and rewards")
            print("  4. Edit outcomes")
            print("  5. Show full action")
            print(f"  6. Repeat rule (currently: {action.get('repeat') or 'default for its source'})")
            print(f"  7. Exclusive group (currently: {action.get('exclusive_group') or 'none'})")
            print("  b. back")
            choice = input("Selection: ").strip().lower()

            if choice in {"b", "back", ""}:
                return changed
            if choice == "1":
                new_name = input("New action name (or type 'suggest'): ").strip()
                if new_name.lower() == "suggest":
                    suggestions = self._generate_field_suggestions(
                        "Action names",
                        self._build_action_suggestion_context(action, "name"),
                    )
                    if suggestions:
                        selected = self._choose_suggestion(suggestions)
                        new_name = selected or ""
                if new_name:
                    action["name"] = new_name
                    changed = True
                continue
            if choice == "2":
                new_id = input("New action id: ").strip()
                if new_id:
                    action["id"] = self._slugify(new_id)
                    changed = True
                continue
            if choice == "3":
                if self._edit_action_requirements(action):
                    changed = True
                continue
            if choice == "4":
                if self._edit_outcomes(action):
                    changed = True
                continue
            if choice == "5":
                print(yaml.safe_dump(action, sort_keys=False, allow_unicode=False).strip())
                continue
            if choice == "6":
                print("How often is this option offered?")
                print("  once       only once per game")
                print("  per_visit  again on each visit to the location")
                print("  always     every turn")
                print("  default    use action_rules.default_repeat for its source")
                value = input("Repeat rule: ").strip().lower()
                if value == "default":
                    changed = action.pop("repeat", None) is not None or changed
                elif value in {"once", "per_visit", "always"}:
                    changed = action.get("repeat") != value or changed
                    action["repeat"] = value
                elif value:
                    print("Please enter once, per_visit, always, or default.")
                continue
            if choice == "7":
                print("Actions with the same exclusive group are one choice: once one succeeds, the others disappear.")
                value = input("Exclusive group name (Enter to remove): ").strip()
                if value:
                    changed = action.get("exclusive_group") != self._slugify(value) or changed
                    action["exclusive_group"] = self._slugify(value)
                elif "exclusive_group" in action:
                    action.pop("exclusive_group")
                    changed = True
                continue
            print("Please choose 1, 2, 3, 4, 5, 6, 7, or b.")

    def _edit_action_requirements(self, action):
        changed = False
        while True:
            print("\nCurrent action requirements:")
            print(f"  required_tag: {action.get('required_tag')}")
            print(f"  required_object: {action.get('required_object')}")
            print(f"  required_coins: {action.get('required_coins', 0)}")
            print(f"  collects_object: {action.get('collects_object')}")
            print(f"  consumes_object: {action.get('consumes_object', False)}")
            print("Options: required_tag, required_object, required_coins, collects_object, consumes_object, back")

            choice = input("Selection: ").strip().lower()
            if choice in {"back", "b", ""}:
                return changed
            if choice in {"required_tag", "required_object", "collects_object"}:
                value = input(f"New value for {choice} (Enter to clear): ").strip()
                if value:
                    action[choice] = value
                else:
                    action.pop(choice, None)
                changed = True
                continue
            if choice == "required_coins":
                value = input("New coin requirement (Enter for 0): ").strip()
                action["required_coins"] = int(value) if value else 0
                changed = True
                continue
            if choice == "consumes_object":
                action["consumes_object"] = self._ask_yes_no("Should this action consume the used object?", action.get("consumes_object", False))
                changed = True
                continue
            print("Unknown option.")

    def _edit_outcomes(self, action):
        action.setdefault("outcomes", [])
        changed = False
        while True:
            print("\nExisting outcomes:")
            for index, outcome in enumerate(action["outcomes"], start=1):
                print(f"  {index}. {self._describe_outcome(outcome)}")
            if not action["outcomes"]:
                print("  (no outcomes yet)")

            print("Options: select, add, suggest, delete, back")
            choice = input("Selection: ").strip().lower()
            if choice in {"back", "b", ""}:
                return changed
            if choice == "add":
                outcome_desc = input("Outcome description: ").strip()
                new_outcome = {
                    "id": self._slugify(input("Outcome id (Enter for auto): ").strip() or outcome_desc),
                    "desc": outcome_desc,
                    "tags": [],
                }
                action["outcomes"].append(new_outcome)
                self._edit_single_outcome(new_outcome)
                changed = True
                continue
            if choice == "suggest":
                suggestions = self._generate_field_suggestions(
                    "Outcome descriptions",
                    {
                        "selection": {"kind": "outcome", "field_name": "desc"},
                        "current_entry": action,
                        "existing_values": [outcome.get("desc") for outcome in action["outcomes"]],
                        "whole_case": self._load_full_case_context(),
                    },
                )
                if not suggestions:
                    continue
                for suggestion in suggestions:
                    if self._ask_yes_no(f"Add suggested outcome '{suggestion}'?", False):
                        new_outcome = {
                            "id": self._slugify(suggestion),
                            "desc": suggestion,
                            "tags": [],
                        }
                        action["outcomes"].append(new_outcome)
                        self._edit_single_outcome(new_outcome)
                        changed = True
                continue
            if choice == "delete":
                index = self._choose_index(action["outcomes"], "Outcome number to delete")
                if index is None:
                    continue
                print(yaml.safe_dump(action["outcomes"][index], sort_keys=False, allow_unicode=False).strip())
                confirm = input("Type 'delete' to confirm: ").strip().lower()
                if confirm == "delete":
                    action["outcomes"].pop(index)
                    changed = True
                continue
            if choice == "select":
                index = self._choose_index(action["outcomes"], "Outcome number to edit")
                if index is None:
                    continue
                if self._edit_single_outcome(action["outcomes"][index]):
                    changed = True
                continue
            print("Use select, add, suggest, delete, or back.")

    def _edit_single_outcome(self, outcome):
        changed = False
        while True:
            print(f"\nOutcome: {outcome.get('desc', 'No description')}")
            print("  1. Edit description")
            print("  2. Edit outcome id")
            print("  3. Edit tags")
            print("  4. Edit effect/link fields")
            print("  5. Show full outcome")
            print("  b. back")
            choice = input("Selection: ").strip().lower()

            if choice in {"b", "back", ""}:
                return changed
            if choice == "1":
                value = input("New outcome description (or type 'suggest'): ").strip()
                if value.lower() == "suggest":
                    suggestions = self._generate_field_suggestions(
                        "Outcome descriptions",
                        self._build_outcome_suggestion_context(outcome, "desc"),
                    )
                    if suggestions:
                        selected = self._choose_suggestion(suggestions)
                        value = selected or ""
                if value:
                    outcome["desc"] = value
                    changed = True
                continue
            if choice == "2":
                new_id = input("New outcome id: ").strip()
                if new_id:
                    outcome["id"] = self._slugify(new_id)
                    changed = True
                continue
            if choice == "3":
                if self._edit_outcome_tags(outcome):
                    changed = True
                continue
            if choice == "4":
                if self._edit_outcome_effects(outcome):
                    changed = True
                continue
            if choice == "5":
                print(yaml.safe_dump(outcome, sort_keys=False, allow_unicode=False).strip())
                continue
            print("Please choose 1, 2, 3, 4, 5, or b.")

    def _edit_outcome_tags(self, outcome):
        outcome.setdefault("tags", [])
        changed = False

        while True:
            print("\nCurrent tags:")
            for index, tag in enumerate(outcome["tags"], start=1):
                print(f"  {index}. {tag} -> {self._tag_hint(tag)}")
            if not outcome["tags"]:
                print("  (no tags yet)")

            print("Options: add, suggest, remove, back")
            choice = input("Selection: ").strip().lower()
            if choice in {"back", "b", ""}:
                return changed
            if choice == "remove":
                index = self._choose_index(outcome["tags"], "Tag number to remove")
                if index is None:
                    continue
                removed = outcome["tags"].pop(index)
                print(f"Removed tag: {removed}")
                changed = True
                continue
            if choice == "add":
                tag = self._prompt_for_tag()
                if not tag:
                    continue
                if tag not in outcome["tags"]:
                    outcome["tags"].append(tag)
                    changed = True
                self._apply_tag_requirements(outcome, tag)
                changed = True
                continue
            if choice == "suggest":
                suggestions = self._generate_field_suggestions(
                    "Outcome tags",
                    self._build_outcome_suggestion_context(outcome, "tags"),
                )
                if not suggestions:
                    continue
                for tag in suggestions:
                    if tag not in outcome["tags"] and self._ask_yes_no(f"Add suggested tag '{tag}'?", False):
                        outcome["tags"].append(tag)
                        self._apply_tag_requirements(outcome, tag)
                        changed = True
                continue
            print("Use add, suggest, remove, or back.")

    def _edit_outcome_effects(self, outcome):
        fields = [
            ("health_change", "integer"),
            ("coin_change", "integer"),
            ("success_prob", "float"),
            ("spawn", "bool"),
            ("move_to", "text"),
            ("send_to", "text"),  # one-way: the player cannot walk back
            ("move_to_tags", "yaml"),
            ("reveal_npc", "text"),
            ("reveal_object", "text"),
            ("reveal_location", "text"),
            ("linked_location", "text"),
            ("linked_location_mode", "text"),
            ("linked_npc", "text"),
            ("linked_item", "text"),
            ("lead_to_known", "bool"),
        ]
        changed = False

        while True:
            print("\nCurrent effect fields:")
            for index, (field, _) in enumerate(fields, start=1):
                print(f"  {index}. {field}: {outcome.get(field)}")
            print("  b. back")
            choice = input("Selection: ").strip().lower()
            if choice in {"b", "back", ""}:
                return changed
            if choice.isdigit() and 1 <= int(choice) <= len(fields):
                field, field_type = fields[int(choice) - 1]
                if self._set_typed_field(outcome, field, field_type):
                    changed = True
                continue
            print("Choose one of the listed fields.")

    def _set_typed_field(self, container, field, field_type):
        raw = input(f"New value for {field} (Enter to clear): ").strip()
        if raw == "":
            container.pop(field, None)
            return True
        if field_type == "integer":
            container[field] = int(raw)
            return True
        if field_type == "float":
            container[field] = float(raw)
            return True
        if field_type == "bool":
            container[field] = raw.lower() in {"1", "true", "yes", "y"}
            return True
        if field_type == "yaml":
            container[field] = yaml.safe_load(raw)
            return True
        container[field] = raw
        return True

    def _prompt_for_tag(self):
        print("\nAvailable common tags:")
        for index, tag in enumerate(self.COMMON_OUTCOME_TAGS, start=1):
            print(f"  {index}. {tag} -> {self._tag_hint(tag)}")
        print("  c. custom tag")
        choice = input("Select a tag: ").strip().lower()
        if choice == "c":
            return input("Custom tag name: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(self.COMMON_OUTCOME_TAGS):
            return self.COMMON_OUTCOME_TAGS[int(choice) - 1]
        print("No tag selected.")
        return None

    def _apply_tag_requirements(self, outcome, tag):
        print(f"Tag '{tag}' added. {self._tag_hint(tag)}")
        if tag == "threat":
            default = outcome.get("health_change", -1)
            value = input(f"Health change for threat [{default}]: ").strip()
            outcome["health_change"] = int(value) if value else default
            return
        if tag == "spawn":
            outcome["spawn"] = True
            if self._ask_yes_no("Reveal a specific location as part of this spawn?", bool(outcome.get("reveal_location"))):
                reveal_location = input("Location name to reveal: ").strip()
                if reveal_location:
                    outcome["reveal_location"] = reveal_location
            return
        if tag == "combat" and "health_change" not in outcome:
            if self._ask_yes_no("Combat often affects health. Add a health change now?", True):
                value = input("Health change (negative for damage, positive for recovery): ").strip()
            if value:
                    outcome["health_change"] = int(value)
            return
        if tag == "success" and "health_change" not in outcome and "coin_change" not in outcome:
            print("Tip: successful outcomes often also reward health, coins, movement, or discovery.")
            return
        if tag == "setup_clue":
            print("No extra field is required. The engine will convert this into a setup_* clue at runtime.")
            return
        print("No extra required fields for this tag.")

    def _edit_interaction_points(self, entry):
        entry.setdefault("interaction_points", [])
        changed = False

        while True:
            print("\nExisting interaction points:")
            for index, point in enumerate(entry["interaction_points"], start=1):
                print(f"  {index}. {self._describe_interaction_point(point)}")
            if not entry["interaction_points"]:
                print("  (no interaction points yet)")

            print("Options: select, add, delete, back")
            choice = input("Selection: ").strip().lower()
            if choice in {"back", "b", ""}:
                return changed
            if choice == "add":
                label = input("Interaction point label: ").strip()
                point = {
                    "id": self._slugify(input("Interaction point id (Enter for auto): ").strip() or label),
                    "label": label,
                    "prompt": input("Prompt shown to the user: ").strip(),
                }
                entry["interaction_points"].append(point)
                if point.get("prompt") and point["prompt"] not in entry.setdefault("interact_prompts", []):
                    entry["interact_prompts"].append(point["prompt"])
                self._edit_single_interaction_point(point)
                changed = True
                continue
            if choice == "delete":
                index = self._choose_index(entry["interaction_points"], "Interaction point number to delete")
                if index is None:
                    continue
                print(yaml.safe_dump(entry["interaction_points"][index], sort_keys=False, allow_unicode=False).strip())
                if input("Type 'delete' to confirm: ").strip().lower() == "delete":
                    entry["interaction_points"].pop(index)
                    changed = True
                continue
            if choice == "select":
                index = self._choose_index(entry["interaction_points"], "Interaction point number to edit")
                if index is None:
                    continue
                if self._edit_single_interaction_point(entry["interaction_points"][index]):
                    changed = True
                continue
            print("Use select, add, delete, or back.")

    def _edit_single_interaction_point(self, point):
        changed = False
        while True:
            print(f"\nInteraction point: {point.get('label') or point.get('id')}")
            for index, (field, _) in enumerate(self.INTERACTION_POINT_FIELDS, start=1):
                print(f"  {index}. {field}: {point.get(field)}")
            print("  s. suggest a text field")
            print("  b. back")
            choice = input("Selection: ").strip().lower()
            if choice in {"back", "b", ""}:
                return changed
            if choice == "s":
                text_fields = [
                    ("label", "Interaction point labels"),
                    ("prompt", "Interaction point prompts"),
                    ("notes", "Interaction point notes"),
                ]
                for index, (field, label) in enumerate(text_fields, start=1):
                    print(f"  {index}. {field}: {point.get(field)}")
                index = self._choose_index(text_fields, "Field number to suggest")
                if index is None:
                    continue
                field, label = text_fields[index]
                suggestions = self._generate_field_suggestions(
                    label,
                    self._build_interaction_point_suggestion_context(point, field),
                )
                if suggestions:
                    selected = self._choose_suggestion(suggestions)
                    if selected:
                        point[field] = selected
                        changed = True
                continue
            if choice.isdigit() and 1 <= int(choice) <= len(self.INTERACTION_POINT_FIELDS):
                field, field_type = self.INTERACTION_POINT_FIELDS[int(choice) - 1]
                if self._set_typed_field(point, field, field_type):
                    changed = True
                continue
            print("Choose one of the listed fields.")

    def _delete_entry(self, data, kind):
        entry_name = self._choose_entry_name(data, kind, allow_cancel=True)
        if entry_name is None:
            return False
        entry = self._get_entry(data, kind, entry_name)
        self._print_entry_details(kind, entry_name, entry)
        confirm = input("Type 'delete' to remove this whole entry: ").strip().lower()
        if confirm != "delete":
            return False

        if kind == "locations":
            del self._get_collection(data, kind)[entry_name]
        else:
            collection = self._get_collection(data, kind)
            for index, item in enumerate(collection):
                if item.get("name") == entry_name:
                    collection.pop(index)
                    break
        print(f"Deleted entry: {entry_name}")
        return True

    def _delete_action(self, data, kind):
        entry_name = self._choose_entry_name(data, kind, allow_cancel=True)
        if entry_name is None:
            return False
        entry = self._get_entry(data, kind, entry_name)
        actions = entry.get("actions", [])
        for index, action in enumerate(actions, start=1):
            print(f"  {index}. {self._describe_action(action)}")
        action_index = self._choose_index(actions, "Action number to delete")
        if action_index is None:
            return False
        confirm = input("Type 'delete' to remove this action: ").strip().lower()
        if confirm != "delete":
            return False
        removed = actions.pop(action_index)
        print(f"Deleted action: {removed.get('name', 'Unnamed action')}")
        return True

    def _delete_outcome(self, data, kind):
        entry_name = self._choose_entry_name(data, kind, allow_cancel=True)
        if entry_name is None:
            return False
        entry = self._get_entry(data, kind, entry_name)
        actions = entry.get("actions", [])
        for index, action in enumerate(actions, start=1):
            print(f"  {index}. {self._describe_action(action)}")
        action_index = self._choose_index(actions, "Action number")
        if action_index is None:
            return False

        outcomes = actions[action_index].get("outcomes", [])
        for index, outcome in enumerate(outcomes, start=1):
            print(f"  {index}. {self._describe_outcome(outcome)}")
        outcome_index = self._choose_index(outcomes, "Outcome number to delete")
        if outcome_index is None:
            return False
        confirm = input("Type 'delete' to remove this outcome: ").strip().lower()
        if confirm != "delete":
            return False
        outcomes.pop(outcome_index)
        print("Outcome deleted.")
        return True

    def _choose_entry_name(self, data, kind, allow_cancel=False):
        collection = self._get_collection(data, kind)
        if kind == "locations":
            names = list(collection.keys())
        else:
            names = [entry.get("name", "Unnamed") for entry in collection]

        if not names:
            print("No entries available.")
            return None

        print("\nChoose an existing configuration:")
        for index, name in enumerate(names, start=1):
            print(f"  {index}. {name}")
        if allow_cancel:
            print("  b. back")

        while True:
            choice = input("Selection: ").strip().lower()
            if allow_cancel and choice in {"b", "back", ""}:
                return None
            if choice.isdigit() and 1 <= int(choice) <= len(names):
                return names[int(choice) - 1]
            print("Please choose one of the listed entries.")

    def _get_collection(self, data, kind):
        if kind == "locations":
            return data.setdefault("world_definition", {}).setdefault("locations", {})
        if kind == "npcs":
            return data.setdefault("world_definition", {}).setdefault("characters", [])
        if kind == "items":
            return data.setdefault("world_definition", {}).setdefault("objects", [])
        return data

    def _get_entry(self, data, kind, entry_name):
        collection = self._get_collection(data, kind)
        if kind == "locations":
            return collection[entry_name]
        for entry in collection:
            if entry.get("name") == entry_name:
                return entry
        raise KeyError(f"Entry not found: {entry_name}")

    def _choose_index(self, items, prompt):
        if not items:
            print("Nothing to choose from.")
            return None
        value = input(f"{prompt}: ").strip()
        if not value.isdigit():
            print("Please enter a number.")
            return None
        index = int(value) - 1
        if 0 <= index < len(items):
            return index
        print("That number is out of range.")
        return None

    def _unique_label(self, entry):
        return "unique" if entry.get("unique", False) else "generic"

    def _encounter_label(self, entry):
        event = entry.get("encounter_event")
        if not event:
            return "none"
        return f"{event} ({'every visit' if entry.get('encounter_repeat') else 'first meeting only'})"

    def _edit_encounter(self, entry):
        """Choose the event a hostile NPC starts by itself when it is in the scene."""
        events_file = os.path.join(self.project_root, "config", "events.yaml")
        events = list(
            (self._load_yaml(events_file) if os.path.exists(events_file) else {})
            .get("world_definition", {}).get("events", {}) or {}
        )
        print("\nEncounter event (starts by itself when this NPC is in the scene):")
        print("  0. none")
        for index, name in enumerate(events, start=1):
            print(f"  {index}. {name}")
        choice = input("Selection (b to cancel): ").strip().lower()
        if choice in {"b", "back", ""}:
            return False
        if choice == "0":
            changed = "encounter_event" in entry or "encounter_repeat" in entry
            entry.pop("encounter_event", None)
            entry.pop("encounter_repeat", None)
            return changed
        if not choice.isdigit() or not 1 <= int(choice) <= len(events):
            print("Please choose one of the listed events.")
            return False
        before = (entry.get("encounter_event"), entry.get("encounter_repeat"))
        entry["encounter_event"] = events[int(choice) - 1]
        entry["encounter_repeat"] = self._ask_yes_no(
            "Repeat on every visit? (no: only the first encounter in a game)",
            default=bool(entry.get("encounter_repeat", False)),
        )
        return before != (entry["encounter_event"], entry["encounter_repeat"])

    def _ask_yes_no(self, prompt, default=False):
        suffix = " [Y/n]: " if default else " [y/N]: "
        value = input(prompt + suffix).strip().lower()
        if value == "":
            return default
        return value in {"y", "yes"}

    def _slugify(self, value):
        value = value.strip().lower()
        value = re.sub(r"[^a-z0-9]+", "_", value)
        return value.strip("_") or "id"

    def _read_value(self, label):
        print(f"{label}: enter YAML/JSON. Finish with an empty line.")
        lines = []
        while True:
            line = input()
            if line == "":
                break
            lines.append(line)

        raw_text = "\n".join(lines).strip()
        if not raw_text:
            return ""

        try:
            return yaml.safe_load(raw_text)
        except yaml.YAMLError:
            return raw_text

    def _load_yaml(self, file_path):
        if not os.path.exists(file_path):
            return {}
        with open(file_path, "r") as handle:
            data = yaml.safe_load(handle)
        return data if data is not None else {}

    def _save_yaml(self, file_path, data):
        with open(file_path, "w") as handle:
            yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=False)

    def _print_preview(self, data):
        preview = yaml.safe_dump(data, sort_keys=False, allow_unicode=False)
        lines = preview.splitlines()
        max_lines = 40
        for line in lines[:max_lines]:
            print(line)
        if len(lines) > max_lines:
            print("... (preview truncated)")

    def _tokenize_path(self, path):
        tokens = []
        for part in path.split("."):
            if not part:
                raise ValueError("Empty path segment found.")

            match = re.match(r"^[^\[\]]+", part)
            if match:
                tokens.append(match.group(0))
            elif part.startswith("["):
                pass
            else:
                raise ValueError(f"Invalid path segment: {part}")

            for index_token in re.findall(r"\[(\d+)\]", part):
                tokens.append(int(index_token))

            cleaned = re.sub(r"^[^\[\]]+", "", part)
            if cleaned and not re.fullmatch(r"(\[\d+\])+", cleaned):
                raise ValueError(f"Invalid list syntax in segment: {part}")

        return tokens

    def _get_path(self, data, path):
        try:
            current = data
            for token in self._tokenize_path(path):
                current = current[token]
            return True, current
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            return False, f"Path not found: {exc}"

    def _set_path(self, data, path, value):
        tokens = self._tokenize_path(path)
        if not tokens:
            raise ValueError("Path cannot be empty.")

        current = data
        for i, token in enumerate(tokens[:-1]):
            next_token = tokens[i + 1]

            if isinstance(token, int):
                if not isinstance(current, list):
                    raise TypeError(f"Expected list before index [{token}]")
                while len(current) <= token:
                    current.append({} if not isinstance(next_token, int) else [])
                current = current[token]
                continue

            if not isinstance(current, dict):
                raise TypeError(f"Expected object before key '{token}'")

            if token not in current or current[token] is None:
                current[token] = [] if isinstance(next_token, int) else {}
            current = current[token]

        last = tokens[-1]
        if isinstance(last, int):
            if not isinstance(current, list):
                raise TypeError(f"Expected list before index [{last}]")
            while len(current) <= last:
                current.append(None)
            current[last] = value
        else:
            if not isinstance(current, dict):
                raise TypeError(f"Expected object before key '{last}'")
            current[last] = value

    def _append_path(self, data, path, value):
        ok, current = self._get_path(data, path)
        if not ok:
            self._set_path(data, path, [])
            ok, current = self._get_path(data, path)

        if not isinstance(current, list):
            raise TypeError("Append requires a list at the target path.")
        current.append(value)

    def _delete_path(self, data, path):
        tokens = self._tokenize_path(path)
        if not tokens:
            raise ValueError("Path cannot be empty.")

        current = data
        for token in tokens[:-1]:
            current = current[token]

        last = tokens[-1]
        if isinstance(last, int):
            if not isinstance(current, list):
                raise TypeError(f"Expected list before index [{last}]")
            del current[last]
        else:
            if not isinstance(current, dict):
                raise TypeError(f"Expected object before key '{last}'")
            del current[last]
