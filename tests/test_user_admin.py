import datetime
import json
import os
import unittest
from unittest import mock

from botocore.exceptions import ClientError

from helpers import FakeTable, load, profile

os.environ.setdefault("USER_POOL_ID", "us-east-1_test")
admin_api = load("user_admin")

ADMIN = "11111111-1111-1111-1111-111111111111"
ALICE = "22222222-2222-2222-2222-222222222222"
BOB = "33333333-3333-3333-3333-333333333333"


def event(resource, method="GET", body=None, user_id=None, sub=ADMIN):
    return {
        "resource": resource,
        "httpMethod": method,
        "pathParameters": {"userId": user_id} if user_id else None,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"claims": {"sub": sub, "email": "boss@example.com"}}},
    }


def cognito_user(sub, email, status="CONFIRMED", enabled=True):
    return {"Username": sub, "Attributes": [{"Name": "sub", "Value": sub}, {"Name": "email", "Value": email}],
            "UserStatus": status, "Enabled": enabled, "UserCreateDate": datetime.datetime(2026, 11, 1)}


class Harness(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable([profile(ADMIN, role="admins", modules=(), email="boss@example.com"),
                                profile(ALICE, modules=("PURCHASE",))])
        self.table.query_items = [i for i in self.table.items.values()]
        self.cognito, self.ddb, self.events = mock.Mock(), mock.Mock(), mock.Mock()
        self.events.put_events.return_value = {"FailedEntryCount": 0}
        self.cognito.admin_get_user.return_value = {"Username": ALICE, "UserStatus": "CONFIRMED", "UserAttributes": []}
        patches = [mock.patch.object(admin_api, "table", return_value=self.table),
                   mock.patch.object(admin_api, "cognito", return_value=self.cognito),
                   mock.patch.object(admin_api, "ddb", return_value=self.ddb),
                   mock.patch.object(admin_api, "events", return_value=self.events)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def call(self, *args, **kwargs):
        res = admin_api.handler(event(*args, **kwargs), None)
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


class AuthorisationTests(Harness):
    def test_only_active_admins_get_in(self):
        status, _ = self.call("/admin/users", sub=ALICE)
        self.assertEqual(status, 403)
        self.table.items[("USER#" + ADMIN, "PROFILE")]["status"] = "DISABLED"
        status, _ = self.call("/admin/users")
        self.assertEqual(status, 403)

    def test_user_ids_are_validated(self):
        status, _ = self.call("/admin/users/{userId}", user_id="../../etc")
        self.assertEqual(status, 404)


class DirectoryTests(Harness):
    def test_list_merges_cognito_and_profiles(self):
        self.cognito.list_users.return_value = {"Users": [
            cognito_user(ADMIN, "boss@example.com"),
            cognito_user(ALICE, "alice@example.com"),
            cognito_user(BOB, "bob@example.com", status="FORCE_CHANGE_PASSWORD"),
        ]}
        status, body = self.call("/admin/users")
        self.assertEqual(status, 200)
        by_id = {u["userId"]: u for u in body["items"]}
        self.assertEqual(by_id[ALICE]["modules"], ["PURCHASE"])
        self.assertEqual(by_id[BOB]["status"], "NO_PROFILE")
        self.assertFalse(by_id[BOB]["managed"])
        self.assertEqual(body["summary"]["invitesPending"], 1)
        self.assertEqual(len(body["modules"]), 5)


class CreateTests(Harness):
    def created(self, sub=BOB, email="new@example.com"):
        self.cognito.admin_create_user.return_value = {"User": cognito_user(sub, email, status="FORCE_CHANGE_PASSWORD")}

    def test_validation(self):
        for body in ({"email": "nope", "role": "requesters"},
                     {"email": "a@b.co", "role": "superuser"},
                     {"email": "a@b.co", "role": "requesters", "modules": ["PAYROLL"]}):
            status, _ = self.call("/admin/users", "POST", body)
            self.assertEqual(status, 400, body)
        self.cognito.admin_create_user.assert_not_called()

    def test_invites_user_and_writes_profile_with_history(self):
        self.created()
        status, body = self.call("/admin/users", "POST", {"email": " New@Example.com ", "name": "New Person",
                                                          "role": "approvers", "modules": ["leave", "PURCHASE"]})
        self.assertEqual(status, 201, body)
        kwargs = self.cognito.admin_create_user.call_args.kwargs
        self.assertEqual(kwargs["Username"], "new@example.com")
        self.assertNotIn("MessageAction", kwargs)
        self.cognito.admin_add_user_to_group.assert_called_once_with(UserPoolId="us-east-1_test", Username=BOB, GroupName="approvers")
        (_, prof, spec), (_, audit, _) = self.written()
        self.assertEqual(spec["ConditionExpression"], "attribute_not_exists(PK)")
        self.assertEqual((prof["role"], sorted(prof["modules"]), prof["status"]), ("approvers", ["LEAVE", "PURCHASE"], "ACTIVE"))
        self.assertEqual(audit["action"], "CREATED")
        detail = json.loads(self.events.put_events.call_args.kwargs["Entries"][0]["Detail"])
        self.assertFalse(detail["privileged"])

    def test_create_without_email_and_privileged_alert(self):
        self.created()
        status, _ = self.call("/admin/users", "POST", {"email": "a@b.co", "role": "admins", "sendInvite": False})
        self.assertEqual(status, 201)
        self.assertEqual(self.cognito.admin_create_user.call_args.kwargs["MessageAction"], "SUPPRESS")
        detail = json.loads(self.events.put_events.call_args.kwargs["Entries"][0]["Detail"])
        self.assertTrue(detail["privileged"])

    def test_duplicate_email_is_a_conflict(self):
        self.cognito.admin_create_user.side_effect = ClientError({"Error": {"Code": "UsernameExistsException", "Message": "x"}}, "AdminCreateUser")
        status, _ = self.call("/admin/users", "POST", {"email": "a@b.co", "role": "requesters"})
        self.assertEqual(status, 409)

    def test_cognito_user_is_rolled_back_if_the_profile_write_fails(self):
        self.created()
        self.ddb.transact_write_items.side_effect = ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "TransactWriteItems")
        status, _ = self.call("/admin/users", "POST", {"email": "a@b.co", "role": "requesters"})
        self.assertEqual(status, 500)
        self.cognito.admin_delete_user.assert_called_once_with(UserPoolId="us-east-1_test", Username=BOB)


class UpdateTests(Harness):
    def test_grant_and_revoke_modules_with_optimistic_locking(self):
        status, body = self.call("/admin/users/{userId}", "PATCH", {"modules": ["LEAVE"], "version": 1}, user_id=ALICE)
        self.assertEqual(status, 200, body)
        (_, prof, spec), (_, audit, _) = self.written()
        self.assertEqual(spec["ConditionExpression"], "#v = :v")
        self.assertEqual(prof["modules"], ["LEAVE"])
        self.assertEqual(int(prof["version"]), 2)
        self.assertIn("granted Leave", audit["summary"])
        self.assertIn("revoked Purchasing", audit["summary"])
        self.cognito.admin_add_user_to_group.assert_not_called()  # role unchanged

    def test_revoking_every_module_removes_the_attribute(self):
        self.call("/admin/users/{userId}", "PATCH", {"modules": []}, user_id=ALICE)
        (_, prof, _), _ = self.written()
        self.assertNotIn("modules", prof)

    def test_stale_version_is_a_conflict(self):
        status, _ = self.call("/admin/users/{userId}", "PATCH", {"modules": [], "version": 7}, user_id=ALICE)
        self.assertEqual(status, 409)
        self.ddb.transact_write_items.assert_not_called()

    def test_role_change_moves_cognito_groups(self):
        status, _ = self.call("/admin/users/{userId}", "PATCH", {"role": "approvers"}, user_id=ALICE)
        self.assertEqual(status, 200)
        self.cognito.admin_add_user_to_group.assert_called_once_with(UserPoolId="us-east-1_test", Username=ALICE, GroupName="approvers")
        removed = {c.kwargs["GroupName"] for c in self.cognito.admin_remove_user_from_group.call_args_list}
        self.assertEqual(removed, {"requesters", "admins"})

    def test_admins_cannot_demote_themselves_or_the_last_admin(self):
        status, body = self.call("/admin/users/{userId}", "PATCH", {"role": "requesters"}, user_id=ADMIN)
        self.assertEqual(status, 400, body)
        self.table.items[("USER#" + BOB, "PROFILE")] = profile(BOB, role="admins")
        self.table.query_items = list(self.table.items.values())
        self.table.items[("USER#" + ADMIN, "PROFILE")]["status"] = "ACTIVE"
        status, _ = self.call("/admin/users/{userId}", "PATCH", {"role": "requesters"}, user_id=BOB)
        self.assertEqual(status, 200)  # another active admin remains

    def test_unmanaged_cognito_user_is_adopted(self):
        self.cognito.admin_get_user.return_value = {"Username": BOB, "Enabled": True, "UserAttributes": [
            {"Name": "sub", "Value": BOB}, {"Name": "email", "Value": "bob@example.com"}]}
        self.cognito.admin_list_groups_for_user.return_value = {"Groups": [{"GroupName": "approvers"}]}
        status, body = self.call("/admin/users/{userId}", "PATCH", {"modules": ["CHANGE"]}, user_id=BOB)
        self.assertEqual(status, 200, body)
        (_, prof, spec), (_, audit, _) = self.written()
        self.assertEqual((prof["role"], prof["modules"], audit["action"]), ("approvers", ["CHANGE"], "ACCESS_SET_UP"))
        self.assertEqual(spec["ConditionExpression"], "attribute_not_exists(PK)")


class ActionTests(Harness):
    def test_disable_blocks_sign_in_and_api_access(self):
        status, body = self.call("/admin/users/{userId}/actions", "POST", {"action": "DISABLE"}, user_id=ALICE)
        self.assertEqual(status, 200, body)
        self.cognito.admin_disable_user.assert_called_once()
        self.cognito.admin_user_global_sign_out.assert_called_once()
        (_, prof, _), (_, audit, _) = self.written()
        self.assertEqual((prof["status"], audit["action"]), ("DISABLED", "DISABLE"))

    def test_cannot_disable_yourself(self):
        status, _ = self.call("/admin/users/{userId}/actions", "POST", {"action": "DISABLE"}, user_id=ADMIN)
        self.assertEqual(status, 400)
        self.cognito.admin_disable_user.assert_not_called()

    def test_invite_and_reset_follow_the_sign_in_state(self):
        status, _ = self.call("/admin/users/{userId}/actions", "POST", {"action": "RESEND_INVITE"}, user_id=ALICE)
        self.assertEqual(status, 409)  # already signed in
        self.cognito.admin_get_user.return_value = {"Username": ALICE, "UserStatus": "FORCE_CHANGE_PASSWORD"}
        status, _ = self.call("/admin/users/{userId}/actions", "POST", {"action": "RESET_PASSWORD"}, user_id=ALICE)
        self.assertEqual(status, 409)  # never signed in
        status, _ = self.call("/admin/users/{userId}/actions", "POST", {"action": "RESEND_INVITE"}, user_id=ALICE)
        self.assertEqual(status, 200)
        self.assertEqual(self.cognito.admin_create_user.call_args.kwargs["MessageAction"], "RESEND")

    def test_unknown_action(self):
        status, _ = self.call("/admin/users/{userId}/actions", "POST", {"action": "PROMOTE"}, user_id=ALICE)
        self.assertEqual(status, 400)


class DeleteTests(Harness):
    def test_delete_keeps_history_and_blocks_self_delete(self):
        status, _ = self.call("/admin/users/{userId}", "DELETE", user_id=ADMIN)
        self.assertEqual(status, 400)
        status, _ = self.call("/admin/users/{userId}", "DELETE", user_id=ALICE)
        self.assertEqual(status, 200)
        kinds = [k for k, _, _ in self.written()]
        self.assertEqual(kinds, ["Delete", "Put"])
        self.cognito.admin_delete_user.assert_called_once()


if __name__ == "__main__":
    unittest.main()
