"""Static checks on the state machine so a broken definition fails in CI, not in the account."""
import json
import pathlib
import unittest

ASL = json.loads((pathlib.Path(__file__).resolve().parent.parent / "statemachine" / "approval-workflow.asl.json").read_text())
STATES = ASL["States"]


def targets(state):
    found = [state.get("Next"), state.get("Default")]
    found += [c.get("Next") for c in state.get("Choices", [])]
    found += [c.get("Next") for c in state.get("Catch", [])]
    return [t for t in found if t]


class WorkflowDefinitionTests(unittest.TestCase):
    def test_every_transition_points_at_a_real_state(self):
        for name, state in STATES.items():
            for target in targets(state):
                self.assertIn(target, STATES, f"{name} -> {target}")

    def test_every_state_is_reachable(self):
        seen, todo = set(), [ASL["StartAt"]]
        while todo:
            name = todo.pop()
            if name in seen:
                continue
            seen.add(name)
            todo.extend(targets(STATES[name]))
        self.assertEqual(seen, set(STATES))

    def test_human_waits_use_task_tokens_with_timeouts(self):
        for name in ("RequestApproval", "Escalate"):
            state = STATES[name]
            self.assertTrue(state["Resource"].endswith(".waitForTaskToken"), name)
            self.assertIn("TimeoutSecondsPath", state, name)
            self.assertEqual(state["Parameters"]["ExpressionAttributeValues"][":t"], {"S.$": "$$.Task.Token"})
            self.assertIn("States.Timeout", [e for c in state["Catch"] for e in c["ErrorEquals"]])

    def test_status_writes_are_conditional_so_duplicates_are_harmless(self):
        for name in ("AutoApprove", "RequestApproval", "Escalate", "Expire", "MarkApproved", "MarkRejected"):
            self.assertIn("ConditionExpression", STATES[name]["Parameters"], name)

    def test_every_status_write_keeps_the_status_index_in_step(self):
        for name, state in STATES.items():
            params = state.get("Parameters", {})
            if "UpdateExpression" in params and "#s = :s" in params["UpdateExpression"]:
                status = params["ExpressionAttributeValues"][":s"]["S"]
                self.assertEqual(params["ExpressionAttributeValues"][":g2"]["S"], f"STATUS#{status}", name)


if __name__ == "__main__":
    unittest.main()
