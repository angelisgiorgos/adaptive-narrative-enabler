import unittest
import sys
import types
from unittest.mock import patch

from core_engine.actions import Action, Outcome
from core_engine.game_state import GameState
from core_engine.genome import Genome
from core_engine.world import Character, Location, Object, WorldGraph
from main import _objective_loss_breakdown
from utils.llm_model_loader import VLLMRuntime


class FixedRandom:
    def __init__(self, value=0.999):
        self.value = value

    def random(self):
        return self.value

    def choice(self, values):
        return values[0]

    def choices(self, values, weights=None, k=1):
        return [values[0]]


def genome():
    return Genome.from_dict({
        "threat_prob": 0.3,
        "spawn_prob": 0.5,
        "setup_prob": 0.3,
        "success_bias": 0.5,
        "guard_bias": 0.3,
        "discovery_bias": 0.5,
        "recovery_bias": 0.5,
        "lead_bias": 0.5,
        "bias_urban": 0.5,
        "bias_maritime": 0.5,
        "bias_social": 0.5,
        "bias_threat": 0.5,
        "bias_stealth": 0.5,
        "bias_luxury": 0.5,
        "voyage_bias": 0.5,
    })


def two_node_world(second_locked=False):
    world = WorldGraph()
    world.add_location(Location("A", {"urban"}))
    world.add_location(Location("B", {"urban"}, exit_locked=second_locked))
    return world


