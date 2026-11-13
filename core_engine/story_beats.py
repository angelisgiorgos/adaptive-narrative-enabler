"""The beats a story is made of, as the engine makes them (each one switched and tuned in
config/hyperparameters.yaml; GameState inherits them):

- climax: a quest ends in a confrontation, not a checkbox (``climax``);
- failures as beats: a failure that led nowhere shows another way, a paid attempt that
  failed returns part of the price (``director.another_way``, ``director.failure_refund``),
  and ``failure_lint`` lists the failures a world leaves as dead ends;
- variation: an outcome told again uses its ``variants`` or says it was told before, and
  what worked once is harder the next time (``variation``);
- travel: fast travel to places visited along known ways, and first trips that are part of
  the story (``travel``);
- stakes: the alert level that ends in a manhunt, and what a failure costs, shown before
  choosing (``stakes``);
- reveals: NPC secrets, consequences that show later, and what happened last time
  (``consequences``, ``memory``).
"""
import re

from utils.config_loader import config

from . import rules
from .actions import Action, Outcome
from .world import Event


def _cap(text):
    text = str(text or "")
    return text[:1].upper() + text[1:]


def _short(text, limit=110):
    text = str(text or "").strip().split(". ")[0].rstrip(".")
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


class StoryBeats:
    """Mixed into GameState: the climax, failures, variation, travel, stakes and reveals."""

    def _beat(self, kind, **info):
        """Log one of the engine's own beats (fitness.beats reads them)."""
        self.__dict__.setdefault("beat_log", []).append({"turn": self.actions_taken, "kind": kind, **info})

    def _setting(self, section):
        cfg = config.get(section) or {}
        return cfg if cfg.get("enabled", False) else None

    # ---- the climax: every quest ends in a confrontation ------------------------------------

    def _climax_state(self):
        return self.__dict__.setdefault("climax_state", {"attempts": 0, "done": False, "antagonist": None,
                                                         "armed": None, "next_turn": 0})

    def _climax_holds_win(self):
        """Whether the quest's win waits for its climax: climax on, not played out yet, and the
        story has run climax.min_turns turns (a quest done at once needs no showdown). Beating
        someone who matters (importance 2+) on the quest's last turn is its climax already."""
        cfg = self._setting("climax")
        state = self._climax_state()
        if cfg is None or self.mission is None or state["done"]:
            return False
        if self.actions_taken < int(cfg.get("min_turns", 6)):
            return False
        last = (self.__dict__.get("defeated") or [{}])[-1]
        if last.get("turn") == self.actions_taken and self.importance(last.get("npc")) >= 2 and not state["armed"]:
            state["done"], state["antagonist"] = True, last["npc"]
            self._beat("climax", npc=last["npc"], won=True, attempts=1)
            self._resolve_open_threads(int(cfg.get("revelations", 2)))
            return False
        return True

    def _climax_antagonist(self):
        """Who stands in the way at the end: a hostile NPC (an encounter, or tagged threat) the
        quest is about, else the most important hostile one who belongs here or was met."""
        targets = self._mission_targets()
        met = self.__dict__.get("met", {})
        best = None
        for char in self.world.characters:
            if self._is_removed(char.name) or not self.is_enabled(char):
                continue
            tags = {str(t).lower() for t in char.associated_tags}
            if not (char.encounter_event or "threat" in tags):
                continue
            target = char.name in targets
            here = (self.npc_homes.get(char.name) == self.current.name) if char.unique else self._npc_fits_location(char)
            if not (target or here or char.name in met):
                continue
            key = (target, self.importance(char.name), here, char.name in met, char.name)
            best = key if best is None or key > best else best
        return best[-1] if best else None

    def _arm_climax(self):
        """Offer the climax (an event of the story's own) once nothing else is under way."""
        cfg = self._setting("climax") or {}
        state = self._climax_state()
        if (state["armed"] or self.active_event is not None or self._pending_event is not None or self.ended
                or self.actions_taken < state["next_turn"]):
            return
        antagonist = state["antagonist"] if state["antagonist"] and not self._is_removed(state["antagonist"]) \
            else self._climax_antagonist()
        state["antagonist"] = antagonist
        called = self._called(antagonist) if antagonist else ""
        title = self.mission.title

        def fmt(text, **extra):
            return str(text).format(antagonist=called, antagonist_cap=_cap(called), title=title, **extra)

        modifier = int(cfg.get("check_modifier", 0))
        damage = int(cfg.get("failure_damage", 2))
        actions = []
        for spec in cfg.get("choices") or []:
            stat = spec.get("stat")
            if stat not in self.stats.names:
                continue
            tags = [str(t) for t in spec.get("tags", [])]
            actions.append(self._climax_action(
                fmt(spec["name"] if antagonist else spec.get("free_name", spec["name"])), fmt(spec.get("desc", "")),
                fmt(cfg.get("failure_desc" if antagonist else "failure_desc_free", "")), tags, stat, modifier, damage))
        ally_spec = cfg.get("ally_choice") or {}
        allies = sorted(self.__dict__.get("allies", ()))
        if allies and ally_spec and ally_spec.get("stat") in self.stats.names:
            ally = self._called(allies[0])
            actions.append(self._climax_action(
                fmt(ally_spec["name"], ally=ally, ally_cap=_cap(ally)), fmt(ally_spec.get("desc", ""), ally=ally, ally_cap=_cap(ally)),
                fmt(cfg.get("failure_desc" if antagonist else "failure_desc_free", "")), [str(t) for t in ally_spec.get("tags", [])],
                ally_spec["stat"], modifier + int(ally_spec.get("bonus", 20)), damage))
        if not actions:
            state["done"] = True  # no stat to play it with: the quest is won as it stands
            return
        name = f"_climax_{self.actions_taken}"
        self.world.events[name] = Event(
            name, title=fmt(cfg.get("title", "Showdown: {title}")), tags=["climax", "confrontation", "threat"],
            descriptions=[fmt(cfg.get("description" if antagonist else "description_free", ""))], actions=actions)
        state["armed"] = name
        self._pending_event = (name, antagonist)
        self._say("climax_start", title=title)
        self.tags_seen.append(["finale", "threat"])

    @staticmethod
    def _climax_action(name, desc, failure_desc, tags, stat, modifier, damage):
        return Action(name, [
            Outcome(desc, tags=tags + ["success", "confrontation"], on_check="pass"),
            Outcome(failure_desc, tags=tags + ["failure", "threat"], on_check="fail", health_change=-damage),
        ], source="event", owner="climax", repeat="always", skill_check={"stat": stat, "modifier": modifier})

    def _resolve_climax(self, action, succeeded):
        """After a climax choice: won (the quest completes), thrown back (it comes again), or,
        after climax.max_attempts setbacks, won at a price: a quest is never lost at its last step."""
        cfg = self._setting("climax") or {}
        state = self._climax_state()
        state["armed"] = None
        antagonist = state["antagonist"]
        called = self._called(antagonist) if antagonist else ""
        if action.source == "event_fallback":
            state["next_turn"] = self.actions_taken + int(cfg.get("retry_turns", 1))
            return
        if not succeeded:
            state["attempts"] += 1
            if state["attempts"] < int(cfg.get("max_attempts", 2)) and self.health_player.is_alive:
                state["next_turn"] = self.actions_taken + int(cfg.get("retry_turns", 1))
                self._say("climax_setback", antagonist=_cap(called) if called else "Fate")
                return
            price = max(0, min(int(cfg.get("price_health", 2)), self.health - 1))
            if price:
                self.health_player.change(-price)
                self.update_power()
            self._say("climax_price", title=self.mission.title)
        else:
            self._say("climax_won", title=self.mission.title)
        state["done"] = True
        self._mission_completed = True
        if antagonist and succeeded and antagonist not in {d["npc"] for d in self.__dict__.get("defeated", [])}:
            role, _ = self.role_of(antagonist)
            self.__dict__.setdefault("defeated", []).append(
                {"npc": antagonist, "role": role, "turn": self.actions_taken, "climax": True})
        self._beat("climax", npc=antagonist, won=bool(succeeded), attempts=state["attempts"] + (1 if succeeded else 0))
        self._resolve_open_threads(int(cfg.get("revelations", 2)))
        self.tags_seen.append(["milestone", "success" if succeeded else "risk"])

    def _resolve_open_threads(self, limit=2):
        """At the climax what is still open comes out: each open clue closes, the first few
        told as revelations ([REVELATION] ...), and planted twists pay off."""
        notes = self.__dict__.get("clue_notes", {})
        told = 0
        for tag in list(self._open_clues()):
            if self.setup_indices.get(tag, self.actions_taken) >= self.actions_taken:
                continue  # planted this very turn: nothing to reveal yet
            payoff = "payoff_" + tag[len("setup_"):]
            if payoff in self.payoff_indices:
                payoff = f"{payoff}_climax"
            self.payoff_indices.setdefault(payoff, self.actions_taken)
            note = notes.get(tag)
            if note and told < limit and not tag.startswith("setup_twist"):
                self._say("revelation", text=_short(note["text"]), place=note["place"])
                self._beat("revelation", place=note["place"])
                told += 1
        for thread in self.__dict__.get("twist_threads", []):
            if not thread["resolved"] and f"twist{thread['id']}" in self.__dict__.get("setups_planted", set()):
                self._pay_off_twist(thread)

    # ---- failures as beats ---------------------------------------------------------------------

    def _another_way(self, action):
        """A failure no complication followed may show another way (director.another_way, drawn
        from the story's own randomness): a witness here, someone who matters named, or a way
        toward the quest. False (a dead end, logged) when it does not."""
        director = self._director() or {}
        chance = float(director.get("another_way", 0.0))
        if chance <= 0 or self._story_draw("another_way", self.actions_taken) >= chance:
            self._beat("dead_end", action=action.name)
            return False
        hidden = [c for c in self.current_inhabitants if c.name not in self.visible_inhabitants
                  and not self._is_removed(c.name) and c.name != action.character_name]
        if hidden:
            char = sorted(hidden, key=lambda c: (-self.importance(c.name), c.name))[0]
            self.visible_inhabitants.add(char.name)
            self._say("another_way_witness", npc=_cap(self._called(char.name)))
            self._note_met(char.name)
            self._beat("another_way", via="witness", npc=char.name)
            return True
        if action.character_name and self._name_hidden_npc(action.character_name):
            self._beat("another_way", via="named")
            return True
        sites = sorted(site for site in self._goal_sites() if site != self.current.name and site in self.world.locations)
        for site in sites:
            step = self._lead_toward(site)
            if step and step != self.current.name and step in self.world.locations:
                self.current.connect(self.world.locations[step])
                self._say("another_way_lead", location=site)
                self._beat("another_way", via="lead", location=site)
                return True
        self._beat("dead_end", action=action.name)
        return False

    def _refund(self, action, outcome):
        """A paid attempt that failed returns director.failure_refund of its price."""
        director = self._director() or {}
        share = float(director.get("failure_refund", 0.0))
        if share <= 0 or not action.required_coins or not rules.is_failure(outcome, set(rules.settings()["failure_tags"])):
            return 0
        refund = int(action.required_coins * share)
        if refund:
            self.coins += refund
            self._say("refund", coins=refund, balance=self.coins)
        return refund

    # ---- variation: what is told again is not told the same way ----------------------------------

    def _told(self, outcome, action=None):
        """The RESULT line of an outcome told for the n-th time: its text the first time, then
        its ``variants`` in turn; once those are used up (or without any), the line says it
        happened before (variation.repeat_lines; not for travel, or an action that starts an
        event). Also kept as the turn's ``told`` text."""
        count = self.outcome_counts[outcome.desc]
        variants = [outcome.desc] + [str(v) for v in getattr(outcome, "variants", None) or []]
        text = variants[(count - 1) % len(variants)]
        cfg = self._setting("variation") or {}
        key = "result"
        plain = action is not None and (action.source == "navigation" or action.triggers_event)
        if cfg.get("repeat_lines", True) and cfg and count > len(variants) and not plain:
            key = "result_again" if count - len(variants) == 1 else "result_habit"
        turn = self.__dict__.get("_turn")
        if turn is not None and "told" not in turn:
            turn["told"] = text
        return self.narrate(key, description=text)

    def _repeat_penalty(self, action):
        """What worked before is harder now: variation.repeat_check_penalty per earlier
        success of the same action (up to max_repeat_penalty); people catch on."""
        cfg = self._setting("variation")
        if cfg is None or action is None or action.source in ("navigation", "conversation", "event"):
            return 0
        done = self.__dict__.get("success_counts", {}).get(action.name, 0)
        return min(int(cfg.get("max_repeat_penalty", 20)), done * int(cfg.get("repeat_check_penalty", 0)))

    # ---- travel: part of the story -------------------------------------------------------------

    def _known_path(self, origin, target):
        """The way from ``origin`` to ``target`` through places already visited, along paths
        known on the map (no cell on the way): the places after the origin, or None."""
        visited = {name for name, count in self.location_counts.items() if count}
        constraints = config.get("location_constraints", {})
        previous, frontier = {origin: None}, [origin]
        while frontier:
            following = []
            for here in frontier:
                location = self.world.locations.get(here)
                for step in sorted(location.connected if location is not None else ()):
                    if step in previous or step not in visited or step not in self.world.locations:
                        continue
                    if not self._adjacent(here, step) or not self.is_enabled(self.world.locations[step]):
                        continue
                    if self.sent_only(step) or (constraints.get(step) or {}).get("requires_escape") \
                            or getattr(self.world.locations[step], "exit_locked", False):
                        continue
                    previous[step] = here
                    if step == target:
                        path = [step]
                        while previous[path[-1]] != origin:
                            path.append(previous[path[-1]])
                        return path[::-1]
                    following.append(step)
            frontier = following
        return None

    def _fast_travel_options(self, adjacent):
        """Travel in one turn to a place visited before, two or more steps away along known
        ways, that still holds something (travel.fast_travel; at most fast_travel_options, and
        not within fast_travel_cooldown turns of the last). Such a turn does not count toward
        the action limit."""
        cfg = config.get("travel") or {}
        if not cfg.get("fast_travel", False) or self._location_is_locked() or self.active_event is not None:
            return []
        cooldown = max(1, int(cfg.get("fast_travel_cooldown", 3)))
        if any(turn.get("fast_travel") for turn in (self.__dict__.get("turn_log") or [])[-cooldown:]):
            return []  # not again so soon: travel is part of the story, not a way to skip it
        goal_sites = self._goal_sites()
        candidates = []
        for name, count in self.location_counts.items():
            if not count or name == self.current.name or name in adjacent or self.sent_only(name):
                continue
            if name not in self.world.locations or not self.is_enabled(self.world.locations[name]):
                continue
            promise = 2 * (name in goal_sites) + int(bool(self._place_promise(name)))
            if not promise:
                continue
            path = self._known_path(self.current.name, name)
            if path and len(path) >= 2:
                candidates.append((-promise, len(path), name, path))
        options = []
        for _, _, name, path in sorted(candidates)[:max(0, int(cfg.get("fast_travel_options", 2)))]:
            via = ", the ".join(path[:-1])
            action = Action(self.narrate("travel_fast", stable=name, location=name), [Outcome(
                desc=self.narrate("travel_fast_result", stable=name, location=name, via=via), move_to=name)],
                repeat="always", source="navigation", owner=name)
            action.fast_travel = list(path)
            options.append(action)
        return options

    def _fast_travel(self, path):
        """Walk a known way in one turn: every place on it is passed through."""
        moved = False
        for step in path:
            if not self._move_to(step, cause="fast"):
                break
            moved = True
        return moved

    def _first_trip(self, place):
        """A first trip somewhere is part of the story (travel.first_trip): a scene on the way,
        chosen by the destination's tags (with chance), sometimes with a small gain or cost, and
        word on the road of someone who matters to be found there."""
        trip = (config.get("travel") or {}).get("first_trip") or {}
        if not trip.get("enabled", False) or place not in self.world.locations:
            return
        location = self.world.locations[place]
        if self._story_draw("first_trip", place) < float(trip.get("chance", 0.5)):
            pool = []
            for tag in sorted(str(t).lower() for t in location.tags):
                pool.extend((trip.get("by_tag") or {}).get(tag) or [])
            pool = pool or list(trip.get("default") or [])
            if pool:
                entry = pool[int(self._story_draw("first_trip_scene", place) * len(pool))]
                entry = entry if isinstance(entry, dict) else {"text": entry}
                self._say("on_the_way", text=str(entry.get("text", "")).format(location=place))
                coins, health = int(entry.get("coins", 0)), int(entry.get("health", 0))
                if coins or health:
                    self.coins = max(0, self.coins + coins)
                    health = self.health_player.change(health)
                    self.update_power()
                    self.history.append(self.narrate("unexpected_effect", health=health, coins=coins))
                self._beat("on_the_way", place=place)
        if trip.get("road_word", True):
            met = self.__dict__.get("met", {})
            who = [name for name, home in self.npc_homes.items() if home == place and name not in met
                   and self.importance(name) >= 1 and not self._is_removed(name)]
            if who:
                name = sorted(who, key=lambda n: (-self.importance(n), n))[0]
                self.__dict__.setdefault("revealed_npcs", set()).add(name)
                self._say("road_word", npc=_cap(self._called(name)), location=place)
                self._beat("reveal", npc=name, via="road")

    # ---- stakes: the alert level, and what a failure costs -----------------------------------

    def _alert_cfg(self):
        cfg = (config.get("stakes") or {}).get("alert") or {}
        return cfg if cfg.get("enabled", False) else None

    def _update_alert(self, action, outcome, succeeded):
        """Crimes and failures in watched places raise the alert (stakes.alert); quiet turns let
        it fall. At the top a manhunt starts and the alert drops back to reset_to."""
        cfg = self._alert_cfg()
        if cfg is None or action is None or action.source in ("navigation", "conversation"):
            return
        watched = {str(t).lower() for t in cfg.get("watched_tags") or []}
        here = {str(t).lower() for t in self.current.tags}
        level = int(self.__dict__.get("alert", 0))
        top = int(cfg.get("max", 5))
        rise = 0
        if not watched or watched & here or (outcome is not None and "guards" in {str(t).lower() for t in outcome.tags}):
            tags = {str(t).lower() for t in (outcome.tags if outcome is not None else [])}
            words = set(re.findall(r"[a-z]+", action.name.lower()))
            table = cfg.get("raise_on_success" if succeeded else "raise_on_failure") or {}
            rise = max([int(amount) for key, amount in table.items() if str(key).lower() in tags | words] or [0])
        if rise:
            self.alert = min(top, level + rise)
            self._alert_quiet = 0
            if self.alert >= top:
                event = cfg.get("manhunt_event")
                self.alert = int(cfg.get("reset_to", 1))
                self._say("manhunt", location=self.current.name)
                self._beat("manhunt")
                if event and self._pending_event is None and event in self.world.events:
                    self._pending_event = (event, None)
            elif self.alert >= int(cfg.get("warn_at", 3)):
                texts = cfg.get("warnings") or ["The watch is on edge."]
                self._say("alert_up", text=texts[min(len(texts) - 1, self.alert - int(cfg.get("warn_at", 3)))],
                          level=self.alert, max=top)
                self._beat("alert", level=self.alert)
            return
        quiet = int(self.__dict__.get("_alert_quiet", 0)) + 1
        self._alert_quiet = quiet
        if level and quiet >= int(cfg.get("decay_turns", 4)):
            self.alert, self._alert_quiet = level - 1, 0
            if level >= int(cfg.get("warn_at", 3)) > self.alert:
                self._say("alert_calm", level=self.alert, max=top)

    def _alert_check_penalty(self, action, tags=()):
        """A watch on edge makes guarded checks harder: stakes.alert.check_penalty per level."""
        cfg = self._alert_cfg()
        level = int(self.__dict__.get("alert", 0))
        if cfg is None or not level or action is None:
            return 0
        opponent = self._opponent(action) or ""
        guarded = "guard" in opponent.lower() or "guard" in action.name.lower() or "guards" in {str(t).lower() for t in tags}
        return level * int(cfg.get("check_penalty", 0)) if guarded else 0

    def stakes(self):
        """What is at stake now, for the player: {alert, max, warn_at} (None: alert off)."""
        cfg = self._alert_cfg()
        if cfg is None:
            return None
        return {"alert": int(self.__dict__.get("alert", 0)), "max": int(cfg.get("max", 5)),
                "warn_at": int(cfg.get("warn_at", 3))}

    def failure_cost(self, action):
        """What failing an action costs, shown before choosing (stakes.show_failure_costs):
        {health, coins, sent_to} (health and coins as changes, sent_to: where a failure takes
        the player); None when nothing is lost."""
        if not (config.get("stakes") or {}).get("show_failure_costs", True):
            return None
        failure_tags = set(rules.settings()["failure_tags"])
        failures = [o for o in action.outcomes if rules.is_failure(o, failure_tags)]
        cost = {"health": 0, "coins": 0, "sent_to": None}
        if failures:
            cost["health"] = min(0, min(o.health_change for o in failures))
            cost["coins"] = min(0, min(o.coin_change for o in failures))
            cost["sent_to"] = next((o.send_to for o in failures if o.send_to), None)
            if action.required_coins:
                share = float((self._director() or {}).get("failure_refund", 0.0))
                cost["coins"] -= action.required_coins - int(action.required_coins * share)
        else:
            attempt = next((o for o in action.outcomes if not rules.is_failure(o, failure_tags)), None)
            guard = attempt is not None and self._is_guard_failure(action, attempt)
            dangerous = guard or bool(attempt is not None and {str(t).lower() for t in attempt.tags} & {"combat", "threat"})
            cost["health"] = -self._scaled_damage(self.combat.failure_penalty(dangerous, self.inventory))
            if guard:
                cost["sent_to"] = config.get("game_state.arrest_location", "Dungeon")
        if not cost["health"] and not cost["coins"] and not cost["sent_to"]:
            return None
        return cost

    # ---- reveals: secrets, consequences, last time ------------------------------------------

    def _check_secret(self, npc):
        """An NPC's secret comes out once the player has spoken with them `talks` times and
        learned `clues` things: [SECRET] its text, its effects, and the actions it unlocks
        (enabled_if: {secret_known: <npc>})."""
        char = next((c for c in self.world.characters if c.name == npc), None)
        secret = getattr(char, "secret", None) if char is not None else None
        known = self.__dict__.setdefault("revealed_secrets", {})
        if not secret or npc in known or not config.get("npc_roles.secrets", True):
            return False
        if self.character_interactions.get(npc, 0) < int(secret.get("talks", 2)) or self._knowledge() < int(secret.get("clues", 0)):
            return False
        known[npc] = self.actions_taken
        self._say("secret", text=str(secret["text"]).format(npc=_cap(self._called(npc))))
        self.tags_seen.append(["discovery", "unexpected"])
        self._beat("secret", npc=npc)
        if secret.get("effects"):
            self._run_effects(secret["effects"], npc=npc)
        return True

    def _queue_consequence(self, kind, npc):
        """Something the player did shows later (consequences.<kind>): due after its delay, at a
        place sharing a tag with the NPC or anywhere else than where it happened."""
        cfg = self._setting("consequences")
        if cfg is None or not (cfg.get("kinds") or {}).get(kind):
            return
        char = next((c for c in self.world.characters if c.name == npc), None)
        spec = cfg["kinds"][kind]
        self.__dict__.setdefault("consequences_due", []).append({
            "kind": kind, "npc": npc, "from": self.current.name,
            "due": self.actions_taken + int(spec.get("delay", cfg.get("delay", 2))),
            "tags": sorted(str(t).lower() for t in (char.associated_tags if char is not None else ())),
        })

    def _tell_consequences(self):
        """Tell the consequences that are due here ([CONSEQUENCE] ...), with their effects."""
        cfg = self._setting("consequences")
        due = self.__dict__.get("consequences_due") or []
        if cfg is None or not due or self.active_event is not None or self.ended:
            return
        here = {str(t).lower() for t in self.current.tags}
        for item in list(due):
            if self.actions_taken < item["due"]:
                continue
            if not (here & set(item["tags"]) or self.current.name != item["from"]
                    or self.actions_taken >= item["due"] + int(cfg.get("overdue", 3))):
                continue
            due.remove(item)
            spec = cfg["kinds"][item["kind"]]
            texts = spec.get("texts") or [spec.get("text", "")]
            text = texts[int(self._story_draw("consequence", item["npc"], item["kind"]) * len(texts))]
            self._say("consequence", text=str(text).format(npc=_cap(self._called(item["npc"])), location=self.current.name))
            coins, health = int(spec.get("coins", 0)), int(spec.get("health", 0))
            if coins or health:
                self.coins = max(0, self.coins + coins)
                health = self.health_player.change(health)
                self.update_power()
                self.history.append(self.narrate("unexpected_effect", health=health, coins=coins))
            if spec.get("alert"):
                self.alert = max(0, int(self.__dict__.get("alert", 0)) + int(spec["alert"]))
            self._beat("consequence", npc=item["npc"], what=item["kind"])
            break  # one a turn

    def _recall_last_time(self):
        """A player's next story remembers the last one (memory, real play only: a profile's
        store): [LAST TIME] what happened, told at the start."""
        cfg = self._setting("memory")
        if cfg is None or self._store is None or not hasattr(self._store, "recent_memories"):
            return
        memories = self._store.recent_memories(self.profile)
        if memories:
            self._say("last_time", text=memories[-1])
        people = self._store.remembered_people(self.profile)
        self._remembered = {"friends": set(people.get("friends", [])), "foes": set(people.get("foes", []))}

    def _recognised(self, name):
        """Someone from the player's last stories recognises them ([LAST TIME] ...)."""
        remembered = self.__dict__.get("_remembered")
        if not remembered:
            return
        if name in remembered["friends"]:
            self._say("remembered_friend", npc=_cap(self._called(name)))
            self._beat("recognised", npc=name)
        elif name in remembered["foes"]:
            self._say("remembered_foe", npc=_cap(self._called(name)))
            self._beat("recognised", npc=name)

    def _remember_story(self):
        """At the end, what this story was, in a few words, for the profile's next one."""
        cfg = self._setting("memory")
        if cfg is None or self._store is None or not hasattr(self._store, "record_memory"):
            return
        place = self.current.name if self.current is not None else self.start_location
        title = self.mission.title if self.mission else None
        foes = [d["npc"] for d in self.__dict__.get("defeated", [])]
        if self.end_kind == "death":
            text = f"fell in the {place}" + (f" trying to {title[:1].lower() + title[1:]}" if title else "")
        elif self.won and title:
            text = f"completed \"{title}\" in the {place}" + (f", beating {self._called(foes[-1])}" if foes else "")
        else:
            text = (f"left \"{title}\" unfinished" if title else "wandered") + f" in the {place}"
        allies = sorted(self.__dict__.get("allies", ()))
        if allies:
            text += f"; {self._called(allies[0])} became a friend"
        self._store.record_memory(self.profile, text)
        self._store.record_people(self.profile, allies, foes)


