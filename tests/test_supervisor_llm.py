"""Supervisor layers (safety / correction / assist) and the LLM escalation path.

The LLM is a scripted fake: no network.
"""
import tempfile
import unittest

from embodied_infra import Command, ControlAck, EmasAdapter, InfraRuntime
from embodied_infra.supervisor.llm import resolve_env

from helpers import EventFactory, snapshot, start_episode


class FakeAdapter(EmasAdapter):
    def __init__(self):
        self.cmds = []

    def control(self, cmd: Command) -> ControlAck:
        self.cmds.append(cmd)
        return ControlAck(command_id=cmd.command_id, status="accepted")


class FakeLLM:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages, tools):
        self.calls.append(messages)
        r = self.replies.pop(0) if self.replies else []
        if isinstance(r, Exception):
            raise r
        return r


def runtime(llm=None, **sup):
    cfg = {"enabled": True, "supervisor": {"llm": {"enabled": llm is not None,
                                                   "min_interval_steps": 0}, **sup}}
    rt = InfraRuntime(cfg, llm_chat=llm)
    ad = FakeAdapter()
    rt.attach(ad)
    return rt, ad


def act(rt, f, n, agent, name, args, s, e=None, status="completed"):
    aid = f"{agent}:{n}"
    rt.emit(f("action.proposed", {"action_id": aid, "name": name, "args": args},
              agent_id=agent, source="agent_report", step=s))
    rt.gate(f("action.proposed", {"action_id": aid, "name": name, "args": args},
              agent_id=agent, step=s))
    rt.emit(f("action.started", {"action_id": aid, "name": name, "args": args},
              agent_id=agent, step=s))
    if e is not None:
        rt.emit(f("action.ended", {"action_id": aid, "status": status}, agent_id=agent, step=e))


def two_pick_failures(rt, f, agent="agent_0"):
    for n, s in ((1, 10), (2, 120)):
        act(rt, f, n, agent, "pick", {"target_obj": "cup"}, s, s + 99, "timeout")
        rt.emit(snapshot(f, {agent: None}, step=s + 100))


TAGS = {"agent_0": ["mobility", "manipulation"], "agent_1": ["mobility", "manipulation"]}


class LayersTest(unittest.TestCase):
    def test_supervisor_off_keeps_only_safety(self):
        rt = InfraRuntime({"enabled": True, "supervisor": {"enabled": False}})
        rt.attach(FakeAdapter())
        self.assertEqual([r.name for r in rt.rules], ["unknown_action", "unknown_entity"])
        f = EventFactory()
        start_episode(rt, f, TAGS)
        rt.emit(snapshot(f, {"agent_0": None, "agent_1": None}))
        two_pick_failures(rt, f)
        self.assertNotIn("outcome_failure", [x.rule for x in rt.findings])

    def test_safety_denies_when_supervisor_off(self):
        rt = InfraRuntime({"enabled": True, "supervisor": {"enabled": False}})
        f = EventFactory()
        start_episode(rt, f, TAGS)
        e = f("action.proposed", {"action_id": "agent_0:1", "name": "pick",
                                  "args": {"target_obj": "mug"}}, agent_id="agent_0")
        rt.emit(e)
        self.assertEqual(rt.gate(e).decision, "deny")

    def test_assist_toggles(self):
        rt, _ = runtime(assist={"workload_balance": True, "first_failure_help": True})
        names = [r.name for r in rt.rules]
        self.assertIn("workload_risk", names)
        help_rule = [r for r in rt.rules if r.name == "help_routing"][0]
        self.assertTrue(help_rule.on_first_failure)
        rt, _ = runtime()
        self.assertNotIn("workload_risk", [r.name for r in rt.rules])


