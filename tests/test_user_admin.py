import datetime
import json
import unittest
from unittest import mock

from botocore.exceptions import ClientError

from helpers import FakeTable, load, profile

admin_api = load("user_admin")
import common  # noqa: E402  (importable once load() has put the function folder on sys.path)

ADMIN = "11111111-1111-1111-1111-111111111111"
ALICE = "22222222-2222-2222-2222-222222222222"
BOB = "33333333-3333-3333-3333-333333333333"
CAROL = "44444444-4444-4444-4444-444444444444"


def event(path, method="GET", body=None, sub=ADMIN):
    return {
        "resource": "/admin/{proxy+}",
        "httpMethod": method,
        "pathParameters": {"proxy": path},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"claims": {"sub": sub, "email": "boss@example.com"}}},
    }


def cognito_user(sub, email, status="CONFIRMED", enabled=True):
    return {"Username": sub, "Attributes": [{"Name": "sub", "Value": sub}, {"Name": "email", "Value": email}],
            "UserStatus": status, "Enabled": enabled, "UserCreateDate": datetime.datetime(2026, 11, 1)}


def future(days):
    return (datetime.datetime.now(datetime.timezone.utc).date() + datetime.timedelta(days=days)).isoformat()


FORM = {"PK": "CONFIG", "SK": "FORM#PURCHASE", "module": "PURCHASE", "version": 2, "approvalField": "amount",
        "fields": [{"key": "amount", "label": "Total", "type": "amount", "required": True}]}
FINANCE = {"PK": "CONFIG", "SK": "GROUP#finance", "groupId": "finance", "name": "Finance", "members": [BOB], "version": 1}


