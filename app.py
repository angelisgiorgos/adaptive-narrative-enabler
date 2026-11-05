"""Gradio UI of the Adaptive Narrative Studio.

This file only builds the interface. Every operation (validation, storage,
authoring, evolution, play) lives in the backend (backend/, core_engine/);
the REST API (api.py) calls the same backend.
"""

from __future__ import annotations

import functools
import os

import gradio as gr
import pandas as pd

import ui_authoring
from backend import content, export, sessions, store
from backend.content import (
    ACTION_REFERENCE, COMMON_TAGS, ENTITY_FILES, PLAYER_STAT_CHOICES, _conditions_text, _entity_name_values, _event_choices,
    load_entity, load_entity_actions_yaml, load_messages_yaml, load_missions_yaml, load_stats_yaml,
    load_variables_yaml, player_profiles, stored_values_yaml,
)
from backend.errors import AuthoringError
from backend.store import (
    APP_SETTINGS, RULE_CHOICES, _weight_entries, load_hyperparameters_yaml, load_logic_section, load_rules,
    load_settings, logic_sections,
)
from core_engine import Mission
from core_engine.stats import stats_config
from utils.config_loader import config


def ui(function):
    """Show backend errors as UI error messages."""
    @functools.wraps(function)
    def handler(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (AuthoringError, ValueError) as exc:
            raise gr.Error(str(exc)) from exc
    return handler


# Backend operations used as UI event handlers.
upload_config = ui(store.upload_config)
use_builtin_config = ui(store.use_builtin_config)
save_settings = ui(store.save_settings)
save_weights = ui(store.save_weights)
save_all_hyperparameters = ui(store.save_all_hyperparameters)
load_rules_ui = ui(store.load_rules_ui)
save_rules_ui = ui(store.save_rules_ui)
save_logic_section = ui(store.save_logic_section)
save_entity_actions_ui = ui(content.save_entity_actions_ui)
save_missions_yaml = ui(content.save_missions_yaml)
save_messages_yaml = ui(content.save_messages_yaml)
save_variables_yaml = ui(content.save_variables_yaml)
save_stored_values_yaml = ui(content.save_stored_values_yaml)
reset_stored_values_ui = ui(content.reset_stored_values_ui)
save_stats_yaml = ui(content.save_stats_yaml)
stats_points_text = ui(content.stats_points_text)
check_points_text = content.check_points_text
load_logic_section = ui(load_logic_section)
stored_values_yaml = ui(stored_values_yaml)


def logic_description_text(section: str) -> str:
    return store.LOGIC_SECTIONS.get(section, "")


CSS = """
.hero {padding: 1.25rem; border-radius: 18px; background: linear-gradient(120deg,#17233f,#4a244f); color:#fff}
.status {border-left: 4px solid #8b5cf6; padding-left: 1rem}
"""


def _option_fields(kind: str, unique, encounter_event, encounter_repeat, enabled_if, title=None, stat_bonus=None):
    """Editor updates for the per-kind options: NPC flags, enabled_if for
    locations, NPCs and items, the title of events, and item stat bonuses."""
    npc = kind == "NPCs"
    return (
        gr.Checkbox(value=bool(unique), visible=npc),
        gr.Dropdown(choices=_event_choices(), value=encounter_event or "", visible=npc),
        gr.Checkbox(value=bool(encounter_repeat), visible=npc),
        gr.Textbox(value=_conditions_text(enabled_if), visible=kind != "Events"),
        gr.Textbox(value=title or "", visible=kind == "Events"),
        gr.Textbox(value=_conditions_text(stat_bonus), visible=kind == "Items"),
    )


def entity_names(kind: str):
    names, value = _entity_name_values(kind)
    values = load_entity(kind, value) if value else ("", [], "", "", False, None, False, None, None, None)
    return gr.Dropdown(choices=names, value=value), *values[:4], *_option_fields(kind, *values[4:])


def _load_entity_ui(kind: str, name: str):
    values = load_entity(kind, name)
    return (*values[:4], *_option_fields(kind, *values[4:]))


def new_entity():
    return gr.Dropdown(value=None), "", [], "", "", False, "", False, "", "", ""


def save_entity_ui(kind, selected_name, name, tags, descriptions, goals, *options):
    choices, saved, message = ui(content.save_entity)(kind, selected_name, name, tags, descriptions, goals, *options)
    return gr.Dropdown(choices=choices, value=saved), message


def delete_entity_ui(kind: str, selected_name: str):
    choices, next_name, message = ui(content.delete_entity)(kind, selected_name)
    values = load_entity(kind, next_name) if next_name else ("", [], "", "", False, None, False, None, None, None)
    return gr.Dropdown(choices=choices, value=next_name), *values[:4], *_option_fields(kind, *values[4:]), message


# ---- evolution and play ------------------------------------------------------------

def _learning_outputs(learning):
    """Learning curve rows and the learned-traits table of a learning summary."""
    if not learning:
        return pd.DataFrame(columns=["generation", "score", "series"]), pd.DataFrame(
            columns=["trait", "meaning", "evolved", "population", "tendency"])
    rows = []
    for stats in learning.get("history", []):
        rows += [
            {"generation": stats["generation"], "score": stats["validation"], "series": "benchmark (champion)"},
            {"generation": stats["generation"], "score": stats["best"], "series": "best this generation"},
            {"generation": stats["generation"], "score": stats["mean"], "series": "population average"},
        ]
    traits = [
        {"trait": t["trait"], "meaning": t["meaning"], "evolved": t["value"],
         "population": t["population_mean"], "tendency": t["tendency"]}
        for t in learning.get("learned_traits", [])
    ]
    return pd.DataFrame(rows), pd.DataFrame(traits)


def _choices(actions):
    return gr.Radio(choices=actions, value=actions[0] if actions else None, interactive=bool(actions))


def learning_memory_text() -> str:
    memory = sessions.learning_memory()
    if not memory["size"]:
        return "The AI has not learned anything yet: the next run starts from random storytellers."
    best = max(entry["score"] for entry in memory["hall_of_fame"])
    return (f"The AI remembers **{memory['size']}** storyteller(s) from earlier runs (best score {best:.1f}). "
            "New runs continue from them when *Continue learning* is on.")


def forget_learning_ui():
    sessions.forget_learning()
    return learning_memory_text()


def run_simulation_ui(candidate_count, generations, simulation_steps, deterministic, profile, stats_choice,
                      stats_points, episodes, warm_start, progress=gr.Progress()):
    def report(generation, total, stats):
        progress((generation + 1, total), desc=f"Generation {generation + 1}/{total}: benchmark {stats.validation:.0f}")

    game = ui(sessions.run_simulation)(
        candidate_count, generations, simulation_steps, deterministic, profile=profile,
        stats_choice=stats_choice, stats_points=stats_points, episodes=episodes, warm_start=warm_start,
        progress=report,
    )
    curve, traits = _learning_outputs(game["learning"])
    generations = [str(stats["generation"]) for stats in game["learning"]["history"]]
    return (game["token"], game["report"], game["scene"], _choices(game["actions"]), game["last_turn"],
            game["config_path"], game["engine_path"], curve, traits, learning_memory_text(),
            game["run_id"], gr.Dropdown(choices=generations, value=generations[-1] if generations else None))


def load_best_engine_ui(uploaded_file, model_parameters_file, profile, stats_choice, stats_points):
    game = ui(sessions.load_best_engine)(uploaded_file, model_parameters_file, profile, stats_choice, stats_points)
    curve, traits = _learning_outputs(game["learning"])
    return (game["token"], game["report"], game["scene"], _choices(game["actions"]), game["last_turn"],
            game["config_path"], game["engine_path"], curve, traits)


EXPORT_SOURCES = {
    "This run's best": "run",
    "A generation of this run": "run",
    "Best remembered storyteller": "memory",
    "This game's storyteller": "game",
}


def export_engine_ui(source_label, run_id, generation, token, include_memory, name):
    source = EXPORT_SOURCES.get(source_label, "run")
    result = ui(export.export_engine)(
        source=source, run_id=run_id,
        generation=generation if source_label == "A generation of this run" else None,
        token=token or None, include_memory=bool(include_memory), name=name or None,
    )
    manifest = result["manifest"]
    score = f"{manifest['score']:.1f}" if isinstance(manifest["score"], (int, float)) else "not benchmarked"
    status = (f"Exported **{manifest['name']}**: {manifest['source']} (benchmark score {score}). "
              f"World: {manifest['world']['locations']} locations, {manifest['world']['npcs']} NPCs, "
              f"{manifest['world']['missions']} missions. Unzip it and run `python run.py play` or `python run.py serve`.")
    return result["path"], status


def play_action_ui(token: str, selected_action: str):
    scene, actions, turn = ui(sessions.play_action)(token, selected_action)
    return scene, _choices(actions), turn


def save_app_parameters(*values):
    settings = dict(zip(ui_authoring.DEFAULTS, values))
    try:
        ui_authoring.save_app_settings(settings)
    except (ValueError, OSError) as exc:
        raise gr.Error(str(exc)) from exc
    return "App parameters saved. Restart the UI to apply them. ANE_UI_HOST, ANE_UI_PORT and ANE_UI_SHARE environment variables override launch settings."


def build_app():
    settings = load_settings()
    names, selected_name = _entity_name_values("Locations")
    initial_name, initial_tags, initial_descriptions, initial_goals, *_ = load_entity("Locations", selected_name)

    with gr.Blocks(title="Adaptive Narrative Studio", css=CSS) as demo:
        session_token = gr.State("")
        gr.Markdown(
            "# Adaptive Narrative Studio\n"
            "Author the world, test its narrative behavior, then play the generated game.",
            elem_classes="hero",
        )

        with gr.Tabs():
            with gr.Tab("1 · World editor"):
                with gr.Row():
                    kind = gr.Radio(list(ENTITY_FILES), value="Locations", label="Content type")
                    entity = gr.Dropdown(label="Entry", choices=names, value=selected_name)
                    add_entry = gr.Button("New entry")
                name = gr.Textbox(label="Name", value=initial_name)
                tags = gr.Dropdown(
                    COMMON_TAGS,
                    value=initial_tags,
                    multiselect=True,
                    allow_custom_value=True,
                    label="Tags",
                    info="Type a new tag and press Enter to add it.",
                )
                descriptions = gr.Textbox(
                    label="Descriptions", value=initial_descriptions, lines=5,
                    info="One description per line.",
                )
                goals = gr.Textbox(label="Goals", value=initial_goals, lines=3, info="One goal per line.")
                unique = gr.Checkbox(
                    label="Unique NPC",
                    visible=False,
                    info="Unique: lives in one location for the whole game. "
                    "Unchecked (generic, e.g. guards): may appear in every location that fits its tags.",
                )
                with gr.Row():
                    encounter_event = gr.Dropdown(
                        _event_choices(),
                        value="",
                        label="Encounter event",
                        visible=False,
                        info="Hostile NPCs: this event starts by itself when the NPC is in the scene, "
                        "before the player can act or leave.",
                    )
                    encounter_repeat = gr.Checkbox(
                        label="Repeat on every visit",
                        visible=False,
                        info="Unchecked: only the first encounter in a game.",
                    )
                enabled_if = gr.Textbox(
                    label="Enabled if (persistent variables)",
                    placeholder="e.g. {thief_caught: false, reputation: {gte: 1}}",
                    info="Leave empty to always enable. Comparisons: eq, ne, gt, gte, lt, lte, in, not_in; "
                    "`playthroughs` counts finished playthroughs.",
                )
                event_title = gr.Textbox(label="Event title", visible=False, info="Shown while the event is active.")
                stat_bonus = gr.Textbox(
                    label="Stat bonus while held", visible=False,
                    placeholder="e.g. {attack: 15}", info="Added to the character's stats while the item is in the inventory.",
                )
                with gr.Row():
                    save_entry = gr.Button("Save entry", variant="primary")
                    delete_entry = gr.Button("Delete entry", variant="stop")
                editor_status = gr.Markdown()

                with gr.Accordion("Actions & outcomes of this entry", open=False):
                    gr.Markdown(
                        "Edit the entry's actions as YAML: one-time (`repeat: once`) and exclusive "
                        "(`exclusive_group`) options, events (`triggers_event`), one-way moves (`send_to`), "
                        "persistent variables (`set_variables` / `change_variables`) and more. Everything is "
                        "checked (fields, events, locations, NPCs, items, variables) before saving."
                        + ACTION_REFERENCE
                    )
                    actions_editor = gr.Code(
                        value=load_entity_actions_yaml("Locations", selected_name), language="yaml", label="actions",
                    )
                    with gr.Row():
                        save_actions_button = gr.Button("Validate & save actions", variant="primary")
                        reload_actions_button = gr.Button("Reload from disk")
                    actions_status = gr.Markdown()

                with gr.Accordion("Missions", open=False):
                    gr.Markdown(
                        "Each story draws one mission (weighted by `weight`) whose `enabled_if` holds. Goal keys: "
                        + ", ".join(f"`{key}`" for key in Mission.GOAL_KEYS)
                        + "; all must be met. References and conditions are checked before saving."
                    )
                    missions_editor = gr.Code(value=load_missions_yaml(), language="yaml", label="missions.yaml")
                    with gr.Row():
                        save_missions_button = gr.Button("Validate & save missions", variant="primary")
                        reload_missions_button = gr.Button("Reload from disk")
                    missions_status = gr.Markdown()
                    save_missions_button.click(save_missions_yaml, missions_editor, [missions_editor, missions_status])
                    reload_missions_button.click(load_missions_yaml, None, missions_editor)

                npc_fields = [unique, encounter_event, encounter_repeat, enabled_if, event_title, stat_bonus]
                entry_fields = [name, tags, descriptions, goals, *npc_fields]
                kind.change(entity_names, kind, [entity, *entry_fields]).then(
                    load_entity_actions_yaml, [kind, entity], actions_editor,
                )
                entity.change(_load_entity_ui, [kind, entity], entry_fields)
                entity.change(load_entity_actions_yaml, [kind, entity], actions_editor)
                save_actions_button.click(save_entity_actions_ui, [kind, entity, actions_editor], [actions_editor, actions_status])
                reload_actions_button.click(load_entity_actions_yaml, [kind, entity], actions_editor)
                add_entry.click(new_entity, None, [entity, *entry_fields])
                save_entry.click(
                    save_entity_ui,
                    [kind, entity, *entry_fields],
                    [entity, editor_status],
                )
                delete_entry.click(
                    delete_entity_ui,
                    [kind, entity],
                    [entity, *entry_fields, editor_status],
                )

            with gr.Tab("2 · Simulation settings"):
                gr.Markdown(
                    "### Complete configuration file\n"
                    "Upload one combined YAML file containing all sections from the four files in `config/`. "
                    "It is checked completely before it can replace the built-in runtime configuration."
                )
                with gr.Row():
                    config_upload = gr.File(
                        label="Combined YAML configuration",
                        file_types=[".yaml", ".yml"],
                        type="filepath",
                    )
                    with gr.Column():
                        activate_config = gr.Button("Validate & use uploaded config", variant="primary")
                        restore_config = gr.Button("Use built-in config")
                with gr.Row():
                    pop_size = gr.Number(settings[0], label="Evolution population", precision=0, minimum=1)
                    generations = gr.Number(settings[1], label="Generations", precision=0, minimum=1)
                    episodes = gr.Number(settings[2], label="Episodes / genome", precision=0, minimum=1)
                    max_steps = gr.Number(settings[3], label="Maximum simulation steps", precision=0, minimum=1)
                with gr.Row():
                    health = gr.Number(settings[4], label="Starting health", precision=0, minimum=1)
                    max_health = gr.Number(
                        settings[9], label="Maximum health", precision=0, minimum=1,
                        info="Healing never raises health above this value.",
                    )
                    coins = gr.Number(settings[5], label="Starting coins", precision=0, minimum=0)
                    max_actions = gr.Number(
                        settings[6],
                        label="Game action limit",
                        precision=0,
                        minimum=100,
                        info="The action-limit cutoff cannot be set below 100; other ending conditions still apply.",
                    )
                with gr.Row():
                    deterministic = gr.Checkbox(
                        value=settings[7],
                        label="Deterministic reproducibility",
                        info="Repeat the same evolution when the configuration and seed are unchanged.",
                    )
                    seed = gr.Number(settings[8], label="Reproducibility seed", precision=0)
                save_config = gr.Button("Save settings", variant="primary")
                config_status = gr.Markdown()
                setting_outputs = [
                    pop_size, generations, episodes, max_steps, health, coins, max_actions,
                    deterministic, seed, max_health, config_status,
                ]
                activate_config.click(upload_config, config_upload, setting_outputs)
                restore_config.click(use_builtin_config, None, setting_outputs)
                save_config.click(
                    save_settings,
                    [
                        pop_size, generations, episodes, max_steps, health, coins,
                        max_actions, deterministic, seed, max_health,
                    ],
                    config_status,
                )
                with gr.Accordion("Persistent variables", open=False):
                    gr.Markdown(
                        "Variables that carry over between playthroughs. Outcomes change them with "
                        "`set_variables` / `change_variables`; locations, NPCs, items and missions use "
                        "`enabled_if`. Only real play (the game on the Play tab) saves them."
                    )
                    variables_editor = gr.Code(value=load_variables_yaml(), language="yaml", label="variables.yaml")
                    save_variables_button = gr.Button("Validate & save variable definitions", variant="primary")
                    variables_status = gr.Markdown()
                    gr.Markdown(
                        "#### Stored values of a player profile\n"
                        "Edit them to test later playthroughs (e.g. set `playthroughs: 1` or `reputation: -3`)."
                    )
                    values_profile = gr.Dropdown(
                        player_profiles(), value=config.get("persistence.profile", "default"),
                        allow_custom_value=True, label="Player profile",
                    )
                    stored_values = gr.Code(value=stored_values_yaml(), language="yaml", label="Stored values")
                    with gr.Row():
                        save_values_button = gr.Button("Save stored values", variant="primary")
                        refresh_values_button = gr.Button("Refresh")
                        reset_values_button = gr.Button("Reset profile (new player)", variant="stop")
                    values_status = gr.Markdown()
                    save_variables_button.click(
                        save_variables_yaml, variables_editor, [variables_editor, variables_status],
                    ).then(stored_values_yaml, values_profile, stored_values)
                    values_profile.change(stored_values_yaml, values_profile, stored_values)
                    refresh_values_button.click(stored_values_yaml, values_profile, stored_values)
                    save_values_button.click(save_stored_values_yaml, [values_profile, stored_values], [stored_values, values_status])
                    reset_values_button.click(reset_stored_values_ui, values_profile, [stored_values, values_status])
                with gr.Accordion("Character stats", open=False):
                    gr.Markdown(
                        "Define the stats of this story (any names), the points budget, how players get their "
                        "points, which outcome tags need a check (`tag_checks`) and which stat softens combat "
                        "damage. Difficulty lives in the Weights tab (**Skill check resolution**)."
                    )
                    stats_editor = gr.Code(value=load_stats_yaml(), language="yaml", label="stats.yaml")
                    with gr.Row():
                        save_stats_button = gr.Button("Validate & save character stats", variant="primary")
                        reload_stats_button = gr.Button("Reload from disk")
                    stats_status = gr.Markdown()
                    save_stats_button.click(save_stats_yaml, stats_editor, [stats_editor, stats_status])
                    reload_stats_button.click(load_stats_yaml, None, stats_editor)
                with gr.Accordion("Story rules", open=False):
                    rules = load_rules()
                    locations = list(config.get("world_definition.locations") or {})
                    gr.Markdown("**Missions and goal pursuit**")
                    with gr.Row():
                        rule_missions = gr.Checkbox(rules["missions_enabled"], label="Missions enabled")
                        rule_end_on_goal = gr.Checkbox(rules["end_on_goal"], label="End the story when the goal is met")
                        rule_start = gr.Number(rules["pursuit_start_step"], precision=0, minimum=0, label="Pursuit starts at step")
                        rule_full = gr.Number(rules["pursuit_full_step"], precision=0, minimum=1, label="Full pursuit from step")
                        rule_route = gr.Number(rules["route_weight"], label="Route weight")
                    gr.Markdown("**How often options are offered** (unless an action sets `repeat`)")
                    with gr.Row():
                        rule_repeat = [
                            gr.Dropdown(RULE_CHOICES["repeat"], value=rules[f"repeat_{source}"], label=f"{source.title()} actions")
                            for source in ("location", "npc", "item", "event")
                        ]
                    gr.Markdown("**Arrests and discovery**")
                    with gr.Row():
                        rule_arrest = gr.Dropdown(locations, value=rules["arrest_location"], label="Arrest location")
                        rule_lock = gr.Dropdown(
                            RULE_CHOICES["arrest_lock_when"], value=rules["arrest_lock_when"], label="Confine until escape",
                            info="sent: only when sent there; always: everyone; never: no escape needed",
                        )
                        rule_travel = gr.Checkbox(rules["auto_travel_on_spawn"], label="Follow compelling new paths")
                        rule_threshold = gr.Number(rules["auto_travel_on_spawn_threshold"], label="Commitment threshold")
                    gr.Markdown("**Persistent variables and events**")
                    with gr.Row():
                        rule_profile = gr.Textbox(rules["profile"], label="Default player profile")
                        rule_save_on = gr.Dropdown(RULE_CHOICES["save_on"], value=rules["save_on"], label="Save variables on")
                        rule_simulate = gr.Dropdown(RULE_CHOICES["simulate_with"], value=rules["simulate_with"], label="Evolution plays with")
                    with gr.Row():
                        rule_fallback = gr.Textbox(rules["fallback_action"], label="Event fallback option")
                        rule_fallback_desc = gr.Textbox(rules["fallback_description"], label="Event fallback text")
                    rule_inputs = [
                        rule_missions, rule_end_on_goal, rule_start, rule_full, rule_route, *rule_repeat,
                        rule_arrest, rule_lock, rule_travel, rule_threshold,
                        rule_profile, rule_save_on, rule_simulate, rule_fallback, rule_fallback_desc,
                    ]
                    with gr.Row():
                        save_rules_button = gr.Button("Save story rules", variant="primary")
                        reload_rules_button = gr.Button("Reload")
                    rules_status = gr.Markdown()
                    save_rules_button.click(save_rules_ui, rule_inputs, rules_status)
                    reload_rules_button.click(load_rules_ui, None, rule_inputs)
                with gr.Accordion("Story messages", open=False):
                    gr.Markdown(
                        "Rewrite any sentence the engine adds to a story, e.g. `voyage: \"You head towards "
                        "the {location}.\"`. Each entry lists its `{placeholders}`; a list of texts is used "
                        "in turn. Unknown messages or placeholders are rejected."
                    )
                    messages_editor = gr.Code(value=load_messages_yaml(), language="yaml", label="messages.yaml")
                    with gr.Row():
                        save_messages_button = gr.Button("Validate & save messages", variant="primary")
                        reload_messages_button = gr.Button("Reload from disk")
                    messages_status = gr.Markdown()
                    save_messages_button.click(save_messages_yaml, messages_editor, [messages_editor, messages_status])
                    reload_messages_button.click(load_messages_yaml, None, messages_editor)
                with gr.Accordion("Advanced · all hyperparameters", open=False):
                    gr.Markdown(
                        "Edit every hyperparameter as YAML. The complete structure and value types "
                        "are validated before saving."
                    )
                    hyperparameters_editor = gr.Code(
                        value=load_hyperparameters_yaml(),
                        language="yaml",
                        label="hyperparameters.yaml",
                    )
                    save_all_config = gr.Button("Validate & save all hyperparameters")
                    advanced_status = gr.Markdown()
                    save_all_config.click(
                        save_all_hyperparameters,
                        hyperparameters_editor,
                        [
                            pop_size, generations, episodes, max_steps, health, coins,
                            max_actions, deterministic, seed, max_health, hyperparameters_editor,
                            advanced_status,
                        ],
                    )

            with gr.Tab("3 · Weights"):
                gr.Markdown(
                    "Configure every numeric weight, bonus, penalty, threshold, chance, rate, "
                    "mitigation, floor, and power control used by scoring and the narrative engine."
                )
                weight_entries = _weight_entries()
                weight_paths = [path for path, _ in weight_entries]
                weight_components = []
                grouped_weights = {}
                for weight_path, value in weight_entries:
                    grouped_weights.setdefault(weight_path.split(".")[0], []).append((weight_path, value))
                for section, section_entries in grouped_weights.items():
                    with gr.Accordion(section.replace("_", " ").title(), open=False):
                        for index in range(0, len(section_entries), 3):
                            with gr.Row():
                                for weight_path, value in section_entries[index:index + 3]:
                                    weight_components.append(
                                        gr.Number(value=value, label=weight_path)
                                    )
                save_weights_button = gr.Button("Save all weights", variant="primary")
                weights_status = gr.Markdown()
                save_weights_button.click(
                    save_weights,
                    [gr.State(weight_paths), *weight_components],
                    weights_status,
                )

            with gr.Tab("4 · Simulate & play"):
                gr.Markdown("Run a quick tournament of narrative genomes. The strongest result becomes your playable game.")
                with gr.Accordion("Load a previous best engine", open=False):
                    gr.Markdown(
                        "Upload a generated engine ZIP, or upload an existing complete configuration YAML "
                        "with its model-parameters YAML. If the configuration already contains a "
                        "`generated_run.genome`, the separate parameters file is optional."
                    )
                    with gr.Row():
                        engine_upload = gr.File(
                            label="Engine bundle or configuration",
                            file_types=[".zip", ".yaml", ".yml"],
                            type="filepath",
                        )
                        model_parameters_upload = gr.File(
                            label="Model parameters (optional)",
                            file_types=[".yaml", ".yml"],
                            type="filepath",
                        )
                        load_engine_button = gr.Button("Load best engine & start game", variant="primary")
                with gr.Row():
                    candidates = gr.Slider(1, APP_SETTINGS["candidate_limit"], value=APP_SETTINGS["default_candidates"], step=1, label="Candidate genomes")
                    generations = gr.Slider(1, APP_SETTINGS["generation_limit"], value=APP_SETTINGS["default_generations"], step=1, label="Evolution generations")
                    sim_steps = gr.Slider(1, APP_SETTINGS["simulation_step_limit"], value=APP_SETTINGS["default_simulation_steps"], step=1, label="Steps per simulation")
                    episodes = gr.Slider(
                        1, 10, value=min(10, int(config.get("evolution.episodes_per_genome", 3))), step=1,
                        label="Stories per storyteller", info="More stories per genome: fairer scores, slower learning.",
                    )
                warm_start = gr.Checkbox(
                    value=bool(config.get("evolution.warm_start", True)),
                    label="Continue learning from earlier runs",
                    info="Seed the population with the best storytellers the AI remembers.",
                )
                with gr.Row():
                    deterministic_run = gr.Checkbox(
                        value=settings[7],
                        label="Run deterministically",
                        info="Uses the reproducibility seed saved in Simulation settings.",
                    )
                    play_profile = gr.Dropdown(
                        player_profiles(), value=config.get("persistence.profile", "default"),
                        allow_custom_value=True, label="Player profile",
                        info="Persistent variables are saved to this profile. Type a new name for a new player.",
                    )
                with gr.Accordion("Character stats of the player", open=False):
                    stat_settings = stats_config()
                    gr.Markdown(
                        f"Spread **{(stat_settings.get('allocation') or {}).get('total_points', 50)} points** over "
                        f"{', '.join(f'`{name}`' for name in (stat_settings.get('stats') or {})) or 'no stats'}; each stat takes "
                        f"{(stat_settings.get('allocation') or {}).get('min_points', 5)}-"
                        f"{(stat_settings.get('allocation') or {}).get('max_points', 20)} points. "
                        "Choose `defined` to play with the points below."
                    )
                    with gr.Row():
                        stats_choice = gr.Dropdown(PLAYER_STAT_CHOICES, value="story default", label="Stats")
                        randomize_stats = gr.Button("Randomize")
                        balance_stats = gr.Button("Balance")
                    stats_points = gr.Code(
                        value=content.defined_points_text(),
                        language="yaml", label="Stat points",
                    )
                    stats_check = gr.Markdown()
                    randomize_stats.click(lambda: stats_points_text("random"), None, stats_points).then(
                        lambda: "defined", None, stats_choice,
                    )
                    balance_stats.click(lambda: stats_points_text("balanced"), None, stats_points).then(
                        lambda: "defined", None, stats_choice,
                    )
                    stats_points.change(check_points_text, stats_points, stats_check)
                run_button = gr.Button("Evolve storytellers & start game", variant="primary")
                simulation_report = gr.Markdown(elem_classes="status")
                with gr.Row():
                    learning_plot = gr.LinePlot(
                        x="generation", y="score", color="series", label="Learning curve",
                        x_title="Generation", y_title="Fitness", height=280,
                    )
                    learned_traits = gr.Dataframe(
                        headers=["trait", "meaning", "evolved", "population", "tendency"],
                        label="What the evolved storyteller learned", interactive=False, wrap=True,
                    )
                with gr.Accordion("Learning memory", open=False):
                    memory_status = gr.Markdown(learning_memory_text())
                    forget_button = gr.Button("Forget what the AI learned", variant="stop")
                    forget_button.click(forget_learning_ui, None, memory_status)
                run_state = gr.State(None)
                with gr.Accordion("Export the engine (offline, for a game)", open=False):
                    gr.Markdown(
                        "Download a self-contained engine: the engine code, this world and its tuning, the chosen "
                        "evolved storyteller, the tag vectors it was trained with and (optionally) the learning "
                        "memory. It runs offline with only `pyyaml` and `numpy`: embed it in a Python game "
                        "(`NarrativeEngine`), run `python run.py serve` for Unity/Godot/web games over HTTP, "
                        "`play` it in a terminal, or keep training it with `evolve`. The ZIP's README explains how."
                    )
                    with gr.Row():
                        export_source = gr.Radio(list(EXPORT_SOURCES), value="This run's best", label="Storyteller")
                        export_generation = gr.Dropdown([], label="Generation (for a generation's champion)")
                    with gr.Row():
                        export_memory = gr.Checkbox(True, label="Include the learning memory (keep training offline)")
                        export_name = gr.Textbox(label="Name", placeholder="narrative-engine")
                    export_button = gr.Button("Export offline engine", variant="primary")
                    export_file = gr.File(label="Offline engine (ZIP)", interactive=False)
                    export_status = gr.Markdown()
                    export_button.click(
                        export_engine_ui,
                        [export_source, run_state, export_generation, session_token, export_memory, export_name],
                        [export_file, export_status],
                    )
                scene = gr.Markdown("Run a simulation to begin.")
                choices = gr.Radio([], label="What do you do?", interactive=True)
                choose = gr.Button("Take action", variant="primary")
                turn_result = gr.Textbox(label="Last turn", lines=7, interactive=False)
                generated_config = gr.File(
                    label="Generated story configuration",
                    interactive=False,
                    visible=True,
                )
                generated_engine = gr.File(
                    label="Best model bundle (config + evolved parameters)",
                    interactive=False,
                    visible=True,
                )

                play_outputs = [
                    session_token, simulation_report, scene, choices, turn_result,
                    generated_config, generated_engine,
                ]

                run_button.click(
                    run_simulation_ui,
                    [candidates, generations, sim_steps, deterministic_run, play_profile, stats_choice, stats_points,
                     episodes, warm_start],
                    [*play_outputs, learning_plot, learned_traits, memory_status, run_state, export_generation],
                    concurrency_limit=1,
                    concurrency_id="narrative_evolution",
                )
                load_engine_button.click(
                    load_best_engine_ui,
                    [engine_upload, model_parameters_upload, play_profile, stats_choice, stats_points],
                    [*play_outputs, learning_plot, learned_traits],
                    concurrency_limit=1,
                )
                choose.click(
                    play_action_ui,
                    [session_token, choices],
                    [scene, choices, turn_result],
                    concurrency_limit=1,
                )

            with gr.Tab("5 · Engine logic & app parameters"):
                gr.Markdown(
                    "The engine's logic runs in the backend scripts; here you change **how it behaves** "
                    "through its parameters, section by section. Every section is checked (same settings, "
                    "same value types) before it is saved, and new simulations and games use it at once."
                )
                with gr.Accordion("Engine logic", open=True):
                    sections = logic_sections()
                    logic_section = gr.Dropdown(
                        [item["section"] for item in sections], value="evolution", label="Logic section",
                    )
                    logic_description = gr.Markdown(logic_description_text("evolution"))
                    logic_editor = gr.Code(value=load_logic_section("evolution"), language="yaml", label="Parameters")
                    with gr.Row():
                        save_logic_button = gr.Button("Validate & save parameters", variant="primary")
                        reload_logic_button = gr.Button("Reload")
                    logic_status = gr.Markdown()
                    logic_section.change(load_logic_section, logic_section, logic_editor)
                    logic_section.change(logic_description_text, logic_section, logic_description)
                    reload_logic_button.click(load_logic_section, logic_section, logic_editor)
                    save_logic_button.click(save_logic_section, [logic_section, logic_editor], logic_status)
                with gr.Accordion("App parameters", open=True):
                    gr.Markdown("Saved in config/ui_settings.yml. Upload limits are in bytes; crossover probability is between 0 and 1.")
                    parameter_inputs = []
                    for key, value in ui_authoring.load_app_settings().items():
                        label = key.replace("_", " ").title()
                        if isinstance(value, bool):
                            component = gr.Checkbox(value=value, label=label)
                        elif isinstance(value, str):
                            component = gr.Textbox(value=value, label=label)
                        else:
                            component = gr.Number(value=value, label=label, precision=0 if isinstance(ui_authoring.DEFAULTS[key], int) else None)
                        parameter_inputs.append(component)
                    save_parameters = gr.Button("Save app parameters", variant="primary")
                    parameters_status = gr.Markdown()
                    save_parameters.click(save_app_parameters, parameter_inputs, parameters_status)

    return demo


demo = build_app()


if __name__ == "__main__":
    demo.queue(default_concurrency_limit=APP_SETTINGS["queue_concurrency"], max_size=APP_SETTINGS["queue_max_size"]).launch(
        server_name=os.getenv("ANE_UI_HOST", APP_SETTINGS["server_host"]),
        server_port=int(os.getenv("ANE_UI_PORT", str(APP_SETTINGS["server_port"]))),
        share=os.getenv("ANE_UI_SHARE", str(APP_SETTINGS["share"])).lower() in {"1", "true", "yes"},
    )
