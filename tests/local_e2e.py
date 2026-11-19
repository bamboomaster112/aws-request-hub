#!/usr/bin/env python3
"""Local end-to-end run of the approval workflow, without an AWS account.

DynamoDB, Cognito, EventBridge and SSM are emulated by moto; the real Lambda handlers
run in-process; and a small interpreter executes statemachine/approval-workflow.asl.json
(the subset this workflow uses: Task, Choice, Map, Pass, Fail, Succeed, Catch,
ResultSelector, ResultPath, waitForTaskToken and the States.Format, States.JsonToString
and States.ArrayGetItem intrinsics). Real DynamoDB expressions are evaluated by moto,
so a typo in an UpdateExpression or a missing attribute value fails here.

  pip install "moto[dynamodb,cognitoidp,events,ssm]"
  python3 tests/local_e2e.py

It is not part of the unit test run (the file name does not start with test_).
"""
import datetime
import json
import os
import pathlib
import re
import sys
import uuid

os.environ.update({"AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
                   "TABLE_NAME": "hub", "USER_POOL_ID": "pool", "POLICY_PARAMETER": "/hub/policy", "EVENT_BUS_NAME": "default",
                   "ATTACHMENTS_BUCKET": "att"})

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from moto import mock_aws  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from helpers import load  # noqa: E402

ASL = json.loads((ROOT / "statemachine" / "approval-workflow.asl.json").read_text()
                 .replace("${TableName}", "hub").replace("${PolicyEngineFunctionArn}", "policy-engine"))
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not ok else ""))


# ---------------------------------------------------------------- a small ASL interpreter
class StatesError(Exception):
    def __init__(self, error, cause=""):
        super().__init__(error)
        self.error, self.cause = error, cause


def get_path(data, path, ctx):
    root, rest = (ctx, path[2:]) if path.startswith("$$") else (data, path[1:])
    for part in [p for p in re.split(r"\.", rest) if p]:
        root = root[part]
    return root


def split_args(text):
    args, depth, cur, quoted = [], 0, "", False
    for i, c in enumerate(text):
        if c == "'" and (i == 0 or text[i - 1] != "\\"):
            quoted = not quoted
        if not quoted and c == "(":
            depth += 1
        if not quoted and c == ")":
            depth -= 1
        if c == "," and depth == 0 and not quoted:
            args.append(cur.strip())
            cur = ""
            continue
        cur += c
    if cur.strip():
        args.append(cur.strip())
    return args


def evaluate(expr, data, ctx):
    expr = expr.strip()
    match = re.match(r"^(States\.\w+)\((.*)\)$", expr, re.S)
    if match:
        name, args = match.group(1), [evaluate(a, data, ctx) for a in split_args(match.group(2))]
        if name == "States.Format":
            template, values = args[0], args[1:]
            for value in values:
                template = template.replace("{}", value if isinstance(value, str) else json.dumps(value), 1)
            return template
        if name == "States.JsonToString":
            return json.dumps(args[0], separators=(",", ":"))
        if name == "States.ArrayGetItem":
            return args[0][int(args[1])]
        raise NotImplementedError(name)
    if expr.startswith("'"):
        return expr[1:-1].replace("\\'", "'")
    if expr.startswith("$"):
        return get_path(data, expr, ctx)
    return json.loads(expr)


def resolve(template, data, ctx):
    if isinstance(template, dict):
        out = {}
        for key, value in template.items():
            if key.endswith(".$"):
                out[key[:-2]] = evaluate(value, data, ctx)
            else:
                out[key] = resolve(value, data, ctx)
        return out
    if isinstance(template, list):
        return [resolve(v, data, ctx) for v in template]
    return template


def set_result(data, path, result):
    if path is None:
        return data
    if path in ("$", None) or path == "":
        return result
    data = json.loads(json.dumps(data))
    node, parts = data, path[2:].split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = result
    return data


def call_dynamodb(action, params):
    client = boto3.client("dynamodb")
    try:
        getattr(client, action)(**params)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        raise StatesError(f"DynamoDB.{code}", exc.response["Error"].get("Message", "")) from exc
    return {}


