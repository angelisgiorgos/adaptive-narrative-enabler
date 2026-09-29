import random
import unittest

import main as engine
from core_engine import Action, Character, Event, GameState, Genome, Location, Outcome, WorldGraph


def _genome():
    random.seed(7)
    genome = Genome()
    # Keep procedural unexpected events out of these scenarios.
    genome.event_prob = 0.0
    genome.chaos_bias = 0.0
    return genome


def _world(event_actions, trigger_outcomes=None):
    world = WorldGraph()
    market = Location("Market", ["urban", "trade", "market"])
    market.actions = [Action("Browse the stalls", [Outcome("You browse.", tags=["neutral"])])]
    alley = Location("Alley", ["urban", "dark"])
    market.connect(alley)
    world.add_location(market)
    world.add_location(alley)
    world.add_character(Character(
        "Thief",
        ["market", "stealth"],
        known_locations=["Market", "Alley"],
        actions=[Action(
            "(Thief) Confront the thief",
            trigger_outcomes or [Outcome("You step in.", tags=["event"])],
            character_name="Thief",
            triggers_event="confront_thief",
        )],
    ))
    world.add_event(Event("confront_thief", title="Confronting the thief",
                          tags=["confrontation"], descriptions=["The thief freezes."],
                          actions=event_actions))
    world.add_event(Event("chase", title="The chase", descriptions=["They run!"],
                          actions=[Action("Give up the chase", [Outcome("You let them go.")])]))
    return world


def _choose(state, name):
    names = [action.name for action in state.ranked_available_actions()]
    return state.step(names.index(name))


class EventTests(unittest.TestCase):
    def _state(self, event_actions, **kwargs):
        return GameState(_world(event_actions, **kwargs), _genome(), start="Market", rng=random.Random(1))

    def test_event_offers_only_its_actions_and_returns_to_origin(self):
        state = self._state([
            Action("Fight the thief", [Outcome("The thief flees.", tags=["combat", "success"], remove_npc=True)]),
            Action("Interrogate the thief", [Outcome("They talk.", tags=["social"])]),
            Action("Steal from the thief", [Outcome("You lift a pouch.", tags=["stealth"], coin_change=5)]),
        ])
        _choose(state, "(Thief) Confront the thief")

        self.assertEqual(state.active_event.name, "confront_thief")
        self.assertEqual(state.scene_name, "Confronting the thief (at Market)")
        self.assertCountEqual(
            [a.name for a in state.ranked_available_actions()],
            ["Fight the thief", "Interrogate the thief", "Steal from the thief"],
        )
        path, visits = list(state.path), dict(state.location_counts)

        _choose(state, "Fight the thief")

        self.assertIsNone(state.active_event)
        self.assertEqual(state.current.name, "Market")
        # Returning from an event is not a new visit to the location.
        self.assertEqual((state.path, state.location_counts), (path, visits))
        self.assertIn("Thief", state.removed_npcs)
        remaining = [a.name for a in state.ranked_available_actions()]
        self.assertNotIn("(Thief) Confront the thief", remaining)
        self.assertFalse(any("Thief" in name for name in remaining))
        self.assertIn("confrontation", state.tags_seen[-1])
        self.assertTrue(state.history[-1].startswith("[EVENT END]"))
        self.assertEqual(state.event_log[-1]["action"], "Fight the thief")

    def test_npc_stays_when_outcome_does_not_remove_it(self):
        state = self._state([Action("Interrogate the thief", [Outcome("They talk.", tags=["social"])])])
        _choose(state, "(Thief) Confront the thief")
        _choose(state, "Interrogate the thief")

        self.assertNotIn("Thief", state.removed_npcs)
        remaining = [a.name for a in state.ranked_available_actions()]
        self.assertIn("(Thief) Talk with Thief", remaining)
        # The trigger was already used during this visit.
        self.assertNotIn("(Thief) Confront the thief", remaining)

    def test_outcome_can_move_player_out_of_event(self):
        state = self._state([Action("Steal from the thief", [Outcome("A guard chases you off.", move_to="Alley")])])
        _choose(state, "(Thief) Confront the thief")
        _choose(state, "Steal from the thief")

        self.assertIsNone(state.active_event)
        self.assertEqual(state.current.name, "Alley")
        self.assertTrue(any("carried you to Alley" in line for line in state.history))
        self.assertTrue(state.history[-2].startswith("\nYou are in the Alley"))

    def test_fallback_action_when_no_event_action_is_available(self):
        state = self._state([Action("Bribe the thief", [Outcome("They take it.")], required_coins=999)])
        _choose(state, "(Thief) Confront the thief")

        actions = state.ranked_available_actions()
        self.assertEqual(len(actions), 1)
        _choose(state, actions[0].name)
        self.assertIsNone(state.active_event)
        self.assertEqual(state.current.name, "Market")

    def test_failed_trigger_does_not_start_event(self):
        state = self._state(
            [Action("Fight the thief", [Outcome("Fight.")])],
            trigger_outcomes=[Outcome("You hesitate.", success_prob=0.0)],
        )
        _choose(state, "(Thief) Confront the thief")
        self.assertIsNone(state.active_event)

    def test_event_action_can_chain_into_another_event(self):
        state = self._state([Action("Chase the thief", [Outcome("They bolt.")], triggers_event="chase")])
        _choose(state, "(Thief) Confront the thief")
        _choose(state, "Chase the thief")

        self.assertEqual(state.active_event.name, "chase")
        self.assertEqual(state.event_source_npc, "Thief")
        self.assertEqual([a.name for a in state.ranked_available_actions()], ["Give up the chase"])
        _choose(state, "Give up the chase")
        self.assertIsNone(state.active_event)
        self.assertEqual(state.current.name, "Market")
        self.assertEqual([entry["event"] for entry in state.event_log], ["confront_thief", "chase"])

    def test_shipped_config_defines_thief_event(self):
        world = engine.build_world(_genome())
        event = world.events["confront_thief"]
        self.assertEqual(
            [a.name for a in event.actions],
            ["Fight the thief", "Interrogate the thief", "Steal from the thief"],
        )
        thief = next(c for c in world.characters if c.name == "Thief")
        trigger = next(a for a in thief.actions if a.triggers_event)
        self.assertEqual(trigger.triggers_event, "confront_thief")
        self.assertTrue(trigger.outcomes, "trigger actions without outcomes get a default one")


if __name__ == "__main__":
    unittest.main()
