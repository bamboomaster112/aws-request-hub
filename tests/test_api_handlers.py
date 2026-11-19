import json
import unittest
from decimal import Decimal
from unittest import mock

from botocore.exceptions import ClientError

from helpers import FakeTable, load, profile

query = load("query_api")
decision = load("decision_handler")
presigner = load("attachment_presigner")
worker = load("fulfilment_worker")
publisher = load("stream_publisher")


def api_event(sub="user-1", groups="", body=None, request_id="r1", resource="/requests/{requestId}", method="GET"):
    return {
        "resource": resource,
        "httpMethod": method,
        "pathParameters": {"requestId": request_id},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"claims": {"sub": sub, "email": f"{sub}@example.com", "cognito:groups": groups}}},
    }


class GroupParsingTests(unittest.TestCase):
    def test_all_shapes_of_the_groups_claim(self):
        for raw in (["approvers", "admins"], "[approvers, admins]", "approvers,admins", "approvers admins"):
            self.assertEqual(query.parse_groups(raw), {"approvers", "admins"}, raw)
        self.assertEqual(query.parse_groups(None), set())


class QueryApiTests(unittest.TestCase):
    def test_clean_hides_keys_and_token_and_converts_numbers(self):
        item = {"PK": "REQ#1", "SK": "META", "approvalToken": "secret", "amount": Decimal("12.5"),
                "createdAt": Decimal("1700000000000"), "policyReasons": '["a reason"]', "title": "x"}
        out = query.clean(item)
        self.assertNotIn("approvalToken", out)
        self.assertNotIn("PK", out)
        self.assertEqual(out["amount"], 12.5)
        self.assertEqual(out["createdAt"], 1700000000000)
        self.assertEqual(out["policyReasons"], ["a reason"])

    def test_can_decide_rules(self):
        item = {"status": "PENDING_APPROVAL", "requesterId": "alice", "approverIds": ["bob", "carol"], "type": "PURCHASE"}
        self.assertTrue(query.can_decide(item, "bob"))
        self.assertFalse(query.can_decide(item, "alice"))                      # own request
        self.assertFalse(query.can_decide(item, "dave"))                       # not on this level
        self.assertFalse(query.can_decide(item, "bob", deciders=["bob"]))      # decided an earlier level
        self.assertFalse(query.can_decide({**item, "status": "APPROVED"}, "bob"))
        self.assertTrue(query.can_decide({**item, "status": "ESCALATED"}, "carol"))

    def test_inbox_is_forbidden_for_requesters(self):
        table = FakeTable([profile("user-1")])
        with mock.patch.object(query, "table", return_value=table):
            res = query.handler(api_event(groups="requesters", resource="/approvals"), None)
        self.assertEqual(res["statusCode"], 403)

    def test_strangers_get_not_found_for_someone_elses_request(self):
        table = FakeTable([profile("mallory", role="approvers"),
                           {"PK": "REQ#r1", "SK": "META", "requesterId": "alice", "status": "PENDING_APPROVAL",
                            "approverIds": ["bob"], "approvalPlan": json.dumps({"levels": [{"approverIds": ["bob"]}]})}])
        with mock.patch.object(query, "table", return_value=table):
            res = query.handler(api_event(sub="mallory"), None)
        self.assertEqual(res["statusCode"], 404)

    def test_planned_approvers_see_the_request_and_the_chain(self):
        plan = {"levels": [{"index": 1, "label": "Line manager: Bob", "approverIds": ["bob"]},
                           {"index": 2, "label": "Finance", "approverIds": ["carol"]}], "escalateTo": ["admin"]}
        table = FakeTable([profile("carol", role="approvers"),
                           {"PK": "REQ#r1", "SK": "META", "requestId": "r1", "requesterId": "alice", "status": "PENDING_APPROVAL",
                            "approverIds": ["bob"], "approvalPlan": json.dumps(plan), "approvalToken": "secret"},
                           {"PK": "REQ#r1", "SK": "APPROVAL#1", "level": Decimal(1), "decision": "APPROVED", "decidedBy": "bob"}])
        with mock.patch.object(query, "table", return_value=table):
            res = query.handler(api_event(sub="carol"), None)
        body = json.loads(res["body"])
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual([l["label"] for l in body["approvalPlan"]["levels"]], ["Line manager: Bob", "Finance"])
        self.assertEqual(body["approvals"][0]["decidedBy"], "bob")
        self.assertFalse(body["canDecide"])  # level 1 is Bob's
        self.assertNotIn("approvalToken", body)
        self.assertNotIn("approverIds", body)

    def test_disabled_or_unprovisioned_users_are_refused_immediately(self):
        for table in (FakeTable([profile("user-1", status="DISABLED")]), FakeTable([])):
            with mock.patch.object(query, "table", return_value=table):
                res = query.handler(api_event(resource="/requests"), None)
            self.assertEqual(res["statusCode"], 403)

    def test_inbox_lists_only_requests_naming_the_caller(self):
        pending = [
            {"PK": "REQ#a", "SK": "META", "requestId": "a", "type": "PURCHASE", "status": "PENDING_APPROVAL",
             "requesterId": "alice", "approverIds": ["bob"], "GSI2PK": "STATUS#PENDING_APPROVAL"},
            {"PK": "REQ#b", "SK": "META", "requestId": "b", "type": "ACCESS", "status": "PENDING_APPROVAL",
             "requesterId": "alice", "approverIds": ["carol"], "GSI2PK": "STATUS#PENDING_APPROVAL"},
            {"PK": "REQ#c", "SK": "META", "requestId": "c", "type": "LEAVE", "status": "ESCALATED",
             "requesterId": "dave", "approverIds": ["bob", "erin"], "GSI2PK": "STATUS#ESCALATED"},
        ]
        table = FakeTable([profile("bob", role="approvers")] + pending)
        with mock.patch.object(query, "table", return_value=table):
            res = query.handler(api_event(sub="bob", resource="/approvals"), None)
        self.assertEqual(sorted(i["requestId"] for i in json.loads(res["body"])["items"]), ["a", "c"])

    def test_forms_only_for_held_modules_with_route_and_auto_rule(self):
        form = {"PK": "CONFIG", "SK": "FORM#LEAVE", "module": "LEAVE", "version": Decimal(4), "approvalField": "days",
                "fields": [{"key": "days", "label": "Working days", "type": "number", "required": True}]}
        other = {**form, "SK": "FORM#PURCHASE", "module": "PURCHASE"}
        workflow = {"PK": "CONFIG", "SK": "WORKFLOW#LEAVE", "module": "LEAVE",
                    "levels": [{"kind": "MANAGER", "minAmount": Decimal(0)}, {"kind": "GROUP", "groupId": "hr", "minAmount": Decimal(15)}]}
        group = {"PK": "CONFIG", "SK": "GROUP#hr", "groupId": "hr", "name": "HR"}
        me = {**profile("user-1", modules=("LEAVE",)), "managerId": "boss"}
        boss = profile("boss", role="approvers", email="boss@example.com")
        rule = {"PK": "USER#user-1", "SK": "AUTORULE#LEAVE", "module": "LEAVE", "maxAmount": Decimal(2),
                "expiresAt": Decimal(9_999_999_999_999), "expiresOn": "2286-11-20"}
        table = FakeTable([me, boss, form, other, workflow, group, rule])
        with mock.patch.object(query, "table", return_value=table):
            body = json.loads(query.handler(api_event(resource="/forms"), None)["body"])
        self.assertEqual([f["module"] for f in body["forms"]], ["LEAVE"])
        leave = body["forms"][0]
        self.assertEqual([r["text"] for r in leave["route"]], ["Your line manager (Boss)", "HR, when Working days is 15 or more"])
        self.assertEqual([r["minAmount"] for r in leave["route"]], [0, 15])
        self.assertEqual(leave["autoApproval"], {"maxAmount": 2, "expiresOn": "2286-11-20"})

    def test_me_reports_live_access(self):
        with mock.patch.object(query, "table", return_value=FakeTable([profile("user-1", modules=("LEAVE",))])):
            body = json.loads(query.handler(api_event(resource="/me"), None)["body"])
        self.assertEqual((body["role"], body["modules"], body["canManageUsers"]), ("requesters", ["LEAVE"], False))
        with mock.patch.object(query, "table", return_value=FakeTable([profile("user-1", role="admins", modules=())])):
            body = json.loads(query.handler(api_event(resource="/me"), None)["body"])
        self.assertEqual(len(body["modules"]), 5)
        self.assertTrue(body["canManageUsers"])
        with mock.patch.object(query, "table", return_value=FakeTable([])):
            body = json.loads(query.handler(api_event(resource="/me"), None)["body"])
        self.assertEqual(body["status"], "NO_PROFILE")