class Harness(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable([
            profile(ADMIN, role="admins", modules=(), email="boss@example.com"),
            profile(ALICE, modules=("PURCHASE",), email="alice@example.com"),
            {**profile(BOB, role="approvers", modules=("PURCHASE",), email="bob@example.com"), "managerId": ADMIN},
            FORM, FINANCE,
        ])
        self.cognito, self.ddb, self.events = mock.Mock(), mock.Mock(), mock.Mock()
        self.events.put_events.return_value = {"FailedEntryCount": 0}
        self.cognito.admin_get_user.return_value = {"Username": ALICE, "UserStatus": "CONFIRMED", "UserAttributes": []}
        for name, value in (("table", self.table), ("cognito", self.cognito), ("ddb", self.ddb), ("events", self.events)):
            patcher = mock.patch.object(common, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, path, method="GET", body=None, sub=ADMIN):
        res = admin_api.handler(event(path, method, body, sub), None)
        return res["statusCode"], json.loads(res["body"])

    def written(self):
        """Items written by the last transaction, deserialised just enough for assertions."""
        items = self.ddb.transact_write_items.call_args.kwargs["TransactItems"]
        out = []
        for entry in items:
            for kind, spec in entry.items():
                raw = spec.get("Item") or spec.get("Key")
                out.append((kind, {k: list(v.values())[0] for k, v in raw.items()}, spec))
        return out

    def last_event(self):
        return json.loads(self.events.put_events.call_args.kwargs["Entries"][0]["Detail"])


class RoutingTests(Harness):
    def test_only_active_admins_get_in(self):
        self.assertEqual(self.call("users", sub=ALICE)[0], 403)
        self.table.items[("USER#" + ADMIN, "PROFILE")]["status"] = "DISABLED"
        self.assertEqual(self.call("users")[0], 403)

    def test_bad_ids_and_unknown_paths(self):
        self.assertEqual(self.call("users/../../etc")[0], 404)
        self.assertEqual(self.call("nothing/here")[0], 404)


class DirectoryTests(Harness):
    def test_list_merges_cognito_profiles_and_reporting_lines(self):
        self.cognito.list_users.return_value = {"Users": [
            cognito_user(ADMIN, "boss@example.com"), cognito_user(ALICE, "alice@example.com"),
            cognito_user(BOB, "bob@example.com"), cognito_user(CAROL, "carol@example.com", status="FORCE_CHANGE_PASSWORD"),
        ]}
        status, body = self.call("users")
        self.assertEqual(status, 200)
        by_id = {u["userId"]: u for u in body["items"]}
        self.assertEqual(by_id[ADMIN]["reports"], 1)
        self.assertEqual(by_id[BOB]["managerId"], ADMIN)
        self.assertEqual(by_id[CAROL]["status"], "NO_PROFILE")
        self.assertEqual(body["summary"]["invitesPending"], 1)


class CreateTests(Harness):
    def created(self, sub=CAROL, email="new@example.com"):
        self.cognito.admin_create_user.return_value = {"User": cognito_user(sub, email, status="FORCE_CHANGE_PASSWORD")}

    def test_validation(self):
        for body in ({"email": "nope", "role": "requesters"}, {"email": "a@b.co", "role": "superuser"},
                     {"email": "a@b.co", "role": "requesters", "modules": ["PAYROLL"]},
                     {"email": "a@b.co", "role": "requesters", "managerId": ALICE}):
            self.assertEqual(self.call("users", "POST", body)[0], 400, body)
        self.cognito.admin_create_user.assert_not_called()

    def test_invites_with_manager_and_department(self):
        self.created()
        status, body = self.call("users", "POST", {"email": " New@Example.com ", "name": "New Person", "role": "approvers",
                                                   "modules": ["leave", "PURCHASE"], "managerId": BOB, "department": "Ops"})
        self.assertEqual(status, 201, body)
        (_, prof, spec), (_, audit, _) = self.written()
        self.assertEqual(spec["ConditionExpression"], "attribute_not_exists(PK)")
        self.assertEqual((prof["role"], sorted(prof["modules"]), prof["managerId"], prof["department"]),
                         ("approvers", ["LEAVE", "PURCHASE"], BOB, "Ops"))
        self.assertEqual(audit["action"], "CREATED")
        self.assertFalse(self.last_event()["privileged"])

    def test_new_admin_raises_the_privileged_flag_and_can_skip_email(self):
        self.created()
        self.assertEqual(self.call("users", "POST", {"email": "a@b.co", "role": "admins", "sendInvite": False})[0], 201)
        self.assertEqual(self.cognito.admin_create_user.call_args.kwargs["MessageAction"], "SUPPRESS")
        self.assertTrue(self.last_event()["privileged"])

    def test_cognito_user_is_rolled_back_if_the_profile_write_fails(self):
        self.created()
        self.ddb.transact_write_items.side_effect = ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "TransactWriteItems")
        self.assertEqual(self.call("users", "POST", {"email": "a@b.co", "role": "requesters"})[0], 500)
        self.cognito.admin_delete_user.assert_called_once_with(UserPoolId="us-east-1_test", Username=CAROL)


class UpdateTests(Harness):
    def test_grant_revoke_and_optimistic_locking(self):
        status, body = self.call(f"users/{ALICE}", "PATCH", {"modules": ["LEAVE"], "version": 1})
        self.assertEqual(status, 200, body)
        (_, prof, spec), (_, audit, _) = self.written()
        self.assertEqual((spec["ConditionExpression"], prof["modules"], int(prof["version"])), ("#v = :v", ["LEAVE"], 2))
        self.assertIn("granted Leave", audit["summary"])
        self.assertIn("revoked Purchasing", audit["summary"])
        self.assertEqual(self.call(f"users/{ALICE}", "PATCH", {"modules": [], "version": 7})[0], 409)

    def test_reporting_line_rules(self):
        self.assertEqual(self.call(f"users/{ALICE}", "PATCH", {"managerId": BOB})[0], 200)
        (_, prof, _), (_, audit, _) = self.written()
        self.assertEqual(prof["managerId"], BOB)
        self.assertIn("manager set to bob@example.com", audit["summary"])
        status, body = self.call(f"users/{BOB}", "PATCH", {"managerId": ALICE})
        self.assertEqual(status, 400)
        self.assertIn("Requester", body["message"])  # requesters cannot manage people
        status, body = self.call(f"users/{ADMIN}", "PATCH", {"managerId": BOB})
        self.assertEqual(status, 400)
        self.assertIn("loop", body["message"])  # Bob already reports to the admin
        self.assertEqual(self.call(f"users/{ALICE}", "PATCH", {"managerId": ALICE})[0], 400)

    def test_people_with_approval_duties_cannot_lose_them_silently(self):
        status, body = self.call(f"users/{BOB}", "PATCH", {"role": "requesters"})
        self.assertEqual(status, 409)
        self.assertIn("Finance", body["message"])  # Bob is a Finance member
        self.table.items[("USER#" + ALICE, "PROFILE")]["managerId"] = BOB
        self.assertEqual(self.call(f"users/{BOB}", "DELETE")[0], 409)

    def test_admins_cannot_demote_themselves(self):
        self.assertEqual(self.call(f"users/{ADMIN}", "PATCH", {"role": "requesters"})[0], 400)

    def test_unmanaged_cognito_user_is_adopted(self):
        self.cognito.admin_get_user.return_value = {"Username": CAROL, "Enabled": True, "UserAttributes": [
            {"Name": "sub", "Value": CAROL}, {"Name": "email", "Value": "carol@example.com"}]}
        self.cognito.admin_list_groups_for_user.return_value = {"Groups": [{"GroupName": "approvers"}]}
        status, body = self.call(f"users/{CAROL}", "PATCH", {"modules": ["CHANGE"]})
        self.assertEqual(status, 200, body)
        (_, prof, spec), (_, audit, _) = self.written()
        self.assertEqual((prof["role"], prof["modules"], audit["action"]), ("approvers", ["CHANGE"], "ACCESS_SET_UP"))