def run(machine, data, ctx, handlers):
    """Generator: yields ('wait', token, timeout) at each waitForTaskToken and receives the outcome."""
    name = machine["StartAt"]
    while True:
        state = machine["States"][name]
        kind = state["Type"]
        ctx = {**ctx, "State": {"EnteredTime": datetime.datetime.utcnow().isoformat() + "Z", "Name": name}}
        try:
            if kind == "Pass":
                result = resolve(state["Parameters"], data, ctx) if "Parameters" in state else data
                data = set_result(data, state.get("ResultPath", "$"), result)
            elif kind == "Choice":
                for rule in state["Choices"]:
                    value = get_path(data, rule["Variable"], ctx)
                    if ("BooleanEquals" in rule and value is rule["BooleanEquals"]) or \
                            ("StringEquals" in rule and value == rule["StringEquals"]):
                        name = rule["Next"]
                        break
                else:
                    name = state["Default"]
                continue
            elif kind == "Fail":
                error = state.get("Error") or get_path(data, state["ErrorPath"], ctx)
                cause = state.get("Cause") or (get_path(data, state["CausePath"], ctx) if "CausePath" in state else "")
                raise StatesError(error, cause)
            elif kind == "Succeed":
                return data
            elif kind == "Map":
                items, outputs = get_path(data, state["ItemsPath"], ctx), []
                for item in items:
                    item_ctx = {**ctx, "Map": {"Item": {"Value": item}}}
                    iteration = resolve(state["ItemSelector"], data, item_ctx)
                    outputs.append((yield from run(state["ItemProcessor"], iteration, item_ctx, handlers)))
                data = set_result(data, state.get("ResultPath", "$"), outputs)
            elif kind == "Task":
                resource = state["Resource"]
                token = None
                if resource.endswith(".waitForTaskToken"):
                    token = uuid.uuid4().hex
                    ctx = {**ctx, "Task": {"Token": token}}
                params = resolve(state["Parameters"], data, ctx)
                if resource == "arn:aws:states:::lambda:invoke":
                    result = {"Payload": json.loads(json.dumps(handlers["policy"](params["Payload"], None)))}
                else:
                    action = {"updateItem": "update_item", "putItem": "put_item"}[resource.split(":")[-1].split(".")[0]]
                    result = call_dynamodb(action, params)
                if token:
                    timeout = int(get_path(data, state["TimeoutSecondsPath"], ctx))
                    outcome = yield ("wait", token, timeout)
                    if outcome == "timeout":
                        raise StatesError("States.Timeout")
                    result = outcome
                if "ResultSelector" in state:
                    result = resolve(state["ResultSelector"], result, ctx)
                data = set_result(data, state.get("ResultPath", "$"), result)
            else:
                raise NotImplementedError(kind)
        except StatesError as err:
            catcher = next((c for c in state.get("Catch", [])
                            if err.error in c["ErrorEquals"] or "States.ALL" in c["ErrorEquals"]), None)
            if not catcher:
                raise
            data = set_result(data, catcher.get("ResultPath", "$"), {"Error": err.error, "Cause": err.cause})
            name = catcher["Next"]
            continue
        if state.get("End"):
            return data
        name = state["Next"]


class Workflows:
    """Plays the Step Functions service: start executions, accept SendTaskSuccess, fire timeouts."""

    def __init__(self, handlers):
        self.handlers, self.waiting, self.finished = handlers, {}, {}

    def _advance(self, request_id, gen, value):
        try:
            step = gen.send(value)
            self.waiting[request_id] = (gen, step[1])
        except StopIteration as stop:
            self.waiting.pop(request_id, None)
            self.finished[request_id] = ("SUCCEEDED", stop.value)
        except StatesError as err:
            self.waiting.pop(request_id, None)
            self.finished[request_id] = ("FAILED", f"{err.error}: {err.cause}")

    def start(self, detail):
        gen = run(ASL, {"source": "request-hub.requests", "detail-type": "Request Submitted", "detail": detail},
                  {"Execution": {"Id": f"exec-{detail['requestId']}"}}, self.handlers)
        self._advance(detail["requestId"], gen, None)

    def send_task_success(self, taskToken, output):  # noqa: N803 (boto3 argument names)
        for request_id, (gen, token) in list(self.waiting.items()):
            if token == taskToken:
                self._advance(request_id, gen, json.loads(output))
                return {}
        raise ClientError({"Error": {"Code": "TaskTimedOut", "Message": "closed"}}, "SendTaskSuccess")

    def time_out(self, request_id):
        gen, _ = self.waiting[request_id]
        self._advance(request_id, gen, "timeout")