class DecisionHandlerTests(unittest.TestCase):
    pending = {"PK": "REQ#r1", "SK": "META", "type": "PURCHASE", "status": "PENDING_APPROVAL", "requesterId": "alice",
               "approverGroup": "Line manager: Bob", "approverIds": ["bob"], "approvalToken": "tok"}

    def run_handler(self, item, sub="bob", body=None, access=None, extra=()):
        people = [access or profile(sub, role="approvers")]
        table, sfn = FakeTable(people + ([item] if item else []) + list(extra)), mock.Mock()
        with mock.patch.object(decision, "table", return_value=table), mock.patch.object(decision, "sfn", return_value=sfn):
            res = decision.handler(api_event(sub=sub, body=body or {"decision": "APPROVED"},
                                             resource="/requests/{requestId}/decision", method="POST"), None)
        return res, sfn

    def test_named_approver_resumes_the_workflow(self):
        res, sfn = self.run_handler(self.pending)
        self.assertEqual(res["statusCode"], 202)
        output = json.loads(sfn.send_task_success.call_args.kwargs["output"])
        self.assertEqual((output["decision"], output["decidedBy"]), ("APPROVED", "bob"))

    def test_segregation_of_duties(self):
        res, sfn = self.run_handler({**self.pending, "approverIds": ["alice"]}, sub="alice")
        self.assertEqual(res["statusCode"], 403)
        sfn.send_task_success.assert_not_called()

    def test_someone_not_on_this_level_is_refused(self):
        res, sfn = self.run_handler(self.pending, sub="carol")
        self.assertEqual(res["statusCode"], 403)
        self.assertIn("Line manager: Bob", json.loads(res["body"])["message"])
        sfn.send_task_success.assert_not_called()

    def test_admins_cannot_jump_the_chain(self):
        res, sfn = self.run_handler(self.pending, sub="root", access=profile("root", role="admins"))
        self.assertEqual(res["statusCode"], 403)
        sfn.send_task_success.assert_not_called()

    def test_same_person_cannot_approve_two_levels(self):
        level2 = {**self.pending, "approverIds": ["bob", "carol"], "currentLevel": 2}
        earlier = {"PK": "REQ#r1", "SK": "APPROVAL#1", "decision": "APPROVED", "decidedBy": "bob"}
        res, sfn = self.run_handler(level2, extra=[earlier])
        self.assertEqual(res["statusCode"], 403)
        sfn.send_task_success.assert_not_called()

    def test_disabled_approver_is_refused(self):
        res, sfn = self.run_handler(self.pending, access=profile("bob", role="approvers", status="DISABLED"))
        self.assertEqual(res["statusCode"], 403)
        sfn.send_task_success.assert_not_called()

    def test_rejection_needs_a_comment(self):
        res, _ = self.run_handler(self.pending, body={"decision": "REJECTED", "comment": "  "})
        self.assertEqual(res["statusCode"], 400)

    def test_closed_token_becomes_conflict(self):
        table, sfn = FakeTable([{**self.pending, "approverIds": ["user-1"]}, profile("user-1", role="approvers")]), mock.Mock()
        sfn.send_task_success.side_effect = ClientError({"Error": {"Code": "TaskTimedOut", "Message": "x"}}, "SendTaskSuccess")
        with mock.patch.object(decision, "table", return_value=table), mock.patch.object(decision, "sfn", return_value=sfn):
            res = decision.handler(api_event(body={"decision": "APPROVED"}, method="POST"), None)
        self.assertEqual(res["statusCode"], 409)