class ActionAndDeleteTests(Harness):
    def test_disable_blocks_sign_in_and_api_access(self):
        status, body = self.call(f"users/{ALICE}/actions", "POST", {"action": "DISABLE"})
        self.assertEqual(status, 200, body)
        self.cognito.admin_disable_user.assert_called_once()
        self.cognito.admin_user_global_sign_out.assert_called_once()
        (_, prof, _), (_, audit, _) = self.written()
        self.assertEqual((prof["status"], audit["action"]), ("DISABLED", "DISABLE"))

    def test_self_protection(self):
        self.assertEqual(self.call(f"users/{ADMIN}/actions", "POST", {"action": "DISABLE"})[0], 400)
        self.assertEqual(self.call(f"users/{ADMIN}", "DELETE")[0], 400)

    def test_invite_and_reset_follow_the_sign_in_state(self):
        self.assertEqual(self.call(f"users/{ALICE}/actions", "POST", {"action": "RESEND_INVITE"})[0], 409)
        self.cognito.admin_get_user.return_value = {"Username": ALICE, "UserStatus": "FORCE_CHANGE_PASSWORD"}
        self.assertEqual(self.call(f"users/{ALICE}/actions", "POST", {"action": "RESET_PASSWORD"})[0], 409)
        self.assertEqual(self.call(f"users/{ALICE}/actions", "POST", {"action": "RESEND_INVITE"})[0], 200)

    def test_delete_removes_profile_and_auto_rules_but_keeps_history(self):
        self.table.items[("USER#" + ALICE, "AUTORULE#PURCHASE")] = {"PK": "USER#" + ALICE, "SK": "AUTORULE#PURCHASE",
                                                                    "module": "PURCHASE", "expiresAt": 1}
        self.assertEqual(self.call(f"users/{ALICE}", "DELETE")[0], 200)
        kinds = [(k, item.get("SK")) for k, item, _ in self.written()]
        self.assertEqual(kinds, [("Delete", "AUTORULE#PURCHASE"), ("Delete", "PROFILE"), ("Put", kinds[-1][1])])
        self.assertTrue(kinds[-1][1].startswith("AUDIT#"))


