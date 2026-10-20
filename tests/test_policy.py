"""Policy contracts: dynamic tiers, once-per-tier, resend, leaf mode and refresh timing."""

import tempfile
import time
import unittest
from pathlib import Path

from core import config, deadline, events, handoffs, policy
from core.observation import Decision, Observation, ToolUse


class PolicyFixture(unittest.TestCase):
    """Default configuration under a temporary home, one session's state and a fixed clock."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.config = config.load_config(config.sources_for("test", self.home, None), {})
        self.store = config.handoff_dir(self.config, self.home)
        self.state = {}
        self.now = 1_000_000.0

    def ctx(self, **changes: str) -> policy.Context:
        return policy.Context(self.config, self.store, self.home, changes.get("policy_text", ""), self.now)

    def observe(
        self,
        event: str,
        tokens: int | None = None,
        window: int | None = None,
        prompt: str | None = None,
        **fields: object,
    ) -> Observation:
        return Observation(
            "test",
            "session-1",
            event,
            tokens=tokens,
            window=window,
            prompt=prompt,
            **fields,
        )

    def prompt(self, text: str, tokens: int | None, window: int | None = None, **fields: object) -> Decision:
        return policy.decide(
            self.observe("prompt", tokens, window, text, **fields),
            self.state,
            self.ctx(),
        )

    def stop(self, tokens: int | None, window: int | None = None) -> Decision:
        return policy.decide(self.observe("stop", tokens, window), self.state, self.ctx())

    def names(self, decision: Decision) -> list[str]:
        return [entry["event"] for entry in decision.events]


class ThresholdTests(PolicyFixture):
    """Tier thresholds scale with the window below the configured caps."""

    def test_tiers_follow_window_fraction_below_caps(self) -> None:
        self.assertEqual(
            policy.thresholds(self.config, 200_000),
            policy.Thresholds(110_000, 150_000, 184_000),
        )
        self.assertEqual(
            policy.thresholds(self.config, 1_000_000),
            policy.Thresholds(150_000, 200_000, 260_000),
        )

    def test_caps_apply_when_window_is_unknown(self) -> None:
        self.assertEqual(
            policy.thresholds(self.config, None),
            policy.Thresholds(150_000, 200_000, 260_000),
        )
        self.assertEqual(self.prompt("a", 149_999).kind, "pass")
        self.assertEqual(self.prompt("b", 150_000).kind, "note")


class TierTests(PolicyFixture):
    """Main-session tiers fire once in order, block with a resend path and rearm."""

    def test_each_tier_fires_once_in_order(self) -> None:
        kinds = [
            (self.prompt(f"p{tokens}", tokens, 200_000).kind, self.state.get("tier"))
            for tokens in (
                100_000,
                120_000,
                130_000,
                160_000,
                170_000,
                190_000,
                195_000,
            )
        ]
        self.assertEqual(
            kinds,
            [
                ("pass", None),
                ("note", "note"),
                ("pass", "note"),
                ("note", "firm"),
                ("pass", "firm"),
                ("block", "block"),
                ("pass", "block"),
            ],
        )

    def test_jumping_past_tiers_fires_only_the_highest(self) -> None:
        self.assertEqual(self.prompt("big", 300_000).tier, "block")
        self.assertEqual(self.prompt("next", 310_000).kind, "pass")

    def test_block_states_tokens_limit_and_resend_instruction(self) -> None:
        decision = self.prompt("deploy it", 190_000, 200_000)
        self.assertEqual(decision.kind, "block")
        self.assertIn("190k", decision.message)
        self.assertIn("200k", decision.message)
        self.assertEqual(self.names(decision), ["prompt", "block"])

    def test_identical_resend_passes_and_silence_follows(self) -> None:
        self.prompt("deploy it", 270_000)
        resent = self.prompt(" deploy it ", 270_100)
        self.assertEqual(resent.kind, "pass")
        self.assertEqual(self.names(resent), ["prompt", "resend"])
        self.assertEqual(self.prompt("another request", 271_000).kind, "pass")

    def test_session_state_keeps_no_blocked_or_label_prompt_text(self) -> None:
        self.prompt("Deploy the payroll service with secret flags", 270_000)
        self.assertNotIn("payroll service", str(self.state))
        self.assertEqual(self.prompt("Deploy the payroll service with secret flags", 270_100).kind, "pass")

    def test_tool_events_note_but_never_block(self) -> None:
        decision = policy.decide(self.observe("tool", 300_000), self.state, self.ctx())
        self.assertEqual((decision.kind, decision.tier), ("note", "firm"))
        self.assertEqual(self.prompt("next", 300_000).kind, "block")

    def test_compaction_resets_fired_tiers(self) -> None:
        self.prompt("a", 160_000)
        self.assertEqual(self.prompt("b", 165_000).kind, "pass")
        self.assertEqual(self.prompt("c", 155_000, compacted=True).kind, "note")

    def test_tiers_rearm_when_context_drops_below_the_first_tier(self) -> None:
        self.assertEqual(self.prompt("a", 160_000, 200_000).tier, "firm")
        self.assertEqual(self.prompt("b", 100_000, 200_000).kind, "pass")
        self.assertNotIn("tier", self.state)
        self.assertEqual(self.prompt("c", 120_000, 200_000).tier, "note")
        self.assertEqual(self.prompt("d", None, 200_000).kind, "pass")
        self.assertEqual(self.state["tier"], "note")

    def test_zero_tokens_are_unknown_and_do_not_rearm(self) -> None:
        self.assertEqual(self.prompt("a", 160_000, 200_000).tier, "firm")
        self.assertEqual(self.prompt("b", 0, 200_000).kind, "pass")
        self.assertEqual(self.state["tier"], "firm")
        self.assertEqual(self.prompt("c", 160_000, 200_000).kind, "pass")

    def test_bookkeeping_and_handoff_prompts_never_warn(self) -> None:
        self.assertEqual(self.prompt("[context-guard] refresh", 300_000), Decision("pass"))
        decision = self.prompt("please write a handoff", 300_000)
        self.assertEqual(decision.kind, "pass")
        self.assertEqual(self.names(decision), ["prompt", "handoff_requested"])

    def test_handoff_requests_are_recognised_by_phrasing(self) -> None:
        for prompt in ("/handoff", "Handoff please", "update the handoff", "let's wrap it up", "start a new chat"):
            with self.subTest(prompt=prompt):
                self.assertTrue(handoffs.HANDOFF_REQUEST.search(prompt))

    def test_prompts_that_only_mention_handoffs_still_warn(self) -> None:
        decision = self.prompt("did you receive any handoffs on this chat? or wrote any?", 300_000)
        self.assertEqual(decision.tier, "block")
        self.assertNotIn("handoff_requested", self.names(decision))
        self.assertIsNone(handoffs.HANDOFF_REQUEST.search("check what's the most recent handoff"))
        self.assertIsNone(handoffs.HANDOFF_REQUEST.search("is this a new chat?"))


class LeafTests(PolicyFixture):
    """Subagents get one budget message instead of the policy, listing and tier notes."""

    def test_subagents_get_no_policy_listing_or_tier_notes(self) -> None:
        self.store.mkdir(parents=True)
        (self.store / "task-0123456789abcdef.md").write_text("# Task\nStatus: open\n")
        session = self.observe("session", is_subagent=True, cwd=str(self.home))
        self.assertEqual(
            policy.decide(session, self.state, self.ctx(policy_text="POLICY")).kind,
            "pass",
        )
        self.assertEqual(self.prompt("go", 79_999, is_subagent=True).kind, "pass")
        self.assertEqual(
            policy.decide(self.observe("stop", 300_000, is_subagent=True), self.state, self.ctx()).kind,
            "pass",
        )

    def test_budget_message_fires_once(self) -> None:
        tool = self.observe("tool", 80_000, is_subagent=True)
        first = policy.decide(tool, self.state, self.ctx())
        self.assertEqual(first.kind, "note")
        self.assertEqual(first.message, policy.BUDGET_MESSAGE.format(tokens=80, budget=80))
        self.assertEqual(self.names(first), ["subagent_budget"])
        self.assertEqual(first.events[0]["detail"], {"role": None, "budget": 80_000})
        self.assertEqual(policy.decide(tool, self.state, self.ctx()).kind, "pass")

    def test_role_budgets_come_from_roles_json_and_fall_back_to_the_global_budget(self) -> None:
        for role, below, at in (
            ("reviewer", 119_999, 120_000),
            ("runner", 59_999, 60_000),
            ("implementer", 79_999, 80_000),
            ("not-a-role", 79_999, 80_000),
        ):
            with self.subTest(role=role):
                self.state = {}
                self.assertEqual(self.prompt("go", below, is_subagent=True, role=role).kind, "pass")
                decision = self.prompt("go", at, is_subagent=True, role=role)
                self.assertEqual(decision.kind, "note")
                self.assertEqual(decision.events[0]["detail"], {"role": role, "budget": at})

    def test_unusable_role_budgets_fall_back(self) -> None:
        content = self.home / "content"
        (content / "roles").mkdir(parents=True)
        (content / "roles" / "roles.json").write_text(
            '{"roles": {"runner": {"budget_tokens": "big"}, "scout": {"budget_tokens": 0}}}'
        )
        for role in ("runner", "scout"):
            self.assertEqual(policy.role_budget(self.config, role, content), 80_000)
        self.assertEqual(policy.role_budget(self.config, "runner", self.home / "missing"), 80_000)

    def test_main_session_gets_policy_and_listing(self) -> None:
        session = self.observe("session", cwd=str(self.home))
        decision = policy.decide(session, self.state, self.ctx(policy_text="POLICY"))
        self.assertEqual((decision.kind, decision.message), ("context", "POLICY"))


class RefreshTests(PolicyFixture):
    """Stop-time refreshes follow unwritten handoffs, token growth and the throttle."""

    def write(self, tokens: int, name: str = "task-0123456789abcdef.md") -> Decision:
        self.store.mkdir(parents=True, exist_ok=True)
        tool = ToolUse("Write", "write", str(self.store / name))
        return policy.decide(self.observe("tool", tokens, tool=tool), self.state, self.ctx())

    def test_a_note_turn_without_a_handoff_write_gets_one_refresh_naming_the_file(self) -> None:
        self.assertEqual(self.prompt("a", 160_000).kind, "note")
        refresh = self.stop(165_000)
        self.assertEqual(refresh.kind, "context")
        self.assertTrue(refresh.message.startswith("[context-guard]"))
        self.assertIn(self.state["handoff_path"], refresh.message)
        self.assertEqual(refresh.events[0]["detail"], {"source": "unwritten"})
        self.assertEqual(self.stop(166_000).kind, "pass")

    def test_a_note_turn_with_a_handoff_write_gets_no_refresh(self) -> None:
        self.assertEqual(self.prompt("a", 160_000).kind, "note")
        self.write(161_000)
        self.assertEqual(self.stop(250_000).kind, "pass")

    def test_a_handoff_request_without_a_write_is_refreshed_and_with_one_is_not(self) -> None:
        self.prompt("start", 150_000)
        self.prompt("please write a handoff", 160_000)
        self.assertEqual(self.stop(160_000).events[0]["detail"], {"source": "unwritten"})
        self.prompt("handoff again", 161_000)
        self.write(161_000)
        self.assertEqual(self.stop(161_000).kind, "pass")

    def test_a_write_to_an_existing_task_handoff_is_credited_and_adopted(self) -> None:
        self.prompt("Ship the parser", 160_000)
        provisional = self.state["handoff_path"]
        existing = "parser-work-fedcba9876543210.md"
        self.assertEqual(self.names(self.write(162_000, existing)), ["handoff_written"])
        self.assertNotEqual(provisional, self.state["handoff_path"])
        self.assertEqual(self.state["handoff_path"], str(self.store / existing))
        self.assertEqual(self.state["handoff_at"], 162_000)
        self.assertEqual(self.stop(163_000).kind, "pass")

    def test_refresh_never_repeats_in_consecutive_turns_without_token_growth(self) -> None:
        self.state.update(tier="block", turn=1)
        self.assertEqual(self.stop(200_000).kind, "context")
        for turn in range(2, 5):
            self.state["turn"] = turn
            self.now += 400
            self.assertEqual(self.stop(200_000 + turn * 1000).kind, "pass")
        self.state["turn"] = 5
        self.assertEqual(self.stop(260_000).kind, "context")

    def test_refresh_needs_growth_and_respects_throttle(self) -> None:
        self.state.update(tier="block", turn=3)
        self.assertEqual(self.stop(100_000).kind, "pass")
        self.assertEqual(self.stop(200_000).kind, "context")
        self.state["turn"] = 4
        self.now += 10
        self.assertEqual(self.stop(270_000).kind, "pass")
        self.now += 400
        self.assertEqual(self.stop(270_000).kind, "context")

    def test_handoff_write_acknowledges_refresh_and_is_logged(self) -> None:
        self.state.update(tier="block", turn=1)
        self.stop(200_000)
        path = str(self.store / "task-0123456789abcdef.md")
        self.store.mkdir(parents=True)
        written = policy.decide(
            self.observe("tool", 201_000, tool=ToolUse("Write", "write", path)),
            self.state,
            self.ctx(),
        )
        self.assertEqual(self.names(written), ["handoff_written"])
        self.assertEqual(self.state["handoff_at"], 201_000)
        self.assertEqual(self.state["handoff_path"], path)
        self.state["turn"] = 2
        self.now += 400
        self.assertEqual(self.stop(230_000).kind, "pass")

    def test_unrequested_handoff_write_counts_as_a_fresh_handoff(self) -> None:
        self.state.update(tier="block", turn=1)
        self.store.mkdir(parents=True)
        tool = ToolUse("Write", "write", str(self.store / "task-0123456789abcdef.md"))
        policy.decide(self.observe("tool", 250_000, tool=tool), self.state, self.ctx())
        self.assertEqual(self.state["handoff_at"], 250_000)
        self.assertEqual(self.stop(280_000).kind, "pass")

    def test_handoff_paths_expand_against_the_explicit_home(self) -> None:
        self.state.update(tier="block", turn=1)
        tool = ToolUse("Write", "write", "~/.context-guard/handoffs/task-0123456789abcdef.md")
        written = policy.decide(self.observe("tool", 250_000, tool=tool), self.state, self.ctx())
        self.assertEqual(self.names(written), ["handoff_written"])


class EventTests(PolicyFixture):
    """Logged events, persisted state and deadline-aware evaluation."""

    def test_spawn_and_reads_are_logged_with_schema(self) -> None:
        spawn = ToolUse(
            "Task",
            "spawn",
            detail={"role": "runner", "tier": "cheap", "model": "m", "effort": "low"},
        )
        decision = policy.decide(self.observe("tool", 1000, 200_000, tool=spawn), self.state, self.ctx())
        [entry] = decision.events
        self.assertEqual(
            set(entry),
            {"ts", "runtime", "session", "event", "tokens", "window", "detail"},
        )
        self.assertEqual(
            entry["detail"],
            {"role": "runner", "tier": "cheap", "model": "m", "effort": "low"},
        )
        outside = ToolUse("Read", "read", str(self.home / "notes.md"))
        self.assertEqual(
            policy.decide(self.observe("tool", 1000, tool=outside), self.state, self.ctx()).events,
            [],
        )

    def test_evaluate_persists_state_and_appends_events(self) -> None:
        obs = self.observe("prompt", 270_000, prompt="ship")
        self.assertEqual(policy.evaluate(obs, self.config, self.home).kind, "block")
        self.assertEqual(policy.evaluate(obs, self.config, self.home).kind, "pass")
        log = config.state_dir(self.config, self.home, "test") / events.EVENTS_FILE
        self.assertEqual(
            [entry["event"] for entry in events.read(log)],
            ["prompt", "block", "prompt", "resend"],
        )

    def test_saved_decision_is_delivered_even_if_the_deadline_passes_afterwards(self) -> None:
        obs = self.observe("prompt", 270_000, prompt="ship")
        log = config.state_dir(self.config, self.home, "test") / events.EVENTS_FILE

        def work() -> Decision:
            decision = policy.evaluate(obs, self.config, self.home)
            time.sleep(1.2)
            return decision

        self.assertEqual(deadline.run_within(work, "fallback", seconds=1).kind, "block")
        self.assertEqual([entry["event"] for entry in events.read(log)], ["prompt", "block"])

    def test_state_is_not_saved_when_too_little_time_is_left_to_respond(self) -> None:
        obs = self.observe("prompt", 270_000, prompt="ship")
        directory = config.state_dir(self.config, self.home, "test")
        result = deadline.run_within(
            lambda: policy.evaluate(obs, self.config, self.home),
            "fallback",
            seconds=deadline.COMMIT_RESERVE_SECONDS / 2,
        )
        self.assertEqual(result, "fallback")
        self.assertEqual(list(directory.glob("*.json")), [])
        self.assertEqual(policy.evaluate(obs, self.config, self.home).kind, "block")

    def test_invalid_observations_raise_explicitly(self) -> None:
        for fields in (
            {"event": "unknown"},
            {"tokens": -1},
            {"window": "200k"},
            {"session_id": ""},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                Observation(
                    **{
                        "runtime": "test",
                        "session_id": "s",
                        "event": "prompt",
                        **fields,
                    }
                )


if __name__ == "__main__":
    unittest.main()