# ---------------------------------------------------------------- the scenario
def api_event(sub, email, resource="/requests", method="POST", body=None, path=None):
    return {"resource": resource, "httpMethod": method, "headers": {}, "pathParameters": path or {},
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {"sub": sub, "email": email}}}}


def create_table():
    boto3.client("dynamodb").create_table(
        TableName="hub", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": n, "AttributeType": t} for n, t in
                              (("PK", "S"), ("SK", "S"), ("GSI1PK", "S"), ("GSI1SK", "N"), ("GSI2PK", "S"), ("GSI2SK", "N"))],
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        GlobalSecondaryIndexes=[{"IndexName": f"GSI{i}", "Projection": {"ProjectionType": "ALL"},
                                 "KeySchema": [{"AttributeName": f"GSI{i}PK", "KeyType": "HASH"}, {"AttributeName": f"GSI{i}SK", "KeyType": "RANGE"}]}
                                for i in (1, 2)])


def main():
    with mock_aws():
        create_table()
        boto3.client("ssm").put_parameter(Name="/hub/policy", Type="String", Value=json.dumps({"slaSeconds": 180, "escalationSeconds": 180}))
        table = boto3.resource("dynamodb").Table("hub")
        engine, intake, decision, query, publisher, admin = (load(n) for n in (
            "policy_engine", "request_intake", "decision_handler", "query_api", "stream_publisher", "user_admin"))
        import common  # noqa: E402
        import defaults  # noqa: E402
        flows = Workflows({"policy": engine.handler})
        decision.sfn = lambda: flows

        # People: requester -> approver -> admin; a second admin so the first one's own requests can route.
        ids = {"admin": "a" * 8 + "-0000-0000-0000-" + "1" * 12, "approver": "b" * 8 + "-0000-0000-0000-" + "2" * 12,
               "requester": "c" * 8 + "-0000-0000-0000-" + "3" * 12, "admin2": "d" * 8 + "-0000-0000-0000-" + "4" * 12}
        managers = {"approver": "admin", "requester": "approver"}
        roles = {"admin": "admins", "admin2": "admins", "approver": "approvers", "requester": "requesters"}
        for who, uid in ids.items():
            item = {"PK": f"USER#{uid}", "SK": "PROFILE", "entity": "USER_PROFILE", "userId": uid, "username": uid,
                    "email": f"{who}@example.com", "name": who.title(), "role": roles[who], "status": "ACTIVE", "version": 1,
                    "modules": set(defaults.MODULES), "createdAt": 1, "GSI2PK": "DIRECTORY#USERS", "GSI2SK": 1}
            if who in managers:
                item["managerId"] = ids[managers[who]]
            table.put_item(Item=item)
        for module, form in defaults.DEFAULT_FORMS.items():
            table.put_item(Item=json.loads(json.dumps({"PK": "CONFIG", "SK": f"FORM#{module}", "module": module, **form, "version": 1}),
                                           parse_float=__import__("decimal").Decimal))
        for group in defaults.DEFAULT_GROUPS:
            table.put_item(Item={"PK": "CONFIG", "SK": f"GROUP#{group['groupId']}", **group, "members": [], "version": 1})

        def as_admin(path, method, body=None):
            event = {"resource": "/admin/{proxy+}", "httpMethod": method, "pathParameters": {"proxy": path},
                     "body": json.dumps(body) if body is not None else None,
                     "requestContext": {"authorizer": {"claims": {"sub": ids["admin"], "email": "admin@example.com"}}}}
            res = admin.handler(event, None)
            return res["statusCode"], json.loads(res["body"])

        common.events = lambda: type("E", (), {"put_events": staticmethod(lambda **_: {"FailedEntryCount": 0})})()
        print("Admin API configuration")
        status, body = as_admin("groups/finance", "PUT", {"name": "Finance", "members": [ids["admin"]], "version": 1})
        check("Finance group gets the admin as its member", status == 200, body)
        levels = [{"kind": "MANAGER", "minAmount": 0}, {"kind": "GROUP", "groupId": "finance", "minAmount": 1000},
                  {"kind": "ADMINS", "minAmount": 10000}]
        status, body = as_admin("workflows/PURCHASE", "PUT", {"levels": levels, "slaSeconds": 180, "escalationSeconds": 180})
        check("Purchasing workflow saved", status == 200, body)
        status, body = as_admin("workflows/GENERAL", "PUT", {"levels": [{"kind": "MANAGER", "minAmount": 0}]})
        check("General workflow saved", status == 200, body)

        def submit(who, module, title, value):
            form = defaults.DEFAULT_FORMS[module]
            fields = {}
            for f in form["fields"]:
                if f["key"] == form["approvalField"]:
                    fields[f["key"]] = value
                elif f["type"] == "select":
                    fields[f["key"]] = f["options"][0]
                elif f["type"] == "date":
                    fields[f["key"]] = "2026-12-01"
                elif f["type"] in ("number", "amount"):
                    fields[f["key"]] = max(f.get("min", 0), 1)
                elif f["type"] == "checkbox":
                    fields[f["key"]] = True
                else:
                    fields[f["key"]] = "Local run"
            res = intake.handler(api_event(ids[who], f"{who}@example.com", body={"type": module, "title": title, "fields": fields}), None)
            request_id = json.loads(res["body"])["requestId"]
            meta = boto3.client("dynamodb").get_item(TableName="hub", Key={"PK": {"S": f"REQ#{request_id}"}, "SK": {"S": "META"}})["Item"]
            entry = publisher.to_entry({"eventName": "INSERT", "dynamodb": {"NewImage": meta}})
            flows.start(json.loads(entry["Detail"]))
            return request_id

        def item(request_id):
            return table.get_item(Key={"PK": f"REQ#{request_id}", "SK": "META"}, ConsistentRead=True)["Item"]

        def decide(who, request_id, verdict, comment="ok"):
            res = decision.handler(api_event(ids[who], f"{who}@example.com", resource="/requests/{requestId}/decision",
                                             body={"decision": verdict, "comment": comment}, path={"requestId": request_id}), None)
            return res["statusCode"]

        def inbox(who):
            res = query.handler(api_event(ids[who], f"{who}@example.com", resource="/approvals", method="GET"), None)
            return [i["requestId"] for i in json.loads(res["body"]).get("items", [])]

        print("No auto-approval without a rule")
        r1 = submit("requester", "PURCHASE", "Small purchase", 250)
        it = item(r1)
        check("Waits at level 1 for the manager", it["status"] == "PENDING_APPROVAL" and it["approverIds"] == [ids["approver"]], it.get("status"))
        check("It is in the manager's inbox only", r1 in inbox("approver") and r1 not in inbox("admin"))
        check("Manager approves (202)", decide("approver", r1, "APPROVED") == 202)
        it = item(r1)
        check("Approved after one level, token and approvers cleared", it["status"] == "APPROVED" and "approvalToken" not in it and "approverIds" not in it, it["status"])
        check("decidedByEmail comes from the last level", it.get("decidedByEmail") == "approver@example.com", it.get("decidedByEmail"))

        print("Admin-granted auto-approval")
        status, body = as_admin(f"users/{ids['requester']}/auto-rules/PURCHASE", "PUT",
                                {"maxAmount": 500, "expiresOn": (datetime.date.today() + datetime.timedelta(days=3)).isoformat()})
        check("Rule granted", status == 200, body)
        r2 = submit("requester", "PURCHASE", "Within the limit", 300)
        check("Auto-approved", item(r2)["status"] == "AUTO_APPROVED", item(r2)["status"])
        r3 = submit("requester", "PURCHASE", "Over the limit", 600)
        check("Over the limit waits for the manager", item(r3)["status"] == "PENDING_APPROVAL")
        check("Rejecting needs a comment (400)", decide("approver", r3, "REJECTED", "") == 400)
        check("Rejected (202)", decide("approver", r3, "REJECTED", "No budget") == 202)
        check("Status REJECTED and the run ends cleanly", item(r3)["status"] == "REJECTED" and flows.finished[r3][0] == "SUCCEEDED", flows.finished.get(r3))

        print("Two levels: manager, then Finance")
        r4 = submit("requester", "PURCHASE", "Laptops", 1500)
        check("Level 1 of 2", item(r4)["currentLevel"] == 1 and item(r4)["levelCount"] == 2)
        check("Admin cannot jump the chain (403)", decide("admin", r4, "APPROVED") == 403)
        check("Manager approves level 1", decide("approver", r4, "APPROVED", "Agreed") == 202)
        it = item(r4)
        check("Level 2 waits for Finance (admin)", it["currentLevel"] == 2 and it["approverIds"] == [ids["admin"]], it.get("approverIds"))
        check("The manager cannot approve level 2 (403)", decide("approver", r4, "APPROVED") == 403)
        check("Finance approves", decide("admin", r4, "APPROVED", "Finance OK") == 202)
        it = item(r4)
        children = table.query(KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(f"REQ#{r4}"))["Items"]
        approvals = [c for c in children if c["SK"].startswith("APPROVAL#")]
        check("Approved with two decision items", it["status"] == "APPROVED" and len(approvals) == 2, (it["status"], len(approvals)))
        detail = json.loads(query.handler(api_event(ids["requester"], "r", resource="/requests/{requestId}", method="GET",
                                                    path={"requestId": r4}), None)["body"])
        check("Detail shows the plan and both decisions", len(detail["approvalPlan"]["levels"]) == 2 and len(detail["approvals"]) == 2)

        print("Escalation and expiry")
        r5 = submit("requester", "PURCHASE", "Slow approval", 1200)
        flows.time_out(r5)
        it = item(r5)
        check("Level 1 escalated to admins", it["status"] == "ESCALATED" and set(it["approverIds"]) == {ids["admin"], ids["admin2"]}, it.get("approverIds"))
        check("Escalated item is in an admin's inbox", r5 in inbox("admin2"))
        check("Admin2 approves the escalated level", decide("admin2", r5, "APPROVED") == 202)
        it = item(r5)
        check("Back to PENDING_APPROVAL at level 2", it["status"] == "PENDING_APPROVAL" and it["currentLevel"] == 2, it["status"])
        flows.time_out(r5)
        flows.time_out(r5)
        check("Two missed timers expire the request", item(r5)["status"] == "EXPIRED" and flows.finished[r5][0] == "SUCCEEDED", flows.finished.get(r5))

        print("The approver's own request and the top of the tree")
        r6 = submit("approver", "GENERAL", "Approver's own", 50)
        check("Goes to the approver's manager", item(r6)["approverIds"] == [ids["admin"]])
        check("Approver cannot approve it (403)", decide("approver", r6, "APPROVED") == 403)
        r7 = submit("admin", "GENERAL", "Admin's own", 50)
        check("Admin with no manager: the other admin stands in", item(r7)["approverIds"] == [ids["admin2"]], item(r7).get("approverIds"))
        table.update_item(Key={"PK": f"USER#{ids['admin2']}", "SK": "PROFILE"}, UpdateExpression="SET #s = :d",
                          ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":d": "DISABLED"})
        r8 = submit("admin", "GENERAL", "Nobody left", 50)
        check("Nobody eligible: UNROUTABLE, not stuck", item(r8)["status"] == "UNROUTABLE", item(r8)["status"])

        failed = [rid for rid, (state, _) in flows.finished.items() if state != "SUCCEEDED"]
        check("No execution failed", not failed, [flows.finished[r] for r in failed])
    print(f"\n{sum(results)}/{len(results)} local end-to-end checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
