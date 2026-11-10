"""Synthetic closed-loop scenarios for the runtime contract."""
import json
import unittest

from embodied_infra import (Command, ControlAck, EmasAdapter, InfraRuntime, NullInfraClient,
                            make_client)
from embodied_infra.supervisor.rules import Rule

from helpers import EventFactory, snapshot, start_episode


class FakeAdapter(EmasAdapter):
    def __init__(self):
        self.cmds = []

    def control(self, cmd: Command) -> ControlAck:
        self.cmds.append(cmd)
        return ControlAck(command_id=cmd.command_id, status="accepted")


def propose(rt, f, agent, n, name, args):
    e = f("action.proposed", {"action_id": f"{agent}:{n}", "name": name, "args": args},
          agent_id=agent, source="agent_report")
    rt.emit(e)
    return rt.gate(e)


class GateTest(unittest.TestCase):
    def setUp(self):
        self.rt = InfraRuntime({"enabled": True})
        self.ad = FakeAdapter()
        self.rt.attach(self.ad)
        self.f = EventFactory()
        start_episode(self.rt, self.f, {"agent_0": ["mobility", "manipulation"],
                                        "agent_1": ["mobility", "perception"]})
        self.rt.emit(snapshot(self.f, {"agent_0": None, "agent_1": None}))

    def test_unknown_entity_denied_always(self):
        for n in range(1, 10):  # never rate-limited: executing it would crash EMOS
            d = propose(self.rt, self.f, "agent_0", n, "pick", {"target_obj": "mug"})
            self.assertEqual(d.decision, "deny")
            self.assertIn("Valid object ids", d.message)
            self.rt.emit(self.f("action.ended", {"action_id": f"agent_0:{n}", "status": "rejected"},
                                agent_id="agent_0"))

    def test_unknown_action_denied(self):
        d = propose(self.rt, self.f, "agent_0", 1, "fly", {})
        self.assertEqual(d.decision, "deny")
        self.assertIn("unknown_action", d.findings)

    def test_capability_denied(self):
        d = propose(self.rt, self.f, "agent_1", 1, "pick", {"target_obj": "cup"})
        self.assertEqual(d.decision, "deny")
        self.assertEqual(d.findings, ["capability"])

    def test_valid_action_allowed(self):
        d = propose(self.rt, self.f, "agent_0", 1, "pick", {"target_obj": "cup"})
        self.assertEqual(d.decision, "allow")

    def test_precondition_uses_latest_snapshot(self):
        self.rt.emit(snapshot(self.f, {"agent_0": "cup", "agent_1": None}, step=5))
        d = propose(self.rt, self.f, "agent_0", 1, "pick", {"target_obj": "cup"})
        self.assertEqual(d.decision, "deny")      # already holding
        d = propose(self.rt, self.f, "agent_0", 2, "place",
                    {"target_obj": "cup", "target_location": "TARGET_cup"})
        self.assertEqual(d.decision, "allow")

    def test_budget_limits_precondition_denials(self):
        self.rt.policy.max_per_agent_episode = 2
        self.rt.emit(snapshot(self.f, {"agent_0": "cup", "agent_1": None}, step=5))
        decisions = []
        for n in range(1, 5):
            decisions.append(propose(self.rt, self.f, "agent_0", n, "pick", {"target_obj": "cup"}).decision)
            self.rt.emit(self.f("action.ended", {"action_id": f"agent_0:{n}", "status": "rejected"},
                                agent_id="agent_0"))
        self.assertEqual(decisions, ["deny", "deny", "allow", "allow"])


class AsyncTest(unittest.TestCase):
    def test_outcome_failure_inject_and_unsupported(self):
        rt = InfraRuntime({"enabled": True})
        ad = FakeAdapter()
        rt.attach(ad)
        f = EventFactory()
        start_episode(rt, f, {"agent_0": ["manipulation"]}, supports=())
        rt.emit(snapshot(f, {"agent_0": None}))
        propose(rt, f, "agent_0", 1, "pick", {"target_obj": "cup"})
        rt.emit(f("action.started", {"action_id": "agent_0:1", "name": "pick",
                                     "args": {"target_obj": "cup"}, "gate": "allow"}, agent_id="agent_0"))
        rt.emit(f("action.ended", {"action_id": "agent_0:1", "status": "timeout"},
                  agent_id="agent_0", step=100))
        rt.emit(snapshot(f, {"agent_0": None}, step=101))
        self.assertEqual(rt.world.actions["agent_0:1"].outcome, "failure")
        self.assertEqual([x.rule for x in rt.findings], ["outcome_failure"])
        # agent declared no supported controls -> command is not delivered
        self.assertEqual(ad.cmds, [])
        iv = [r for r in rt.records if r["type"] == "intervention.issued"]
        self.assertEqual(iv[0]["data"]["ack"]["status"], "unsupported")

    def test_repeat_failure(self):
        rt = InfraRuntime({"enabled": True, "interventions": {"cooldown_steps": 0}})
        rt.attach(FakeAdapter())
        f = EventFactory()
        start_episode(rt, f, {"agent_0": ["manipulation"]})
        rt.emit(snapshot(f, {"agent_0": None}))
        for n in (1, 2):
            propose(rt, f, "agent_0", n, "pick", {"target_obj": "cup"})
            rt.emit(f("action.started", {"action_id": f"agent_0:{n}", "name": "pick",
                                         "args": {"target_obj": "cup"}}, agent_id="agent_0"))
            rt.emit(f("action.ended", {"action_id": f"agent_0:{n}", "status": "timeout"},
                      agent_id="agent_0", step=n * 100))
            rt.emit(snapshot(f, {"agent_0": None}, step=n * 100 + 1))
        self.assertEqual([x.rule for x in rt.findings],
                         ["outcome_failure", "outcome_failure", "repeat_failure"])


