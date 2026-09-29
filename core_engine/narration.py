"""Every sentence the engine writes into a story, customisable in config/messages.yaml.

A message is a template with ``{placeholders}`` (Python format syntax, so
``{health:+d}`` works). It may also be a list of variants; variants rotate in
order each time the message is used, so a story never repeats the same wording
twice in a row and the choice is reproducible (no random selection).
"""
import string

from utils.config_loader import config

DEFAULT_MESSAGES = {
    # Missions and endings
    "mission_start": "[MISSION] {title}: {description}",
    "mission_start_brief": "[MISSION] {title}",
    "mission_complete": "[MISSION COMPLETE] {title}",
    "end_line": "[END] {reason}",
    "end_death": "The traveler can no longer continue.",
    "end_mission": "Mission complete: {title}.",
    "end_action_limit": "The story reached its action limit of {limit} steps.",
    "end_stalled": "The story stalled in {location} for too long.",
    "end_dungeon": "The traveler was thrown into the {location}.",
    # Arriving and staying
    "arrival": "\nYou are in the {location}. {description}",
    "arrival_npc": "Met a {npc}: {description}",
    "arrival_item": "Nearby you see: {description}",
    "continue_here": "\nContinuing your activities in the {location}...",
    # Choices and results
    "action_taken": "> {action}",
    "result": "RESULT: {description}",
    "failure": "[FAILURE] {action} failed.",
    "failure_arrest": "[FAILURE] The traveler lost the encounter and was dragged to the {location}.",
    # Travel
    "travel_new": "Move to {location}",
    "travel_return": "Return to {location}",
    "travel_result": "You travel to {location}.",
    "location_current": "[LOCATION] Current location: {location}",
    "location_moved": "[LOCATION] Transferred from {origin} to {destination}",
    "location_stayed": "[LOCATION] Still at {location}",
    "auto_transfer": "[AUTO TRANSFER] No actions remained in {origin}, so a new route carried you to {destination}.",
    "spawn": "[SPAWN] New path discovered: {location}",
    "spawn_forced": "[SPAWN] A situational path opens to: {location}",
    "voyage": "[VOYAGE] The opening draws you toward {location}.",
    "escape": "[ESCAPE] You found a way out of {location}.",
    # People and things
    "talk_action": "Talk with {npc}",
    "talk_default": "{npc} shares a few thoughts with you.",
    "talk_goal": " They speak about their goal: {goal}.",
    "reveal_npc": "[DISCOVERY] You noticed {npc} in the scene.",
    "reveal_item": "[DISCOVERY] You spotted the {item} nearby.",
    "lead_path": "[LEAD] {npc} told you about the way to: {location}",
    "lead_move": "\n >>> [LEAD] {npc} led you to: {location}",
    "npc_removed": "[NPC REMOVED] {npc} is no longer part of the story.",
    "npc_removed_scene": "[NPC REMOVED] {npc} leaves the {location}.",
    "encounter": "[ENCOUNTER] {npc} confronts you!",
    "item_collected": "You collected the {item}.",
    "item_used": "You have utilized the {item}.",
    "paid": "[RESOURCES] Paid: {coins} coins (Balance: {balance})",
    "health_capped": "[HEALTH] Already at {health}/{max_health}; healing is capped at the maximum.",
    # Events
    "event_start": "[EVENT] {title}: {description}",
    "event_trigger_result": "{title} begins.",
    "event_chain": "[EVENT END] {title} gives way to a new situation.",
    "event_end_return": "[EVENT END] {title} is over. You are back in the {location}.",
    "event_end_moved": "[EVENT END] {title} is over. The outcome carried you to {location}.",
    "event_unknown": "[EVENT] Unknown event '{event}' was ignored.",
    "unexpected_event": "[UNEXPECTED EVENT] {description}",
    "unexpected_effect": "[EVENT EFFECT] Health {health:+d}, coins {coins:+d}.",
    "unexpected_discovery": "[EVENT DISCOVERY] A path opens to {location}.",
}


# Values the engine passes beyond those the default text uses.
EXTRA_FIELDS = {
    "arrival_item": {"item"},
    "failure_arrest": {"action"},
    "talk_goal": {"npc"},
}


def allowed_fields(key):
    """Placeholders a message may use."""
    return placeholders(DEFAULT_MESSAGES[key]) | EXTRA_FIELDS.get(key, set())


class _KeepUnknown(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def placeholders(template):
    """Field names used by a template, e.g. {"location"} for "Move to {location}"."""
    return {field for _, field, _, _ in string.Formatter().parse(template) if field}


def _variants(value):
    return list(value) if isinstance(value, list) else [value]


def validate_messages(messages):
    """Return a list of problems with a messages mapping (empty when valid)."""
    if not isinstance(messages, dict):
        return ["`messages` must be a mapping of message keys to text."]
    errors = []
    for key, value in messages.items():
        if key not in DEFAULT_MESSAGES:
            errors.append(f"Unknown message `{key}`.")
            continue
        variants = _variants(value)
        if not variants or not all(isinstance(v, str) for v in variants):
            errors.append(f"`messages.{key}` must be text or a non-empty list of texts.")
            continue
        allowed = allowed_fields(key)
        for variant in variants:
            try:
                unknown = placeholders(variant) - allowed
            except ValueError as exc:
                errors.append(f"`messages.{key}`: {exc}.")
                continue
            if unknown:
                errors.append(
                    f"`messages.{key}` uses unknown placeholder(s) {', '.join(sorted(unknown))}; "
                    f"available: {', '.join(sorted(allowed)) or 'none'}."
                )
    return errors


class Narrator:
    """Formats messages for one story, rotating through each message's variants."""

    def __init__(self):
        self.uses = {}

    def __call__(self, key, stable=None, **values):
        """Format ``key``. With ``stable`` (e.g. a location name) the variant depends
        only on that value, which keeps action names identical between turns."""
        configured = (config.get("messages") or {}).get(key)
        variants = _variants(configured) if configured else [DEFAULT_MESSAGES[key]]
        if stable is not None:
            index = sum(ord(ch) for ch in str(stable))
        else:
            index = self.uses.get(key, 0)
            self.uses[key] = index + 1
        template = variants[index % len(variants)]
        try:
            return template.format_map(_KeepUnknown(values))
        except (ValueError, TypeError):
            # A malformed customisation must never stop a story.
            return DEFAULT_MESSAGES[key].format_map(_KeepUnknown(values))


def message(key, **values):
    """Format a message once, outside a story (always the first variant)."""
    return Narrator()(key, **values)
