import unittest

from embodied_infra.semantics import (EvalContext, SemanticsError, check_all, evaluate,
                                      parse, validate_table)


class SemanticsTest(unittest.TestCase):
    def ctx(self, holding="__missing__", args=None, preds=None):
        c = EvalContext(agent_id="agent_0", args=args, predicates=preds)
        if holding != "__missing__":
            c.agent_holding = holding
        return c

    def test_compare_null(self):
        self.assertTrue(evaluate(parse("agent.holding == null"), self.ctx(holding=None)))
        self.assertFalse(evaluate(parse("agent.holding == null"), self.ctx(holding="cup")))

    def test_compare_args(self):
        c = self.ctx(holding="cup", args={"target_obj": "cup"})
        self.assertTrue(evaluate(parse("agent.holding == args.target_obj"), c))
        self.assertFalse(evaluate(parse("agent.holding != args.target_obj"), c))
        self.assertTrue(evaluate(parse("args.target_obj == 'cup'"), c))

    def test_pred(self):
        c = self.ctx(args={"target_obj": "cup", "target_location": "T"},
                     preds={"at(cup,T)", "robot_at(cup,agent_0)"})
        self.assertTrue(evaluate(parse("pred(at, args.target_obj, args.target_location)"), c))
        self.assertTrue(evaluate(parse("pred(robot_at, args.target_obj, agent.id)"), c))
        self.assertFalse(evaluate(parse("not pred(at, args.target_obj, args.target_location)"), c))

    def test_unknown_is_none(self):
        # holding never observed / predicates never observed / missing arg
        self.assertIsNone(evaluate(parse("agent.holding == null"), self.ctx()))
        self.assertIsNone(evaluate(parse("pred(at, args.target_obj, 'x')"),
                                   self.ctx(args={"target_obj": "cup"}, preds=None)))
        self.assertIsNone(evaluate(parse("agent.holding == args.nope"), self.ctx(holding=None, args={})))

    def test_check_all(self):
        r = check_all(["agent.holding == null", "pred(at, 'a', 'b')"],
                      self.ctx(holding=None, preds={"at(a,b)"}))
        self.assertIs(r.ok, True)
        r = check_all(["agent.holding == null", "pred(at, 'a', 'c')"],
                      self.ctx(holding=None, preds={"at(a,b)"}))
        self.assertIs(r.ok, False)
        self.assertEqual(r.failed, ["pred(at, 'a', 'c')"])

    def test_bad_syntax(self):
        for bad in ["agent.position == null", "eval(1)", "pred(at, __import__)", "holding"]:
            with self.assertRaises(SemanticsError, msg=bad):
                parse(bad)
        errs = validate_table({"pick": {"semantics": {"pre": ["agent.holding = null"]}}})
        self.assertEqual(len(errs), 1)


if __name__ == "__main__":
    unittest.main()