class AutoRuleTests(Harness):
    def test_grant_with_limit_and_expiry(self):
        status, body = self.call(f"users/{ALICE}/auto-rules/PURCHASE", "PUT", {"maxAmount": 500, "expiresOn": future(30)})
        self.assertEqual(status, 200, body)
        (_, rule, _), (_, audit, _) = self.written()
        self.assertEqual((rule["module"], rule["maxAmount"], rule["GSI2PK"]), ("PURCHASE", "500", "DIRECTORY#AUTORULES"))
        self.assertEqual(audit["action"], "AUTO_RULE_SET")

    def test_guardrails(self):
        cases = [
            (f"users/{ADMIN}/auto-rules/PURCHASE", {"maxAmount": 5, "expiresOn": future(5)}, 403),   # self-grant
            (f"users/{ALICE}/auto-rules/LEAVE", {"maxAmount": 5, "expiresOn": future(5)}, 409),      # module not held
            (f"users/{ALICE}/auto-rules/PURCHASE", {"maxAmount": 5, "expiresOn": future(400)}, 400), # too long
            (f"users/{ALICE}/auto-rules/PURCHASE", {"maxAmount": 5, "expiresOn": future(-1)}, 400),  # in the past
            (f"users/{ALICE}/auto-rules/PURCHASE", {"maxAmount": 0, "expiresOn": future(5)}, 400),   # no limit
            (f"users/{ALICE}/auto-rules/PAYROLL", {"maxAmount": 5, "expiresOn": future(5)}, 404),
        ]
        for path, body, expected in cases:
            self.assertEqual(self.call(path, "PUT", body)[0], expected, path)
        self.ddb.transact_write_items.assert_not_called()

    def test_revoke(self):
        self.assertEqual(self.call(f"users/{ALICE}/auto-rules/PURCHASE", "DELETE")[0], 404)
        self.table.items[("USER#" + ALICE, "AUTORULE#PURCHASE")] = {"PK": "USER#" + ALICE, "SK": "AUTORULE#PURCHASE",
                                                                    "module": "PURCHASE", "email": "alice@example.com"}
        self.assertEqual(self.call(f"users/{ALICE}/auto-rules/PURCHASE", "DELETE")[0], 200)


class FormTests(Harness):
    fields = [{"key": "amount", "label": "Total", "type": "amount", "required": True, "min": 0, "max": 100},
              {"key": "kind", "label": "Kind", "type": "select", "options": ["A", "B", "A"]}]

    def test_new_version_with_snapshot(self):
        status, body = self.call("forms/PURCHASE", "PUT", {"fields": self.fields, "approvalField": "amount", "version": 2})
        self.assertEqual(status, 200, body)
        (_, current, spec), (_, snapshot, _), (_, audit, _) = self.written()
        self.assertEqual((int(current["version"]), spec["ConditionExpression"]), (3, "#v = :v"))
        self.assertEqual(snapshot["SK"], "FORMVER#PURCHASE#000003")
        self.assertEqual(body["form"]["fields"][1]["options"], ["A", "B"])
        self.assertEqual(audit["action"], "FORM_SAVED")

    def test_definition_rules(self):
        bad = [
            {"fields": []},
            {"fields": [{"key": "Bad Key", "label": "x", "type": "text"}]},
            {"fields": [{"key": "title", "label": "x", "type": "text"}]},
            {"fields": [{"key": "a", "label": "x", "type": "colour"}]},
            {"fields": [{"key": "a", "label": "x", "type": "text"}, {"key": "a", "label": "y", "type": "text"}]},
            {"fields": [{"key": "a", "label": "x", "type": "select", "options": []}]},
            {"fields": [{"key": "a", "label": "x", "type": "number", "min": 5, "max": 1}]},
            {"fields": [{"key": "a", "label": "x", "type": "text", "required": True}], "approvalField": "a"},
            {"fields": [{"key": "a", "label": "x", "type": "number"}], "approvalField": "a"},
        ]
        for body in bad:
            self.assertEqual(self.call("forms/PURCHASE", "PUT", body)[0], 400, body)

    def test_stale_version_conflicts(self):
        self.assertEqual(self.call("forms/PURCHASE", "PUT", {"fields": self.fields, "approvalField": "amount", "version": 1})[0], 409)