class RobustnessTest(unittest.TestCase):
    def test_malformed_event_dropped(self):
        rt = InfraRuntime({"enabled": True})
        rt.emit({"no": "type"})       # must not raise
        rt.emit("garbage")            # must not raise
        self.assertEqual(rt.world.agents, {})

    def test_broken_rule_is_isolated(self):
        class Boom(Rule):
            name = "boom"

            def on_event(self, event, world):
                raise RuntimeError("boom")

        rt = InfraRuntime({"enabled": True})
        rt.rules.insert(0, Boom())
        f = EventFactory()
        start_episode(rt, f, {"agent_0": ["manipulation"]})
        self.assertIn("agent_0", rt.world.agents)        # world still updated
        self.assertTrue(rt.bus.errors)                    # failure recorded, not raised
        d = rt.gate(f("action.proposed", {"action_id": "agent_0:1", "name": "wait", "args": {}},
                      agent_id="agent_0"))
        self.assertEqual(d.decision, "allow")
        # the other rules still run after the broken one
        d = rt.gate(f("action.proposed", {"action_id": "agent_0:2", "name": "fly", "args": {}},
                      agent_id="agent_0"))
        self.assertEqual(d.decision, "deny")

    def test_null_client_and_factory(self):
        self.assertIsInstance(make_client({"enabled": False}), NullInfraClient)
        self.assertIsInstance(make_client({"infra": {"enabled": True}}), InfraRuntime)
        n = NullInfraClient()
        self.assertEqual(n.gate({}).decision, "allow")
        self.assertEqual(n.tool_schemas("a"), [])


class ToolsTest(unittest.TestCase):
    def test_schemas_and_calls(self):
        rt = InfraRuntime({"enabled": True})
        f = EventFactory()
        start_episode(rt, f, {"agent_0": ["manipulation"]})
        rt.emit(f("state.snapshot", {"agents": {"agent_0": {"pos": [0, 0, 0], "holding": "cup"}},
                                     "entities": {"cup": {"pos": [0.5, 0, 0]},
                                                  "TARGET_cup": {"pos": [9, 0, 0]}},
                                     "predicates": ["holding(cup,agent_0)"]}, source="sim_gt"))
        names = [s["function"]["name"] for s in rt.tool_schemas("agent_0")]
        self.assertEqual(names, ["world_get_entity", "world_find_entities",
                                 "world_get_agent_state", "memory_get_action_history"])
        for n in names:
            self.assertRegex(n, r"^[a-zA-Z0-9_]+$")
        cup = json.loads(rt.call_tool("agent_0", "world_get_entity", {"entity_id": "cup"}))
        self.assertEqual(cup["held_by"], "agent_0")
        near = json.loads(rt.call_tool("agent_0", "world_find_entities",
                                       {"near_agent": "agent_0", "radius": 1.0}))
        self.assertEqual([e["entity_id"] for e in near["entities"]], ["cup"])
        err = json.loads(rt.call_tool("agent_0", "world_get_entity", {"entity_id": "x"}))
        self.assertIn("error", err)
        self.assertTrue(any(r["type"] == "tool.called" for r in rt.records))


def start(rt, f, n, agent, name, args, step):
    rt.emit(f("action.proposed", {"action_id": f"{agent}:{n}", "name": name, "args": args},
              agent_id=agent, source="agent_report", step=step))
    rt.emit(f("action.started", {"action_id": f"{agent}:{n}", "name": name, "args": args},
              agent_id=agent, step=step))


def end(rt, f, n, agent, step, status="completed"):
    rt.emit(f("action.ended", {"action_id": f"{agent}:{n}", "status": status},
              agent_id=agent, step=step))