class PresignerTests(unittest.TestCase):
    def test_file_names_are_sanitised(self):
        self.assertEqual(presigner.safe_file_name("../../etc/passwd"), "passwd")
        self.assertEqual(presigner.safe_file_name("my quote (final).pdf"), "my_quote__final_.pdf")
        self.assertEqual(presigner.safe_file_name(""), "file")

    def test_disallowed_type_is_rejected(self):
        with mock.patch.object(presigner, "table", return_value=FakeTable([profile("user-1")])):
            res = presigner.handler(api_event(body={"fileName": "x.exe", "contentType": "application/x-msdownload"},
                                              method="POST"), None)
        self.assertEqual(res["statusCode"], 400)

    def test_disabled_user_cannot_upload(self):
        with mock.patch.object(presigner, "table", return_value=FakeTable([profile("user-1", status="DISABLED")])):
            res = presigner.handler(api_event(body={"fileName": "a.pdf", "contentType": "application/pdf"},
                                              method="POST"), None)
        self.assertEqual(res["statusCode"], 403)


class WorkerTests(unittest.TestCase):
    def test_partial_batch_failure_reports_only_the_bad_message(self):
        good = {"messageId": "m1", "body": json.dumps({"detail": {"requestId": "good"}})}
        bad = {"messageId": "m2", "body": json.dumps({"detail": {"requestId": "bad"}})}
        items = {"good": {"requestId": "good", "type": "PURCHASE", "status": "APPROVED", "title": "ok"},
                 "bad": {"requestId": "bad", "type": "GENERAL", "status": "APPROVED", "title": "[fail] x"}}
        table = mock.Mock()
        table.get_item.side_effect = lambda Key, ConsistentRead: {"Item": items[Key["PK"][4:]]}
        with mock.patch.object(worker, "table", return_value=table), \
                mock.patch.object(worker, "downstream_credentials", return_value={"apiKey": "k"}):
            res = worker.handler({"Records": [good, bad]}, None)
        self.assertEqual(res, {"batchItemFailures": [{"itemIdentifier": "m2"}]})
        self.assertEqual(table.update_item.call_count, 1)

    def test_already_fulfilled_is_a_no_op(self):
        table = mock.Mock()
        table.get_item.return_value = {"Item": {"requestId": "r", "status": "FULFILLED"}}
        with mock.patch.object(worker, "table", return_value=table):
            worker.process(json.dumps({"detail": {"requestId": "r"}}))
        table.update_item.assert_not_called()


