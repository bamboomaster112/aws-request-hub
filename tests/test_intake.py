import json
import unittest
from decimal import Decimal
from unittest import mock

from botocore.exceptions import ClientError

from helpers import FakeTable, load, profile

intake = load("request_intake")

FORM = {
    "PK": "CONFIG", "SK": "FORM#PURCHASE", "module": "PURCHASE", "version": Decimal(7), "approvalField": "amount",
    "fields": [
        {"key": "item", "label": "Item", "type": "text", "required": True, "maxLength": Decimal(20)},
        {"key": "category", "label": "Category", "type": "select", "required": True, "options": ["Hardware", "Software"]},
        {"key": "amount", "label": "Total cost", "type": "amount", "required": True, "min": Decimal(0), "max": Decimal(5000)},
        {"key": "neededBy", "label": "Needed by", "type": "date", "required": False},
        {"key": "notes", "label": "Notes", "type": "textarea", "required": False},
        {"key": "confirmed", "label": "Budget confirmed", "type": "checkbox", "required": True},
    ],
}
GOOD = {"item": "Laptop", "category": "Hardware", "amount": 1234.567, "neededBy": "2026-12-01", "confirmed": True}


def event(body, sub="user-1", key=None):
    headers = {"Idempotency-Key": key} if key else {}
    return {"body": json.dumps(body), "headers": headers,
            "requestContext": {"authorizer": {"claims": {"sub": sub, "email": f"{sub}@example.com"}}}}


class ValidationTests(unittest.TestCase):
    def test_valid_values_are_normalised(self):
        fields, errors = intake.validate(FORM, GOOD)
        self.assertEqual(errors, {})
        by_key = {f["key"]: f for f in fields}
        self.assertEqual(by_key["amount"]["value"], Decimal("1234.57"))
        self.assertEqual(by_key["amount"]["label"], "Total cost")
        self.assertTrue(by_key["confirmed"]["value"])
        self.assertNotIn("notes", by_key)  # optional and empty

    def test_each_rule_reports_against_its_field(self):
        bad = {"item": "x" * 21, "category": "Furniture", "amount": 9999, "neededBy": "01/12/2026",
               "confirmed": False, "extra": "nope"}
        _, errors = intake.validate(FORM, bad)
        self.assertEqual(set(errors), {"item", "category", "amount", "neededBy", "confirmed", "extra"})

    def test_required_and_type_checks(self):
        _, errors = intake.validate(FORM, {"amount": True, "confirmed": "yes"})
        self.assertEqual(errors["item"], "This field is required")
        self.assertEqual(errors["amount"], "Enter a number")
        self.assertEqual(errors["confirmed"], "Must be true or false")


class HandlerTests(unittest.TestCase):
    def submit(self, body, people=None, key=None, put_error=None):
        table = FakeTable(people if people is not None else [profile("user-1", modules=("PURCHASE",)), FORM])
        if put_error:
            table.put_item.side_effect = put_error
        with mock.patch.object(intake, "table", return_value=table):
            res = intake.handler(event(body, key=key), None)
        return res, json.loads(res["body"]), table

    def test_stores_snapshot_with_form_version_and_approval_value(self):
        res, body, table = self.submit({"type": "PURCHASE", "title": "New laptop", "fields": GOOD, "currency": "inr"})
        self.assertEqual(res["statusCode"], 201, body)
        item = table.put_item.call_args.kwargs["Item"]
        self.assertEqual((item["formVersion"], item["amount"], item["currency"]), (7, Decimal("1234.57"), "INR"))
        self.assertEqual(item["approvalField"], "Total cost")
        self.assertEqual(item["status"], "SUBMITTED")
        self.assertEqual(table.put_item.call_args.kwargs["ConditionExpression"], "attribute_not_exists(PK)")

    def test_module_access_and_account_status(self):
        res, _, _ = self.submit({"type": "PURCHASE", "title": "New laptop", "fields": GOOD},
                             people=[profile("user-1", modules=("LEAVE",)), FORM])
        self.assertEqual(res["statusCode"], 403)
        res, _, _ = self.submit({"type": "PURCHASE", "title": "New laptop", "fields": GOOD},
                             people=[profile("user-1", status="DISABLED"), FORM])
        self.assertEqual(res["statusCode"], 403)
        res, _, _ = self.submit({"type": "PURCHASE", "title": "New laptop", "fields": GOOD},
                             people=[profile("user-1", role="admins", modules=()), FORM])
        self.assertEqual(res["statusCode"], 201)

    def test_field_errors_come_back_by_key(self):
        res, body, table = self.submit({"type": "PURCHASE", "title": "x", "fields": {"item": "Laptop"}})
        self.assertEqual(res["statusCode"], 400)
        self.assertTrue({"title", "category", "amount", "confirmed"} <= set(body["fieldErrors"]))
        table.put_item.assert_not_called()

    def test_module_without_a_form(self):
        res, _, _ = self.submit({"type": "PURCHASE", "title": "New laptop", "fields": GOOD},
                             people=[profile("user-1", modules=("PURCHASE",))])
        self.assertEqual(res["statusCode"], 409)

    def test_idempotency_key_makes_retries_safe(self):
        first, body1, _ = self.submit({"type": "PURCHASE", "title": "New laptop", "fields": GOOD}, key="key-12345678")
        dup = ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "PutItem")
        second, body2, _ = self.submit({"type": "PURCHASE", "title": "New laptop", "fields": GOOD}, key="key-12345678", put_error=dup)
        self.assertEqual((first["statusCode"], second["statusCode"]), (201, 200))
        self.assertEqual(body1["requestId"], body2["requestId"])
        self.assertTrue(body2["duplicate"])


if __name__ == "__main__":
    unittest.main()
