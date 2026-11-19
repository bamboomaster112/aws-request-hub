import json
import unittest
from unittest import mock

from helpers import load

engine = load("policy_engine")

NOW = 1_800_000_000_000
DAY = 86_400_000
TIMERS = {"slaSeconds": 180, "escalationSeconds": 180}


def person(uid, role="requesters", manager=None, status="ACTIVE"):
    return {"userId": uid, "email": f"{uid}@example.com", "name": uid.title(), "role": role,
            "status": status, "managerId": manager}


def org():
    """rahul -> ana -> priya -> (nobody). Two admins: priya and vikram. Finance: li."""
    return {
        "rahul": person("rahul", manager="ana"),
        "ana": person("ana", "approvers", manager="priya"),
        "priya": person("priya", "admins"),
        "vikram": person("vikram", "admins"),
        "li": person("li", "approvers", manager="priya"),
    }


PURCHASE = {"version": 3, "levels": [
    {"kind": "MANAGER", "minAmount": 0},
    {"kind": "GROUP", "groupId": "finance", "minAmount": 1000},
    {"kind": "ADMINS", "minAmount": 10000},
]}
GROUPS = {"finance": {"groupId": "finance", "name": "Finance", "members": ["li"]}}


def plan(amount, requester="rahul", workflow=PURCHASE, profiles=None, groups=GROUPS, rule=None):
    profiles = profiles or org()
    request = {"requestId": "r1", "type": "PURCHASE", "amount": amount, "requesterId": requester}
    return engine.build_plan(request, profiles.get(requester), workflow, profiles, groups, rule, NOW, TIMERS)


def chain(result):
    return [(l["label"], l["approverIds"]) for l in result["levels"]]


class AutoApprovalTests(unittest.TestCase):
    rule = {"module": "PURCHASE", "maxAmount": 500, "expiresAt": NOW + DAY, "expiresOn": "2027-01-16"}

    def test_nothing_is_auto_approved_without_a_rule(self):
        result = plan(1)
        self.assertFalse(result["autoApprove"])
        self.assertEqual(chain(result), [("Line manager: Ana", ["ana"])])

    def test_rule_within_limit_auto_approves(self):
        result = plan(500, rule=self.rule)
        self.assertTrue(result["autoApprove"])
        self.assertIn("up to 500", result["reasons"][0])

    def test_over_the_limit_goes_to_approval(self):
        result = plan(501, rule=self.rule)
        self.assertFalse(result["autoApprove"])
        self.assertIn("above the auto-approval limit of 500", result["reasons"][0])

    def test_expired_rule_goes_to_approval(self):
        result = plan(10, rule={**self.rule, "expiresAt": NOW - 1})
        self.assertFalse(result["autoApprove"])
        self.assertIn("expired", result["reasons"][0])

    def test_disabled_requester_never_auto_approves(self):
        profiles = org()
        profiles["rahul"]["status"] = "DISABLED"
        self.assertFalse(plan(10, profiles=profiles, rule=self.rule)["autoApprove"])


class ChainTests(unittest.TestCase):
    def test_thresholds_add_levels_in_order(self):
        self.assertEqual(len(plan(999)["levels"]), 1)
        self.assertEqual(chain(plan(1000)), [("Line manager: Ana", ["ana"]), ("Finance", ["li"])])
        three = plan(25000)
        self.assertEqual([l["label"] for l in three["levels"]], ["Line manager: Ana", "Finance", "Admins"])
        self.assertEqual(three["levels"][2]["approverIds"], ["priya", "vikram"])
        self.assertEqual(three["lastIndex"], 2)

    def test_the_approvers_own_request_goes_to_their_manager(self):
        self.assertEqual(chain(plan(50, requester="ana")), [("Line manager: Priya", ["priya"])])

    def test_requester_is_never_an_approver(self):
        # Li is the only Finance member and raises the request: admins stand in for Finance.
        result = plan(2000, requester="li")
        self.assertEqual(chain(result), [("Line manager: Priya", ["priya"]), ("Finance (admins standing in)", ["vikram"])])

    def test_same_person_cannot_approve_two_levels(self):
        groups = {"finance": {"groupId": "finance", "name": "Finance", "members": ["ana", "li"]}}
        result = plan(2000, groups=groups)
        self.assertEqual(result["levels"][1]["approverIds"], ["li"])  # Ana already approves level 1

    def test_top_of_the_tree_falls_back_to_other_admins(self):
        result = plan(50, requester="priya")
        self.assertEqual(chain(result), [("Line manager (admins standing in)", ["vikram"])])
        self.assertIn("no manager set", " ".join(result["reasons"]))

    def test_disabled_manager_falls_back_to_admins(self):
        profiles = org()
        profiles["ana"]["status"] = "DISABLED"
        result = plan(50, profiles=profiles)
        self.assertEqual(result["levels"][0]["approverIds"], ["priya", "vikram"])
        self.assertTrue(result["levels"][0]["fallback"])

    def test_reasons_are_written_for_people(self):
        profiles = org()
        request = {"requestId": "r1", "type": "PURCHASE", "amount": 4200, "requesterId": "rahul", "approvalField": "Total cost"}
        result = engine.build_plan(request, profiles["rahul"], PURCHASE, profiles, GROUPS, None, NOW, TIMERS)
        text = " ".join(result["reasons"])
        self.assertIn("nobody has auto-approval for Purchasing requests", text)
        self.assertIn("Admins only approve from 10,000, and the total cost is 4,200, so this level is skipped.", text)
        self.assertIn("Approval order: Line manager: Ana, then Finance.", text)
        self.assertNotIn("->", text)

    def test_nobody_eligible_is_unroutable(self):
        profiles = {"solo": person("solo", "admins")}
        result = plan(50, requester="solo", profiles=profiles, workflow={"levels": [{"kind": "MANAGER", "minAmount": 0}]})
        self.assertFalse(result["routable"])
        self.assertIn("Nobody can approve level", result["reasons"][-1])

    def test_plan_json_and_dynamodb_shapes(self):
        result = plan(1000)
        self.assertEqual(result["levels"][0]["approverIdsDdb"], [{"S": "ana"}])
        stored = json.loads(result["planJson"])
        self.assertEqual([l["label"] for l in stored["levels"]], ["Line manager: Ana", "Finance"])
        self.assertEqual(result["escalateToDdb"], [{"S": "priya"}, {"S": "vikram"}])

    def test_workflow_timers_win_over_defaults_with_a_floor(self):
        result = plan(10, workflow={**PURCHASE, "slaSeconds": 30, "escalationSeconds": 600})
        self.assertEqual((result["slaSeconds"], result["escalationSeconds"]), (60, 600))


class HandlerTests(unittest.TestCase):
    def test_handler_loads_context_and_returns_the_plan(self):
        with mock.patch.object(engine, "load_context", return_value=(org()["rahul"], PURCHASE, org(), GROUPS, None)), \
                mock.patch.object(engine, "load_defaults", return_value=TIMERS):
            out = engine.handler({"request": {"requestId": "r1", "type": "PURCHASE", "amount": "1500",
                                              "requesterId": "rahul"}}, None)
        self.assertEqual(out["levelCount"], 2)
        self.assertTrue(out["routable"])


if __name__ == "__main__":
    unittest.main()