class GameplayTests(unittest.TestCase):
    def test_gemma4_vllm_uses_text_only_memory_limits(self):
        fake_vllm = types.ModuleType("vllm")

        class FakeLLM:
            kwargs = None

            def __init__(self, **kwargs):
                FakeLLM.kwargs = kwargs

        class FakeSamplingParams:
            pass

        fake_vllm.LLM = FakeLLM
        fake_vllm.SamplingParams = FakeSamplingParams
        runtime = VLLMRuntime()
        with patch.dict(sys.modules, {"vllm": fake_vllm}):
            runtime._ensure_embedded_engine()

        self.assertEqual(runtime.model_name, "google/gemma-4-E2B-it")
        self.assertEqual(
            FakeLLM.kwargs["limit_mm_per_prompt"],
            {"image": 0, "audio": 0},
        )
        self.assertEqual(FakeLLM.kwargs["max_model_len"], 4096)

    def test_unchecked_outcomes_are_deterministic(self):
        outcome = Outcome("Always succeeds")
        state = GameState(two_node_world(), genome(), start="A", rng=FixedRandom())
        self.assertEqual(outcome.success_probability(genome(), state), 1.0)
        self.assertTrue(outcome.success(genome(), state))

    def test_explicit_check_is_visible_and_probabilistic(self):
        action = Action(
            "Take the guarded object",
            [Outcome("Taken", tags=["stealth"])],
            check={"skill": "stealth", "base_success": 0.6},
        )
        state = GameState(two_node_world(), genome(), start="A", rng=FixedRandom())
        summary = state._action_check_summary(action)
        self.assertIn("stealth check", summary)
        self.assertLess(action.outcomes[0].success_probability(genome(), state, action), 1.0)

    def test_discovery_connects_both_directions_without_auto_move(self):
        world = two_node_world()
        state = GameState(world, genome(), start="A")
        state.apply(Outcome("A route appears", spawn=True, reveal_location="B"))

        self.assertEqual(state.current.name, "A")
        self.assertIn("B", world.locations["A"].connected)
        self.assertIn("A", world.locations["B"].connected)
        self.assertIn("Move to B", [action.name for action in state.get_available_actions()])

        state._move_to("B")
        self.assertIn("Move to A", [action.name for action in state.get_available_actions()])

    def test_self_edges_and_duplicate_location_names_are_rejected(self):
        world = two_node_world()
        self.assertFalse(world.connect("A", "A"))
        with self.assertRaises(ValueError):
            world.add_location(Location(" a ", {"duplicate"}))

    def test_parameterized_templates_allow_variants_but_reject_exact_scenarios(self):
        world = WorldGraph()
        world.add_location(Location(
            "North Market",
            {"urban", "trade"},
            template_id="district_market",
            parameters={"district": "north"},
        ))
        world.add_location(Location(
            "South Market",
            {"urban", "trade"},
            template_id="district_market",
            parameters={"district": "south"},
        ))
        with self.assertRaises(ValueError):
            world.add_location(Location(
                "North Market Copy",
                {"urban", "trade"},
                template_id="district_market",
                parameters={"district": "north"},
            ))

    def test_world_extension_selects_by_tags_not_authored_transition(self):
        world = WorldGraph()
        world.add_location(Location("A", {"urban"}))
        world.add_location(Location("Forest", {"nature", "secluded"}))
        world.add_location(Location("Guard Post", {"urban", "guarded"}))
        world.allow_spawn("A", "Guard Post")
        state = GameState(world, genome(), start="A")

        selected = world.spawn(
            state.current,
            target_tags={"nature", "secluded"},
            genome=state.genome,
            state=state,
        )
        self.assertEqual(selected.name, "Forest")

    def test_tag_driven_discovery_is_recorded_for_objective_losses(self):
        world = WorldGraph()
        world.add_location(Location("A", {"urban"}))
        world.add_location(Location("Forest", {"nature", "secluded"}))
        state = GameState(world, genome(), start="A")
        state.apply(Outcome(
            "A woodland trail appears",
            spawn=True,
            target_tags=["nature", "secluded"],
        ))

        self.assertIn("Forest", world.locations["A"].connected)
        self.assertEqual(state.transition_records[0]["resolution"], "tags")

    def test_duplicate_scenario_events_create_an_objective_loss(self):
        world = two_node_world()
        state = GameState(world, genome(), start="A")
        action = Action("Repeatable beat", [Outcome("Repeated", tags=["social"])])
        state.actions_taken = 1
        state.apply(action.outcomes[0], action=action)
        state.actions_taken = 2
        state.apply(action.outcomes[0], action=action)

        losses = _objective_loss_breakdown(state)
        self.assertGreater(losses["scenario_duplication"], 0)

    def test_locked_location_needs_explicit_unlock(self):
        world = two_node_world(second_locked=True)
        world.connect("A", "B")
        state = GameState(world, genome(), start="B")
        self.assertNotIn("Move to A", [action.name for action in state.get_available_actions()])

        state.apply(Outcome("The exit opens", unlock_exit=True))
        self.assertIn("Move to A", [action.name for action in state.get_available_actions()])

    def test_item_template_uses_item_name_not_description(self):
        world = two_node_world()
        wine = Object(
            "Vintage Wine",
            {"urban", "alcohol"},
            descriptions=["The labeling is in a prestigious script"],
        )
        world.add_object(wine)
        guard_action = Action(
            "Give him the {alcohol_desc}",
            [Outcome("Accepted")],
            character_name="Guard",
            required_tag="alcohol",
            source_type="npc",
            source_name="Guard",
        )
        world.add_character(Character(
            "Guard",
            {"urban"},
            actions=[guard_action],
        ))
        state = GameState(world, genome(), start="A")
        state.inventory.add("Vintage Wine")

        names = [action.name for action in state.get_available_actions()]
        self.assertIn("Give him the Vintage Wine", names)
        self.assertFalse(any("prestigious script" in name for name in names))

    def test_npc_actions_are_not_hidden_and_goals_gain_progress(self):
        world = two_node_world()
        priest_action = Action(
            "Offer guidance",
            [Outcome("The palace is protected", tags=["palace", "success"])],
            character_name="Priest",
            source_type="npc",
            source_name="Priest",
            npc_goals=["Protect the palace"],
        )
        world.add_character(Character(
            "Priest",
            {"urban"},
            goals=["Protect the palace"],
            actions=[priest_action],
        ))
        state = GameState(world, genome(), start="A")
        actions = state.get_available_actions()
        selected = next(action for action in actions if action.character_name == "Priest")
        state.apply(selected.outcomes[0], action=selected)

        self.assertIn("Priest", state.npc_goal_progress)
        self.assertGreater(state.npc_goal_progress["Priest"], 0)

    def test_mandatory_encounter_blocks_navigation_and_returns(self):
        world = two_node_world()
        world.connect("A", "B")
        encounter = Action(
            "Confront the bandits",
            [Outcome("The bandits retreat")],
            mandatory=True,
            encounter=True,
            return_to_previous=True,
            source_type="location",
            source_name="Bandit ambush",
        )
        world.locations["B"].actions = [encounter]
        state = GameState(world, genome(), start="A")
        state._move_to("B")

        actions = state.get_available_actions()
        self.assertEqual([action.name for action in actions], ["Confront the bandits"])
        state.apply(actions[0].outcomes[0], action=actions[0])
        self.assertEqual(state.current.name, "A")

    def test_setup_gates_matching_payoff(self):
        payoff = Outcome(
            "The earlier clue pays off",
            requires_setup="palace_route",
            payoff_id="palace_route",
        )
        action = Action("Use clue", [payoff])
        state = GameState(two_node_world(), genome(), start="A")
        self.assertEqual(action.available_outcomes(state), [])

        state.apply(Outcome("Found clue", setup_id="palace_route"))
        self.assertEqual(action.available_outcomes(state), [payoff])
        state.apply(payoff, action=action)
        self.assertIn("palace_route", state.completed_payoffs)

    def test_power_is_combat_rating_and_increases_with_weapon(self):
        world = two_node_world()
        world.add_object(Object("Sword", {"weapon", "sword"}))
        state = GameState(world, genome(), start="A")
        before = state.power
        state.inventory.add("Sword")
        state.update_power()
        self.assertGreater(state.power, before)


if __name__ == "__main__":
    unittest.main()