class EscalationTest(unittest.TestCase):
    def setUp(self):
        self.f = EventFactory()

    def start(self, llm):
        rt, ad = runtime(llm)
        start_episode(rt, self.f, TAGS)
        rt.emit(snapshot(self.f, {"agent_0": None, "agent_1": None}))
        return rt, ad

    def test_llm_decides_escalated_findings(self):
        llm = FakeLLM([{"name": "inject_message",
                        "arguments": {"agent_id": "agent_1", "text": "Please pick cup."}},
                       {"name": "interrupt", "arguments": {"agent_id": "agent_1", "reason": "x"}}])
        rt, ad = self.start(llm)
        act(rt, self.f, 1, "agent_1", "wait", {}, 0)
        two_pick_failures(rt, self.f)
        self.assertEqual(len(llm.calls), 1)  # only on repeat_failure, not the first failure
        tail = [(c.type, c.agent_id) for c in ad.cmds[-2:]]
        self.assertEqual(tail, [("interrupt", "agent_1"), ("inject_message", "agent_1")])
        self.assertTrue(ad.cmds[-1].payload["text"].startswith("[Supervisor]"))
        dec = [r for r in rt.records if r["type"] == "supervisor.llm_decision"]
        self.assertEqual(dec[0]["data"]["used"], "llm")
        ctx = llm.calls[0][1]["content"]
        self.assertIn("repeat_failure", ctx)
        self.assertIn("default_intervention", ctx)

    def test_first_failure_is_rule_only(self):
        llm = FakeLLM()
        rt, ad = self.start(llm)
        act(rt, self.f, 1, "agent_0", "pick", {"target_obj": "cup"}, 10, 109, "timeout")
        rt.emit(snapshot(self.f, {"agent_0": None}, step=110))
        self.assertEqual(llm.calls, [])
        self.assertEqual([c.type for c in ad.cmds], ["inject_message"])

    def test_llm_error_falls_back_to_rules(self):
        llm = FakeLLM(TimeoutError("slow"))
        rt, ad = self.start(llm)
        act(rt, self.f, 1, "agent_1", "wait", {}, 0)
        two_pick_failures(rt, self.f)
        dec = [r["data"] for r in rt.records if r["type"] == "supervisor.llm_decision"]
        self.assertEqual(dec[0]["used"], "fallback")
        self.assertIn("help_routing", [x.rule for x in rt.findings])
        self.assertIn(("interrupt", "agent_1"), [(c.type, c.agent_id) for c in ad.cmds])

    def test_use_default_and_no_intervention(self):
        rt, ad = self.start(FakeLLM([{"name": "use_default", "arguments": {}}]))
        two_pick_failures(rt, self.f)
        n_default = len(ad.cmds)
        self.assertGreater(n_default, 1)
        rt2, ad2 = self.start(FakeLLM([{"name": "no_intervention",
                                        "arguments": {"reason": "fine"}}]))
        two_pick_failures(rt2, self.f)
        self.assertEqual(len(ad2.cmds), 1)  # only the first-failure template

    def test_garbage_reply_falls_back(self):
        rt, ad = self.start(FakeLLM([{"name": "inject_message",
                                      "arguments": {"agent_id": "nobody", "text": "x"}}]))
        two_pick_failures(rt, self.f)
        dec = [r["data"] for r in rt.records if r["type"] == "supervisor.llm_decision"]
        self.assertEqual(dec[0]["used"], "fallback")
        self.assertGreater(len(ad.cmds), 1)

    def test_budget(self):
        rt = InfraRuntime({"enabled": True, "supervisor": {"llm": {
            "enabled": True, "max_calls_per_episode": 1, "min_interval_steps": 0}}},
            llm_chat=FakeLLM([{"name": "use_default", "arguments": {}}]))
        start_episode(rt, self.f, TAGS)
        rt.emit(snapshot(self.f, {"agent_0": None, "agent_1": None}))
        self.assertTrue(rt.llm._can_call(0)[0])
        rt.llm.calls = 1
        self.assertEqual(rt.llm._can_call(0), (False, "llm budget exhausted"))


