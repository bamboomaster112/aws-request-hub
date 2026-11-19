"""Static checks on the state machine so a broken definition fails in CI, not in the account."""
import json
import pathlib
import unittest

ASL = json.loads((pathlib.Path(__file__).resolve().parent.parent / "statemachine" / "approval-workflow.asl.json").read_text())
LEVEL = ASL["States"]["ApprovalLevels"]["ItemProcessor"]


def targets(state):
    found = [state.get("Next"), state.get("Default")]
    found += [c.get("Next") for c in state.get("Choices", [])]
    found += [c.get("Next") for c in state.get("Catch", [])]
    return [t for t in found if t]


def all_states():
    out = dict(ASL["States"])
    out.update(LEVEL["States"])
    return out


class WorkflowDefinitionTests(unittest.TestCase):
    def check_graph(self, machine):
        states = machine["States"]
        for name, state in states.items():
            for target in targets(state):
                self.assertIn(target, states, f"{name} -> {target}")
        seen, todo = set(), [machine["StartAt"]]
        while todo:
            name = todo.pop()
            if name in seen:
                continue
            seen.add(name)
            todo.extend(targets(states[name]))
        self.assertEqual(seen, set(states))

    def test_both_graphs_are_closed_and_reachable(self):
        self.check_graph(ASL)
        self.check_graph(LEVEL)

    def test_levels_run_one_at_a_time(self):
        level_map = ASL["States"]["ApprovalLevels"]
        self.assertEqual(level_map["MaxConcurrency"], 1)
        self.assertEqual(level_map["ItemsPath"], "$.policy.levels")
        caught = {e for c in level_map["Catch"] for e in c["ErrorEquals"]}
        self.assertTrue({"Rejected", "Expired", "AlreadyHandled"} <= caught)

    def test_human_waits_use_task_tokens_with_timeouts(self):
        for name in ("AwaitLevelDecision", "EscalateLevel"):
            state = LEVEL["States"][name]
            self.assertTrue(state["Resource"].endswith(".waitForTaskToken"), name)
            self.assertIn("TimeoutSecondsPath", state, name)
            self.assertEqual(state["Parameters"]["ExpressionAttributeValues"][":t"], {"S.$": "$$.Task.Token"})
            self.assertIn("States.Timeout", [e for c in state["Catch"] for e in c["ErrorEquals"]])
            self.assertIn(":ids", state["Parameters"]["ExpressionAttributeValues"], "each level names its approvers")

    def test_status_writes_are_conditional_so_duplicates_are_harmless(self):
        for name in ("AutoApprove", "MarkUnroutable", "AwaitLevelDecision", "EscalateLevel", "ExpireRequest",
                     "MarkApproved", "MarkRejected"):
            self.assertIn("ConditionExpression", all_states()[name]["Parameters"], name)

    def test_every_status_write_keeps_the_status_index_in_step(self):
        for name, state in all_states().items():
            params = state.get("Parameters", {})
            if "UpdateExpression" in params and "#s = :s" in params["UpdateExpression"]:
                status = params["ExpressionAttributeValues"][":s"]["S"]
                self.assertEqual(params["ExpressionAttributeValues"][":g2"]["S"], f"STATUS#{status}", name)

    def test_terminal_writes_clear_the_token_and_approvers(self):
        for name in ("MarkApproved", "MarkRejected", "ExpireRequest", "MarkFailed"):
            expr = all_states()[name]["Parameters"]["UpdateExpression"]
            self.assertIn("REMOVE approvalToken, approverIds", expr, name)

    def test_decisions_are_child_items(self):
        for name in ("RecordApproval", "RecordRejection"):
            item = LEVEL["States"][name]["Parameters"]["Item"]
            self.assertEqual(item["SK"], {"S.$": "States.Format('APPROVAL#{}', $.level.index)"})


if __name__ == "__main__":
    unittest.main()