class WorkflowTests(Harness):
    def test_save_manager_then_groups(self):
        levels = [{"kind": "MANAGER", "minAmount": 0}, {"kind": "GROUP", "groupId": "finance", "minAmount": 1000},
                  {"kind": "ADMINS", "minAmount": 10000}]
        status, body = self.call("workflows/PURCHASE", "PUT", {"levels": levels, "slaSeconds": 300, "escalationSeconds": 600})
        self.assertEqual(status, 200, body)
        self.assertIn("Line manager -> Finance when Total is 1,000 or more -> Admins when Total is 10,000 or more", body["message"])

    def test_rules(self):
        bad = [
            {"levels": []},
            {"levels": [{"kind": "GROUP", "groupId": "finance", "minAmount": 0}, {"kind": "MANAGER", "minAmount": 0}]},
            {"levels": [{"kind": "MANAGER", "minAmount": 50}]},
            {"levels": [{"kind": "MANAGER", "minAmount": 0}, {"kind": "GROUP", "groupId": "nope", "minAmount": 0}]},
            {"levels": [{"kind": "MANAGER", "minAmount": 0}, {"kind": "GROUP", "groupId": "finance"},
                        {"kind": "GROUP", "groupId": "finance"}]},
            {"levels": [{"kind": "MANAGER", "minAmount": 0}], "slaSeconds": 5},
            {"levels": [{"kind": "MANAGER", "minAmount": 0}] * 6},
        ]
        for body in bad:
            self.assertEqual(self.call("workflows/PURCHASE", "PUT", body)[0], 400, body)

    def test_thresholds_need_an_approval_field(self):
        self.table.items[("CONFIG", "FORM#PURCHASE")]["approvalField"] = None
        body = {"levels": [{"kind": "MANAGER", "minAmount": 0}, {"kind": "ADMINS", "minAmount": 10}]}
        self.assertEqual(self.call("workflows/PURCHASE", "PUT", body)[0], 400)


class GroupTests(Harness):
    def test_members_must_be_active_approvers(self):
        self.assertEqual(self.call("groups", "POST", {"name": "IT security", "members": [ALICE]})[0], 400)
        status, body = self.call("groups", "POST", {"name": "IT security", "members": [BOB, ADMIN, BOB]})
        self.assertEqual(status, 201, body)
        self.assertEqual((body["group"]["groupId"], body["group"]["members"]), ("it-security", [BOB, ADMIN]))
        self.assertEqual(self.call("groups", "POST", {"name": "Finance", "groupId": "finance"})[0], 409)

    def test_cannot_delete_a_group_a_workflow_uses(self):
        self.table.items[("CONFIG", "WORKFLOW#PURCHASE")] = {"PK": "CONFIG", "SK": "WORKFLOW#PURCHASE", "module": "PURCHASE",
                                                             "levels": [{"kind": "GROUP", "groupId": "finance"}]}
        status, body = self.call("groups/finance", "DELETE")
        self.assertEqual(status, 409)
        self.assertIn("Purchasing", body["message"])


class ImportTests(Harness):
    def test_dry_run_catches_loops_across_rows_and_unknown_people(self):
        rows = [{"email": "boss@example.com", "managerEmail": "bob@example.com"},  # Bob reports to boss already
                {"email": "ghost@example.com", "managerEmail": "bob@example.com"}]
        status, body = self.call("users/import", "POST", {"rows": rows})
        self.assertEqual(status, 422)
        errors = {r["email"]: r.get("error", "") for r in body["results"]}
        self.assertIn("loop", errors["boss@example.com"])
        self.assertIn("No user", errors["ghost@example.com"])
        self.ddb.transact_write_items.assert_not_called()

    def test_apply_updates_only_changed_people(self):
        rows = [{"email": "alice@example.com", "managerEmail": "bob@example.com", "department": "Sales"},
                {"email": "bob@example.com", "managerEmail": "boss@example.com"}]
        status, body = self.call("users/import", "POST", {"rows": rows, "dryRun": True})
        self.assertEqual((status, body["applied"]), (200, 0))
        status, body = self.call("users/import", "POST", {"rows": rows, "dryRun": False})
        self.assertEqual((status, body["applied"]), (200, 1), body)


class ConfigReadTests(Harness):
    def test_config_fills_unsaved_modules_with_defaults(self):
        status, body = self.call("config")
        self.assertEqual(status, 200)
        self.assertEqual(body["forms"]["PURCHASE"]["version"], 2)
        self.assertTrue(body["forms"]["LEAVE"]["unsaved"])
        self.assertEqual(body["workflows"]["PURCHASE"]["levels"][0]["kind"], "MANAGER")
        self.assertEqual([g["groupId"] for g in body["groups"]], ["finance"])


if __name__ == "__main__":
    unittest.main()