def failure_lint(world):
    """The failures a world leaves as dead ends: [{action, owner, why}] for every action that can
    fail (a skill check, or failure outcomes) where failing changes nothing but health and coins
    and no complication event (tagged `complication`) shares a tag with it. The engine still
    shows another way at play time (director.another_way); this lists what to write."""
    failure_tags = set(rules.settings()["failure_tags"])
    complications = [set(event.tags) - {"complication"} for event in world.events.values() if "complication" in event.tags]
    owners = [(loc.name, loc.actions) for loc in world.locations.values()] + \
             [(char.name, char.actions) for char in world.characters] + \
             [(obj.name, obj.actions) for obj in world.objects]
    found = []
    for owner, actions in owners:
        for action in actions:
            failures = [o for o in action.outcomes if rules.is_failure(o, failure_tags)]
            if not action.skill_check and not failures:
                continue
            leads_on = any(o.move_to or o.send_to or o.spawn or o.reveal_location or o.reveal_npc or o.reveal_object
                           or o.setup_id or getattr(o, "return_to_previous", False) or list(o.effects.writes)
                           for o in failures)
            if leads_on:
                continue
            tags = {str(t).lower() for o in action.outcomes for t in o.tags}
            if any(tags & {str(t).lower() for t in event_tags} for event_tags in complications):
                continue
            why = ("a failed check tells nothing" if not failures
                   else "failing only costs health or coins")
            found.append({"action": action.name, "owner": owner, "why": why})
    return found