class StalledAndHelpTest(unittest.TestCase):
    def setUp(self):
        self.rt = InfraRuntime({"enabled": True})
        self.ad = FakeAdapter()
        self.rt.attach(self.ad)
        self.f = EventFactory()
        start_episode(self.rt, self.f, {"agent_0": ["mobility", "manipulation"],
                                        "agent_1": ["mobility", "manipulation"],
                                        "drone": ["mobility", "perception"]})
        self.rt.emit(snapshot(self.f, {"agent_0": None, "agent_1": None, "drone": None}))

    def cmds(self, agent=None):
        return [(c.type, c.agent_id) for c in self.ad.cmds if agent in (None, c.agent_id)]

    def test_stalled_nav_interrupted_once(self):
        start(self.rt, self.f, 1, "agent_0", "nav_to_obj", {"target_obj": "cup"}, 10)
        self.rt.emit(snapshot(self.f, {"agent_0": None}, step=300))     # 290 <= 300
        self.assertEqual(self.cmds("agent_0"), [])
        self.rt.emit(snapshot(self.f, {"agent_0": None}, step=320))
        self.assertEqual(self.cmds("agent_0"), [("interrupt", "agent_0"),
                                                ("inject_message", "agent_0")])
        self.rt.emit(snapshot(self.f, {"agent_0": None}, step=500))     # once per action
        self.assertEqual(len(self.cmds("agent_0")), 2)

    def test_actions_without_max_steps_or_idle_never_stall(self):
        start(self.rt, self.f, 1, "agent_0", "wait", {}, 0)
        start(self.rt, self.f, 1, "agent_1", "send_request", {}, 0)
        self.rt.emit(snapshot(self.f, {"agent_0": None}, step=5000))
        self.assertNotIn("stalled_action", [x.rule for x in self.rt.findings])

    def test_help_routed_to_idle_capable_teammate_only(self):
        start(self.rt, self.f, 1, "agent_1", "wait", {}, 0)    # idle, capable
        start(self.rt, self.f, 1, "drone", "wait", {}, 0)      # idle, lacks manipulation
        for n, s in ((1, 10), (2, 120)):
            start(self.rt, self.f, n, "agent_0", "pick", {"target_obj": "cup"}, s)
            end(self.rt, self.f, n, "agent_0", s + 99, "timeout")
            self.rt.emit(snapshot(self.f, {"agent_0": None}, step=s + 100))
        rules = [(x.rule, x.agent_id) for x in self.rt.findings]
        self.assertIn(("repeat_failure", "agent_0"), rules)
        self.assertIn(("help_routing", "agent_1"), rules)
        self.assertNotIn(("help_routing", "drone"), rules)
        self.assertIn(("interrupt", "agent_1"), self.cmds("agent_1"))
        msg = [c for c in self.ad.cmds if c.type == "inject_message" and c.agent_id == "agent_1"]
        self.assertIn("agent_0", msg[0].payload["text"])

    def test_busy_teammate_not_disturbed(self):
        start(self.rt, self.f, 1, "agent_1", "nav_to_obj", {"target_obj": "cup"}, 0)
        for n, s in ((1, 10), (2, 120)):
            start(self.rt, self.f, n, "agent_0", "pick", {"target_obj": "cup"}, s)
            end(self.rt, self.f, n, "agent_0", s + 99, "timeout")
            self.rt.emit(snapshot(self.f, {"agent_0": None}, step=s + 100))
        self.assertNotIn("help_routing", [x.rule for x in self.rt.findings])

    def test_capability_denial_routes_help(self):
        start(self.rt, self.f, 1, "agent_0", "wait", {}, 0)
        start(self.rt, self.f, 1, "agent_1", "nav_to_obj", {"target_obj": "cup"}, 0)  # busy
        d = propose(self.rt, self.f, "drone", 1, "pick", {"target_obj": "cup"})
        self.assertEqual(d.decision, "deny")
        rules = [(x.rule, x.agent_id) for x in self.rt.findings]
        self.assertIn(("help_routing", "agent_0"), rules)
        self.assertNotIn(("help_routing", "agent_1"), rules)


class IdleDeadlockRepeatTest(unittest.TestCase):
    def test_same_unmet_state_alarms_at_most_twice(self):
        rt = InfraRuntime({"enabled": True, "interventions": {"cooldown_steps": 0,
                                                               "max_per_agent_episode": 99}})
        ad = FakeAdapter(); rt.attach(ad); f = EventFactory()
        start_episode(rt, f, {"agent_0": ["mobility"], "agent_1": ["mobility"]})
        rt.emit(snapshot(f, {"agent_0": None, "agent_1": None}))
        n = 0
        for k in range(5):
            for a in ("agent_0", "agent_1"):
                n += 1
                start(rt, f, n, a, "wait", {}, 100 * k)
            for a in ("agent_0", "agent_1"):
                end(rt, f, n - (a == "agent_0"), a, 100 * k + 50, "interrupted")
        alarms = [x for x in rt.findings if x.rule == "idle_deadlock"]
        self.assertEqual(len(alarms), 4)  # 2 alarms x 2 agents


if __name__ == "__main__":
    unittest.main()