class PlanReviewTest(unittest.TestCase):
    def test_plan_review_only_advises(self):
        llm = FakeLLM([{"name": "inject_message",
                        "arguments": {"agent_id": "agent_1", "text": "Take cup 2."}},
                       {"name": "interrupt", "arguments": {"agent_id": "agent_0", "reason": "x"}}])
        rt, ad = runtime(llm, assist={"plan_review": True})
        f = EventFactory()
        start_episode(rt, f, TAGS)
        rt.emit(f("plan.assigned", {"assignments": {"agent_0": "do everything",
                                                    "agent_1": "wait"}}))
        self.assertEqual(len(llm.calls), 1)
        self.assertIn("agreed on a plan", llm.calls[0][0]["content"])
        self.assertEqual([(c.type, c.agent_id) for c in ad.cmds], [("inject_message", "agent_1")])

    def test_plan_review_off_by_default(self):
        llm = FakeLLM()
        rt, _ = runtime(llm)
        f = EventFactory()
        start_episode(rt, f, TAGS)
        rt.emit(f("plan.assigned", {"assignments": {"agent_0": "x"}}))
        self.assertEqual(llm.calls, [])


class AssistRulesTest(unittest.TestCase):
    def episode(self, rt, f, max_steps=750):
        rt.emit(f("episode.started", {
            "task_text": "move two cups", "max_steps": max_steps,
            "goal": {"conditions": ["at(cup,TARGET_cup)", "at(mug,TARGET_mug)",
                                    "not_holding(agent_0)"]},
            "entities": [{"entity_id": e, "type": t} for e, t in (
                ("cup", "movable_entity_type"), ("mug", "movable_entity_type"),
                ("TARGET_cup", "goal_entity_type"), ("TARGET_mug", "goal_entity_type"),
                ("agent_0", "robot_entity_type"), ("agent_1", "robot_entity_type"))]}))
        from helpers import emos_action_space
        for aid, tags in TAGS.items():
            rt.emit(f("agent.registered", {"embodiment": "Robot", "capability_tags": tags,
                                           "action_space": emos_action_space(),
                                           "supports": ["inject_message", "interrupt"]},
                      agent_id=aid))
        rt.emit(snapshot(f, {"agent_0": None, "agent_1": None}, predicates=["not_holding(agent_0)"]))

    def test_workload_risk_suggests_idle_teammate(self):
        rt, ad = runtime(FakeLLM([{"name": "use_default", "arguments": {}}]),
                         assist={"workload_balance": True},
                         rule_params={"stagnation": {"window": 10000}})
        f = EventFactory()
        self.episode(rt, f)
        act(rt, f, 1, "agent_1", "wait", {}, 0)
        act(rt, f, 1, "agent_0", "nav_to_obj", {"target_obj": "cup"}, 0)
        rt.emit(snapshot(f, {"agent_0": None}, predicates=["not_holding(agent_0)"], step=25))
        fs = [x for x in rt.findings if x.rule == "workload_risk"]
        # agent_1 is told to take the unowned 'mug' condition (agent_0 works on cup)
        self.assertEqual([(x.agent_id, x.evidence.get("condition")) for x in fs],
                         [("agent_1", "at(mug,TARGET_mug)")])
        self.assertIn(("interrupt", "agent_1"), [(c.type, c.agent_id) for c in ad.cmds])
        rt.emit(snapshot(f, {"agent_0": None}, predicates=["not_holding(agent_0)"], step=50))
        self.assertEqual(len([x for x in rt.findings if x.rule == "workload_risk"]), 1)  # once

    def test_time_based_suggestion_needs_llm(self):
        rt, ad = runtime(None, assist={"workload_balance": True},
                         rule_params={"stagnation": {"window": 10000}})  # no LLM
        f = EventFactory()
        self.episode(rt, f)
        act(rt, f, 1, "agent_1", "wait", {}, 0)
        act(rt, f, 1, "agent_0", "nav_to_obj", {"target_obj": "cup"}, 0)
        self.assertIn("workload_risk", [x.rule for x in rt.findings])  # detected ...
        self.assertEqual(ad.cmds, [])                                  # ... but not sent

    def test_no_risk_when_time_suffices(self):
        rt, _ = runtime(assist={"workload_balance": True},
                        rule_params={"stagnation": {"window": 10000}})
        f = EventFactory()
        self.episode(rt, f, max_steps=5000)
        act(rt, f, 1, "agent_1", "wait", {}, 0)
        act(rt, f, 1, "agent_0", "nav_to_obj", {"target_obj": "cup"}, 0)
        rt.emit(snapshot(f, {"agent_0": None}, step=25))
        self.assertNotIn("workload_risk", [x.rule for x in rt.findings])

    def test_owner_informed_without_interrupt(self):
        rt, ad = runtime(assist={"workload_balance": True},
                         rule_params={"stagnation": {"window": 10000}})
        f = EventFactory()
        self.episode(rt, f)
        act(rt, f, 1, "agent_0", "nav_to_obj", {"target_obj": "mug"}, 0, 50)  # owns mug
        act(rt, f, 2, "agent_0", "nav_to_obj", {"target_obj": "cup"}, 51)    # now on cup
        act(rt, f, 1, "agent_1", "wait", {}, 0)
        rt.emit(snapshot(f, {"agent_0": None}, step=60))
        owner_msgs = [c for c in ad.cmds if c.agent_id == "agent_0"]
        self.assertTrue(owner_msgs)
        self.assertEqual({c.type for c in owner_msgs}, {"inject_message"})

    def test_blocked_owner_handed_over_without_time_risk(self):
        rt, ad = runtime(assist={"workload_balance": True},
                         rule_params={"stagnation": {"window": 10000}})
        f = EventFactory()
        self.episode(rt, f, max_steps=100000)  # plenty of time
        act(rt, f, 1, "agent_0", "wait", {}, 0)
        act(rt, f, 1, "agent_1", "pick", {"target_obj": "mug"}, 0, 99, "timeout")
        rt.emit(snapshot(f, {"agent_1": None}, step=100))           # pick inferred: failure
        act(rt, f, 2, "agent_1", "nav_to_obj", {"target_obj": "cup"}, 101)  # busy elsewhere
        fs = [x for x in rt.findings if x.rule == "workload_risk" and "owner" in x.evidence]
        self.assertEqual([(x.agent_id, x.evidence["condition"], x.evidence["blocked_owner"])
                          for x in fs], [("agent_0", "at(mug,TARGET_mug)", True)])
        told = [x for x in rt.findings if x.rule == "workload_risk" and "helper" in x.evidence]
        self.assertEqual([x.agent_id for x in told], ["agent_1"])  # owner informed, not stopped
        self.assertIs(told[0].interrupt, False)

    def test_repeated_denial(self):
        rt, ad = runtime()
        f = EventFactory()
        start_episode(rt, f, TAGS)
        rt.emit(snapshot(f, {"agent_0": "cup", "agent_1": None}))
        for n in range(1, 5):  # pick while holding -> precondition deny
            e = f("action.proposed", {"action_id": f"agent_0:{n}", "name": "pick",
                                      "args": {"target_obj": "cup"}}, agent_id="agent_0")
            rt.emit(e)
            rt.gate(e)
            rt.emit(f("action.ended", {"action_id": f"agent_0:{n}", "status": "rejected"},
                      agent_id="agent_0"))
        self.assertEqual([x.rule for x in rt.findings].count("repeated_denial"), 1)


class EnvTest(unittest.TestCase):
    def test_env_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write("# c\nSUPERVISOR_LLM_API_KEY=k\nSUPERVISOR_LLM_MODEL='m'\n")
        env = resolve_env(fh.name)
        self.assertEqual((env["SUPERVISOR_LLM_API_KEY"], env["SUPERVISOR_LLM_MODEL"]), ("k", "m"))


if __name__ == "__main__":
    unittest.main()