class StreamPublisherTests(unittest.TestCase):
    def record(self, name, new_status, old_status=None, sk="META"):
        new = {"SK": {"S": sk}, "requestId": {"S": "r1"}, "status": {"S": new_status}, "amount": {"N": "10"}}
        rec = {"eventName": name, "dynamodb": {"NewImage": new}}
        if old_status:
            rec["dynamodb"]["OldImage"] = {"SK": {"S": sk}, "status": {"S": old_status}}
        return rec

    def test_insert_and_status_change_become_events(self):
        import os
        os.environ["EVENT_BUS_NAME"] = "bus"
        self.assertEqual(publisher.to_entry(self.record("INSERT", "SUBMITTED"))["DetailType"], "Request Submitted")
        entry = publisher.to_entry(self.record("MODIFY", "APPROVED", "PENDING_APPROVAL"))
        self.assertEqual(json.loads(entry["Detail"])["previousStatus"], "PENDING_APPROVAL")

    def test_new_requests_start_the_workflow_once(self):
        import os
        os.environ["EVENT_BUS_NAME"] = "default"
        os.environ["STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:1:stateMachine:wf"
        sfn = mock.Mock()
        sfn.start_execution.side_effect = [None, ClientError({"Error": {"Code": "ExecutionAlreadyExists", "Message": "x"}}, "StartExecution")]
        entry = publisher.to_entry(self.record("INSERT", "SUBMITTED"))
        with mock.patch.object(publisher, "sfn", return_value=sfn):
            self.assertTrue(publisher.start_workflow(entry))
            self.assertTrue(publisher.start_workflow(entry))  # redelivery is harmless
            self.assertFalse(publisher.start_workflow(publisher.to_entry(self.record("MODIFY", "APPROVED", "PENDING_APPROVAL"))))
        self.assertEqual(sfn.start_execution.call_args.kwargs["name"], "r1")

    def test_a_new_approval_level_is_an_event(self):
        rec = self.record("MODIFY", "PENDING_APPROVAL", "PENDING_APPROVAL")
        rec["dynamodb"]["NewImage"]["currentLevel"] = {"N": "2"}
        rec["dynamodb"]["NewImage"]["levelCount"] = {"N": "3"}
        rec["dynamodb"]["OldImage"]["currentLevel"] = {"N": "1"}
        detail = json.loads(publisher.to_entry(rec)["Detail"])
        self.assertEqual((detail["level"], detail["levelCount"]), (2, 3))

    def test_noise_is_ignored(self):
        self.assertIsNone(publisher.to_entry(self.record("MODIFY", "APPROVED", "APPROVED")))
        self.assertIsNone(publisher.to_entry(self.record("INSERT", "SUBMITTED", sk="ATT#1")))


if __name__ == "__main__":
    unittest.main()
