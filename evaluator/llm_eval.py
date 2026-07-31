import json
import torch


from utils.config_loader import config
from utils.llm_model_loader import load_causal_lm_with_fallback


class LLMNarrativeEvaluator:
    def __init__(
        self,
        model_name=None,
        max_new_tokens=None,
        temperature=None
    ):
        self.model_name = model_name or config.get("llm_evaluator.model_name", "meta-llama/Llama-3.2-3B-Instruct")
        self.max_new_tokens = max_new_tokens or config.get("llm_evaluator.max_new_tokens", 2048)
        self.temperature = temperature or config.get("llm_evaluator.temperature", 0.3)

        torch.cuda.empty_cache()
        self.tokenizer, self.model, self.model_name = load_causal_lm_with_fallback(
            self.model_name,
            purpose="LLM evaluator",
            prefer_quantized=True,
        )

        self.device = self.model.device

    # ----------------------------
    # Core generation
    # ----------------------------
    def generate(self, prompt, **kwargs):
        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.device)
        prompt_length = inputs["input_ids"].shape[1]

        gen_params = {
            "max_new_tokens": self.max_new_tokens,
            "temperature": self.temperature,
            "do_sample": True,
            "pad_token_id": self.tokenizer.eos_token_id
        }
        gen_params.update(kwargs)

        with torch.no_grad():
            outputs = self.model.generate(
                **inputs,
                **gen_params
            )

        # Only decode the NEWly generated tokens
        new_tokens = outputs[0][prompt_length:]
        text = self.tokenizer.decode(new_tokens, skip_special_tokens=True)

        return text

    # ----------------------------
    # JSON extraction helper
    # ----------------------------
    def extract_json(self, text):
        try:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start != -1 and end > start:
                json_str = text[start:end]
                # Try standard parsing first
                try:
                    return json.loads(json_str)
                except json.JSONDecodeError:
                    # Try a more aggressive regex-based approach for common LLM mistakes
                    import re
                    # Remove potential trailing commas or other minor junk
                    json_str = re.sub(r',\s*([\]}])', r'\1', json_str)
                    return json.loads(json_str)
            return None
        except Exception as e:
            print(f"JSON extraction failed: {e}")
            return None

    # ----------------------------
    # Story evaluation
    # ----------------------------
    def evaluate_story(self, state):
        story_text = state.get_full_story()

        prompt = f"""
You are an expert narrative designer.

Evaluate the following interactive story.

Score from 0 to 100 based on:
- coherence
- narrative tension
- pacing
- setup and payoff

Return ONLY JSON:
{{"score": number, "reason": "short explanation"}}

STORY:
{story_text}
"""

        raw_output = self.generate(prompt)

        parsed = self.extract_json(raw_output)

        if parsed is None:
            return 50, "Parsing failed"

        score = parsed.get("score", 50)
        reason = parsed.get("reason", "No reason")

        return score, reason

    # ----------------------------
    # Algorithmic Structural Evaluation
    # ----------------------------
    def calculate_structural_score(self, state):
        """Calculates a mathematical score based on story structure (non-LLM)."""
        # 1. Location Repetition & Distribution
        total_locs_in_world = len(state.world.locations)
        unique_visited = len(state.location_counts)
        dist_score = (unique_visited / total_locs_in_world) * 100
        
        # Penalize sequential repeats (A -> A -> A)
        repeats = 0
        for i in range(len(state.path) - 1):
            if state.path[i] == state.path[i+1]:
                repeats += 1
        repeat_penalty = min(50, repeats * 10)
        
        loc_score = max(0, dist_score - repeat_penalty)
        
        # 2. Character Diversity
        total_chars = len(state.world.characters)
        chars_met = len(state.character_interactions)
        char_score = (chars_met / max(1, total_chars)) * 100
        
        # 3. Threat Density (Target ~30% of actions)
        threat_count = state.tags_seen.count("threat") + state.tags_seen.count("combat")
        density = threat_count / max(1, state.actions_taken)
        # Score is highest at 0.3 density, drops if 0.0 or 1.0
        threat_score = 100 * (1.0 - abs(density - 0.3) / 0.7)
        
        # 4. Success/Failure Balance (Target 50/50 mix)
        successes = state.tags_seen.count("success")
        failures = state.tags_seen.count("failure")
        total_outcomes = successes + failures
        if total_outcomes > 0:
            balance = successes / total_outcomes
            balance_score = 100 * (1.0 - abs(balance - 0.5) / 0.5)
        else:
            balance_score = 0 # No success/failure tags at all is bad
            
        final_math = (loc_score * 0.4) + (char_score * 0.2) + (threat_score * 0.2) + (balance_score * 0.2)
        
        breakdown = {
            "location": int(loc_score),
            "character": int(char_score),
            "threat": int(threat_score),
            "balance": int(balance_score),
            "total": int(final_math)
        }
        return breakdown

    # ----------------------------
    # Debug mode
    # ----------------------------
    def evaluate_with_debug(self, state):
        # Use the new detailed story transcript system
        story_text = state.get_full_story()
        
        # 1. Structural Calculation
        math_breakdown = self.calculate_structural_score(state)
        math_score = math_breakdown["total"]

        # 2. LLM Narrative Calculation
        prompt = f"""
### Task: CRITICAL Narrative Story Evaluation
### Objective: Evaluate the following interactive story for quality, narrative momentum, and internal consistency.

### Story Transcript:
{story_text}

### Evaluation Criteria (BE HARSHLY CRITICAL):
1. **logic**: (0-100) Do events follow a logical progression? Are setups resolved?
2. **momentum**: (0-100) Does the story avoid loops? Subtract 30 points if the traveler repeats the same location more than 3 times without a major plot transition.
3. **pacing**: (0-100) Is there a clear early-game setup and a late-game payoff? 
4. **variety**: (0-100) Is there a mix of combat, social, and exploration? Or is it monotonous?

### Final Instructions:
- YOUR RESPONSE MUST BE A SINGLE JSON OBJECT.
- YOU MUST PROVIDE INDIVIDUAL SCORES FOR: "logic", "momentum", "pacing", "variety".
- Calculate a "score" which is the average of the four.
- DO NOT INCLUDE ANY CONVERSATIONAL TEXT.

### REQUIRED JSON FORMAT:
{{
  "logic": number,
  "momentum": number,
  "pacing": number,
  "variety": number,
  "score": number,
  "reason": "short explanation"
}}

### YOUR RESPONSE:
"""

        raw_output = self.generate(prompt, temperature=0.1)

        print("\n--- RAW LLM EVALUATION OUTPUT ---")
        print(raw_output)
        print("----------------------------------")

        parsed = self.extract_json(raw_output)

        if parsed is None:
            return math_score, f"[Math Score: {math_score}] LLM Parsing failed."

        # 3. Hybrid Calculation (50/50 Weight)
        llm_score = parsed.get("score", 50)
        final_score = int((0.5 * math_score) + (0.5 * llm_score))
        
        reason = f"Math[{math_breakdown['location']}/{math_breakdown['character']}/{math_breakdown['threat']}/{math_breakdown['balance']}] LLM[{parsed.get('logic', 0)}/{parsed.get('momentum', 0)}/{parsed.get('pacing', 0)}/{parsed.get('variety', 0)}] - {parsed.get('reason', '')}"

        return final_score, reason
