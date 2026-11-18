import json
import pathlib
import re
import unittest

from helpers import load

engine = load("policy_engine")

TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "template.yaml"


def rules_from_template():
    """Reads the policy JSON straight out of template.yaml so tests and deployment never drift."""
    text = TEMPLATE.read_text()
    block = text.split("Value: !Sub |", 1)[1]
    lines = []
    for line in block.splitlines()[1:]:
        if line.strip() and not line.startswith("        "):
            break
        lines.append(line)
    doc = "\n".join(lines).replace("${ApprovalSlaSeconds}", "180").replace("${EscalationSlaSeconds}", "180")
    return json.loads(doc)


RULES = rules_from_template()


class PolicyEngineTests(unittest.TestCase):
    def decide(self, rtype, amount):
        return engine.evaluate({"type": rtype, "amount": amount}, RULES)

    def test_small_purchase_is_auto_approved(self):
        result = self.decide("PURCHASE", 250)
        self.assertTrue(result["autoApprove"])
        self.assertEqual(result["riskTier"], "LOW")

    def test_mid_purchase_needs_an_approver(self):
        result = self.decide("PURCHASE", 1200)
        self.assertFalse(result["autoApprove"])
        self.assertEqual((result["riskTier"], result["approverGroup"]), ("MEDIUM", "approvers"))

    def test_large_purchase_needs_an_admin(self):
        result = self.decide("PURCHASE", 5000)
        self.assertEqual((result["riskTier"], result["approverGroup"]), ("HIGH", "admins"))

    def test_access_always_needs_a_human(self):
        result = self.decide("ACCESS", 0)
        self.assertFalse(result["autoApprove"])
        self.assertEqual(result["riskTier"], "MEDIUM")

    def test_change_is_always_high_risk(self):
        result = self.decide("CHANGE", 0)
        self.assertEqual((result["riskTier"], result["approverGroup"]), ("HIGH", "admins"))

    def test_short_leave_is_auto_approved_long_leave_is_high_risk(self):
        self.assertTrue(self.decide("LEAVE", 2)["autoApprove"])
        self.assertEqual(self.decide("LEAVE", 5)["riskTier"], "MEDIUM")
        self.assertEqual(self.decide("LEAVE", 15)["riskTier"], "HIGH")

    def test_unknown_type_and_bad_amount_fall_back_safely(self):
        result = engine.evaluate({"type": "SOMETHING", "amount": "not-a-number"}, RULES)
        self.assertEqual(result["riskTier"], "LOW")  # GENERAL with 0
        self.assertIn("GENERAL", result["reasons"][0])

    def test_timers_have_a_floor_and_are_integers(self):
        rules = {"default": {"slaSeconds": 5, "escalationSeconds": 10}, "types": {}}
        result = engine.evaluate({"type": "ACCESS", "amount": 1}, rules)
        self.assertEqual((result["slaSeconds"], result["escalationSeconds"]), (60, 60))
        self.assertIsInstance(result["slaSeconds"], int)

    def test_every_decision_explains_itself(self):
        for rtype in engine.VALID_TYPES:
            for amount in (0, 1, 499, 500, 5000, 20000):
                result = self.decide(rtype, amount)
                self.assertTrue(result["reasons"], f"{rtype} {amount}")
                self.assertTrue(re.match(r"^[A-Z]", result["reasons"][0]))


if __name__ == "__main__":
    unittest.main()
