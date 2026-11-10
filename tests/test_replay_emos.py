"""Golden tests on real EMOS traces (recorded 2026-11-10, see docs/03 §3.1)."""
import json
import os
import tempfile
import unittest

from embodied_infra.adapters.replay import load_profile, replay, split_and_goal

from helpers import EMOS_PROFILE, trace

PROFILE = load_profile(EMOS_PROFILE)


def findings(rt):
    return [(f.rule, f.step, f.agent_id) for f in rt.findings]


class ReplayEp0FailTest(unittest.TestCase):
    """Fetch+Stretch ep0: agent_1's pick fails silently, then an empty place,
    then both agents wait until the episode times out."""

    @classmethod
    def setUpClass(cls):
        cls.rt = replay(trace("emos_fetch_stretch_ep0_fail.jsonl"), PROFILE)

    def test_golden_findings(self):
        self.assertEqual(findings(self.rt), [
            ("outcome_failure", 285, "agent_1"),   # pick timed out, hand is empty
            ("precondition", 308, "agent_1"),      # place while holding nothing
            ("outcome_failure", 328, "agent_1"),   # the empty place did not achieve at(...)
            ("idle_deadlock", 329, "agent_0"),     # both idle, goal 1 unmet
            ("idle_deadlock", 329, "agent_1"),
        ])

    def test_gate_denies_empty_place(self):
        gates = [r for r in self.rt.records if r["type"] == "infra.gate_decision"]
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0]["data"]["action_id"], "agent_1:4")
        self.assertEqual(gates[0]["data"]["decision"]["decision"], "deny")
        self.assertIn("holding: nothing", gates[0]["data"]["decision"]["message"])

    def test_commands_and_cooldown(self):
        cmds = [(c.type, c.agent_id) for c in self.rt.adapter_log.commands]
        # 285 inject; 328 suppressed by cooldown (43 < 50 steps); 329 interrupt+inject x2
        self.assertEqual(cmds, [("inject_message", "agent_1"),
                                ("interrupt", "agent_0"), ("inject_message", "agent_0"),
                                ("interrupt", "agent_1"), ("inject_message", "agent_1")])

    def test_inferred_outcomes(self):
        out = {a: r.outcome for a, r in self.rt.world.actions.items()}
        self.assertEqual(out["agent_1:2"], "failure")   # pick (timeout)
        self.assertEqual(out["agent_0:4"], "success")   # place (timeout!) still succeeded
        self.assertEqual(out["agent_0:2"], "success")

    def test_world_state(self):
        w = self.rt.world
        self.assertTrue(w.ended)
        self.assertFalse(w.success)
        self.assertEqual(w.unmet_goal_conditions(), ["at(any_targets|1,TARGET_any_targets|1)"])
        self.assertEqual(len(self.rt.bus.errors), 0)


class ReplayNoFalsePositiveTest(unittest.TestCase):
    def test_success_trace_is_quiet(self):
        rt = replay(trace("emos_spot_drone_ep119_success.jsonl"), PROFILE)
        self.assertEqual(findings(rt), [])
        self.assertTrue(rt.world.success)
        self.assertEqual(len(rt.bus.errors), 0)

    def test_ep1_known_gap(self):
        # ep1 fails because reset_arm never terminates and agent_1 only waits.
        # No v1 rule covers a stalled action; documented in docs/03 §3.1.
        rt = replay(trace("emos_fetch_stretch_ep1_fail.jsonl"), PROFILE)
        self.assertEqual(findings(rt), [])
        self.assertEqual(len(rt.bus.errors), 0)

    def test_without_profile_rules_stay_silent(self):
        # Old traces lack semantics: infra must degrade (warn), not misfire.
        rt = replay(trace("emos_fetch_stretch_ep0_fail.jsonl"), None)
        self.assertEqual(findings(rt), [])
        self.assertTrue(any(r["type"] == "infra.warning" for r in rt.records))


class ReplaySendRequestDeadlockTest(unittest.TestCase):
    """Schema-0.2 trace (semantics emitted by the EMOS recorder, no profile needed):
    both agents send_request at step 0, EMOS maps that to wait(500) -> 500 idle steps;
    agent_0 repeats it at 664 while holding the object; episode times out at 750."""

    @classmethod
    def setUpClass(cls):
        cls.rt = replay(trace("emos_spot_drone_ep119_v02_sendrequest_deadlock.jsonl"), None)

    def test_detects_both_deadlocks_without_profile(self):
        self.assertEqual(findings(self.rt), [
            ("idle_deadlock", 0, "agent_0"), ("idle_deadlock", 0, "agent_1"),
            ("idle_deadlock", 664, "agent_0"), ("idle_deadlock", 664, "agent_1"),
        ])
        self.assertFalse(any(r["type"] == "infra.warning" for r in self.rt.records))

    def test_interrupts_issued(self):
        types = [(c.type, c.agent_id) for c in self.rt.adapter_log.commands]
        self.assertEqual(types.count(("interrupt", "agent_0")), 2)
        self.assertEqual(types.count(("interrupt", "agent_1")), 2)


class TraceFilesTest(unittest.TestCase):
    def test_input_trace_roundtrip(self):
        src = trace("emos_spot_drone_ep119_success.jsonl")
        with tempfile.TemporaryDirectory() as d:
            replay(src, None, {"enabled": True, "trace": {"enabled": True, "dir": d}})
            out = os.path.join(d, "episode_119.jsonl")
            self.assertTrue(os.path.exists(os.path.join(d, "episode_119.infra.jsonl")))
            with open(src) as fa, open(out) as fb:
                a = [json.loads(x) for x in fa]
                b = [json.loads(x) for x in fb]
            # replay marks every agent as supporting all controls; ignore that field
            for e in a + b:
                if e["type"] == "agent.registered":
                    e["data"].pop("supports", None)
            self.assertEqual(a, b)


class GoalSplitTest(unittest.TestCase):
    def test_split(self):
        self.assertEqual(split_and_goal("and(at(a,b),not_holding(agent_0))"),
                         ["at(a,b)", "not_holding(agent_0)"])
        self.assertIsNone(split_and_goal("or(a,b)"))


if __name__ == "__main__":
    unittest.main()
