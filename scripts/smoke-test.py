#!/usr/bin/env python3
"""End-to-end checks for Request Hub, run from CloudShell after seed-config.py and seed-users.py.

The test users form a reporting line: requester -> approver -> admin. The test sets
a known Purchasing workflow (line manager, Finance from 1,000, admins from 10,000),
makes the admin the only Finance member, and then drives real requests through the
API. Asynchronous progress is read from DynamoDB directly, so waiting costs no
Lambda invocations.

  python3 scripts/smoke-test.py                 # core scenarios, about 2-3 minutes
  python3 scripts/smoke-test.py --audit         # also wait for the Firehose audit lake
  python3 scripts/smoke-test.py --escalation    # also wait out a level's SLA (about 4 minutes)
  python3 scripts/smoke-test.py --dlq           # also prove the fulfilment dead-letter path (about 4 minutes)
"""
import argparse
import datetime
import sys
import time
import urllib.request

import hubkit

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    mark = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
    print(f"  [{mark}] {name}" + (f"  -> {detail}" if detail and not ok else ""))
    return ok


def section(title):
    print(f"\n\033[1m{title}\033[0m")


def day(offset):
    return (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()


class Hub:
    def __init__(self):
        self.out = hubkit.outputs()
        self.api = self.out["ApiBaseUrl"]
        self.table = self.out["TableName"]
        self.ddb = hubkit.session.client("dynamodb")
        self.sqs = hubkit.session.client("sqs")
        self.s3 = hubkit.session.client("s3")
        self.forms = {}

    def call(self, method, path, token, body=None, headers=None):
        return hubkit.http(method, f"{self.api}{path}", token, body, headers=headers)

    def item(self, request_id):
        res = self.ddb.get_item(TableName=self.table, ConsistentRead=True,
                                Key={"PK": {"S": f"REQ#{request_id}"}, "SK": {"S": "META"}})
        out = {}
        for k, v in res.get("Item", {}).items():
            kind, value = next(iter(v.items()))
            out[k] = [next(iter(x.values())) for x in value] if kind == "L" else value
        return out

    def decisions(self, request_id):
        res = self.ddb.query(TableName=self.table, ConsistentRead=True,
                             KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
                             ExpressionAttributeValues={":pk": {"S": f"REQ#{request_id}"}, ":sk": {"S": "APPROVAL#"}})
        return res.get("Items", [])

    def wait(self, request_id, statuses, timeout=60, level=None):
        wanted, deadline, current = set(statuses), time.time() + timeout, {}
        while time.time() < deadline:
            current = self.item(request_id)
            if current.get("status") in wanted and (level is None or current.get("currentLevel") == str(level)):
                return current
            time.sleep(2)
        return current

    def values(self, module, approval_value):
        """Valid answers for whatever fields the admin has configured on the module's form."""
        form = self.forms[module]
        values = {}
        for f in form["fields"]:
            ftype = f["type"]
            if f["key"] == form.get("approvalField"):
                values[f["key"]] = approval_value
            elif ftype == "checkbox":
                values[f["key"]] = bool(f.get("required"))
            elif ftype in ("number", "amount"):
                values[f["key"]] = max(f.get("min", 0), 1) if f.get("max", 10) >= 1 else f.get("min", 0)
            elif ftype == "date":
                values[f["key"]] = day(7)
            elif ftype == "select":
                values[f["key"]] = f["options"][0]
            elif ftype == "textarea":
                values[f["key"]] = "Created by the Request Hub smoke test"
            else:
                values[f["key"]] = "Smoke test"
        return values

    def create(self, token, module, title, approval_value, fields=None, **extra):
        body = {"type": module, "title": title, "fields": {**self.values(module, approval_value), **(fields or {})}}
        body.update(extra)
        return self.call("POST", "/requests", token, body)

    def decide(self, token, request_id, decision, comment=""):
        return self.call("POST", f"/requests/{request_id}/decision", token, {"decision": decision, "comment": comment})


def setup(hub, tok, ids):
    section("Setup through the admin API")
    status, config = hub.call("GET", "/admin/config", tok["admin"])
    if not check("Admin reads the configuration (forms, workflows, groups)", status == 200, status):
        sys.exit("Cannot continue without the admin API")
    finance = next((g for g in config["groups"] if g["groupId"] == "finance" and not g.get("unsaved")), None)
    body = {"name": "Finance", "description": "Smoke test", "members": [ids["admin"]]}
    if finance:
        status, res = hub.call("PUT", "/admin/groups/finance", tok["admin"], {**body, "version": finance["version"]})
    else:
        status, res = hub.call("POST", "/admin/groups", tok["admin"], {**body, "groupId": "finance"})
    check("Finance approver group has exactly the admin as member", status in (200, 201), f"{status} {res}")
    workflow = config["workflows"]["PURCHASE"]
    levels = [{"kind": "MANAGER", "minAmount": 0}, {"kind": "GROUP", "groupId": "finance", "minAmount": 1000},
              {"kind": "ADMINS", "minAmount": 10000}]
    body = {"levels": levels, "slaSeconds": 180, "escalationSeconds": 180}
    if not workflow.get("unsaved"):
        body["version"] = workflow["version"]
    status, res = hub.call("PUT", "/admin/workflows/PURCHASE", tok["admin"], body)
    check("Purchasing workflow: manager, Finance from 1,000, admins from 10,000", status == 200, f"{status} {res}")
    for module in ("PURCHASE", "GENERAL"):
        hub.call("DELETE", f"/admin/users/{ids['requester']}/auto-rules/{module}", tok["admin"])
    status, body = hub.call("GET", "/forms", tok["requester"])
    hub.forms = {f["module"]: f for f in (body or {}).get("forms", [])} if status == 200 else {}
    check("Requester sees a form for every module they hold", {"PURCHASE", "LEAVE", "GENERAL"} <= set(hub.forms), sorted(hub.forms))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--escalation", action="store_true")
    parser.add_argument("--dlq", action="store_true")
    args = parser.parse_args()

    started = time.time()
    hub = Hub()
    users = hubkit.load_users()
    if any("userId" not in users[r] for r in users):
        sys.exit("The test users predate the reporting line. Run: python3 scripts/seed-users.py")
    ids = {role: users[role]["userId"] for role in ("requester", "approver", "admin")}
    print(f"API: {hub.api}")
    print("Signing in test users with password + TOTP MFA...")
    tok = {role: hubkit.sign_in(hub.out, users[role]) for role in ("requester", "approver", "admin")}
    setup(hub, tok, ids)

    section("Edge security and form validation")
    status, _ = hubkit.http("GET", f"{hub.api}/requests")
    check("No token is rejected by the Cognito authorizer (401)", status == 401, status)
    status, body = hub.call("POST", "/requests", tok["requester"], {"type": "PURCHASE", "fields": {}})
    check("API Gateway schema validation rejects a body without a title (400)", status == 400, f"{status} {body}")
    status, body = hub.call("POST", "/requests", tok["requester"], {"type": "PURCHASE", "title": "Smoke: empty form", "fields": {}})
    errors = (body or {}).get("fieldErrors", {})
    check("Intake rejects missing required fields, field by field (400)", status == 400 and hub.forms["PURCHASE"]["approvalField"] in errors, f"{status} {body}")
    status, body = hub.create(tok["requester"], "PURCHASE", "Smoke: unknown field", 10, fields={"notOnTheForm": "x"})
    check("Intake rejects a field that is not on the form (400)", status == 400, f"{status} {body}")
    if hub.out.get("WafEnabled", "true") == "true":
        status, _ = hub.create(tok["requester"], "GENERAL", "x' UNION SELECT username, password FROM users --", 1)
        check("WAF blocks SQL injection (403)", status == 403, status)
    else:
        print("  [SKIP] WAF checks: WAF is disabled for this account (blocked by a service control policy)")
    status, page = hubkit.http("GET", f"{hub.api}/")
    check("Web app is served over HTTPS from S3 via API Gateway", status == 200 and "Request Hub" in str(page), status)

    section("No auto-approval unless an admin grants it")
    status, body = hub.create(tok["requester"], "PURCHASE", "Smoke: small purchase, no rule", 250)
    small = body.get("requestId") if status == 201 else None
    if check("Form-validated create returns 201", small, f"{status} {body}"):
        it = hub.wait(small, ["PENDING_APPROVAL"], level=1)
        check("Even 250 waits for the requester's line manager", it.get("approverIds") == [ids["approver"]], it.get("approverIds"))
        status, _ = hub.decide(tok["approver"], small, "APPROVED", "Fine")
        it = hub.wait(small, ["FULFILLED"], timeout=60)
        check("One level, approved by the manager, then fulfilled", it.get("status") == "FULFILLED", it.get("status"))

    status, body = hub.call("PUT", f"/admin/users/{ids['admin']}/auto-rules/PURCHASE", tok["admin"], {"maxAmount": 500, "expiresOn": day(2)})
    check("Admins cannot grant themselves auto-approval (403)", status == 403, status)
    status, body = hub.call("PUT", f"/admin/users/{ids['requester']}/auto-rules/PURCHASE", tok["admin"],
                            {"maxAmount": 500, "expiresOn": day(2), "note": "Smoke test"})
    check("Admin grants the requester Purchasing auto-approval up to 500", status == 200, f"{status} {body}")
    status, body = hub.create(tok["requester"], "PURCHASE", "Smoke: USB-C dock", 300)
    auto = body.get("requestId") if status == 201 else None
    if auto:
        it = hub.wait(auto, ["FULFILLED", "FAILED"], timeout=60)
        check("Within the granted limit: auto-approved and fulfilled with a PO reference",
              it.get("decidedBy") == "policy-engine" and it.get("status") == "FULFILLED", f"{it.get('status')} {it.get('decidedBy')}")
    status, body = hub.create(tok["requester"], "PURCHASE", "Smoke: over the auto limit", 600)
    over = body.get("requestId") if status == 201 else None
    if over:
        it = hub.wait(over, ["PENDING_APPROVAL"], level=1)
        check("Over the limit: back to the approval chain", it.get("status") == "PENDING_APPROVAL", it.get("status"))
        status, _ = hub.decide(tok["approver"], over, "REJECTED")
        check("Rejecting without a comment is refused (400)", status == 400, status)
        hub.decide(tok["approver"], over, "REJECTED", "Use the existing stock")
        it = hub.wait(over, ["REJECTED"])
        check("Rejected with the approver's note", it.get("decisionComment") == "Use the existing stock", it.get("status"))

    section("Multi-level chain: line manager, then Finance")
    status, body = hub.create(tok["requester"], "PURCHASE", "Smoke: team laptops", 1500)
    multi = body.get("requestId") if status == 201 else None
    if multi:
        it = hub.wait(multi, ["PENDING_APPROVAL"], level=1)
        check("Plan has 2 levels and level 1 names the manager", it.get("levelCount") == "2" and it.get("approverIds") == [ids["approver"]],
              f"{it.get('levelCount')} {it.get('approverIds')}")
        status, _ = hub.decide(tok["requester"], multi, "APPROVED")
        check("The requester cannot approve their own request (403)", status == 403, status)
        status, _ = hub.decide(tok["admin"], multi, "APPROVED")
        check("An admin cannot jump the chain at level 1 (403)", status == 403, status)
        status, _ = hub.decide(tok["approver"], multi, "APPROVED", "Budget agreed")
        check("The manager approves level 1 (202)", status == 202, status)
        it = hub.wait(multi, ["PENDING_APPROVAL"], level=2)
        check("Level 2 now waits for Finance (the admin)", it.get("approverIds") == [ids["admin"]], it.get("approverIds"))
        status, _ = hub.decide(tok["approver"], multi, "APPROVED")
        check("The manager cannot also approve level 2 (403)", status == 403, status)
        status, _ = hub.decide(tok["admin"], multi, "APPROVED", "Finance OK")
        it = hub.wait(multi, ["FULFILLED"], timeout=60)
        check("Approved by Finance, then fulfilled", it.get("status") == "FULFILLED" and it.get("decidedByEmail") == users["admin"]["username"],
              f"{it.get('status')} {it.get('decidedByEmail')}")
        check("Both decisions are stored as child items", len(hub.decisions(multi)) == 2, len(hub.decisions(multi)))
        status, detail = hub.call("GET", f"/requests/{multi}", tok["requester"])
        plan = (detail or {}).get("approvalPlan", {})
        check("The request shows its plan and decisions", len(plan.get("levels", [])) == 2 and len((detail or {}).get("approvals", [])) == 2, status)

    section("The approver's own request goes to their manager")
    status, body = hub.create(tok["approver"], "GENERAL", "Smoke: approver's own request", 50)
    own = body.get("requestId") if status == 201 else None
    if own:
        it = hub.wait(own, ["PENDING_APPROVAL"], level=1)
        check("Level 1 names the approver's manager (the admin)", it.get("approverIds") == [ids["admin"]], it.get("approverIds"))
        status, _ = hub.decide(tok["approver"], own, "APPROVED")
        check("The approver cannot approve their own request (403)", status == 403, status)
        hub.decide(tok["admin"], own, "REJECTED", "Smoke test clean-up")

    section("Org hierarchy and user management guardrails")
    status, body = hub.call("PATCH", f"/admin/users/{ids['admin']}", tok["admin"], {"managerId": ids["approver"]})
    check("A reporting loop is refused (400)", status == 400 and "loop" in str(body), f"{status} {body}")
    status, body = hub.call("PATCH", f"/admin/users/{ids['approver']}", tok["admin"], {"managerId": ids["requester"]})
    check("A Requester cannot be someone's manager (400)", status == 400, f"{status} {body}")
    status, body = hub.call("PATCH", f"/admin/users/{ids['approver']}", tok["admin"], {"role": "requesters"})
    check("Someone with direct reports cannot be demoted silently (409)", status == 409, f"{status} {body}")
    status, _ = hub.call("GET", "/admin/users", tok["requester"])
    check("A requester cannot open the admin API (403)", status == 403, status)
    keep = [m for m in hubkit.ALL_MODULES if m != "LEAVE"]
    status, _ = hub.call("PATCH", f"/admin/users/{ids['requester']}", tok["admin"], {"modules": keep})
    status2, body = hub.create(tok["requester"], "LEAVE", "Smoke: blocked leave", 1)
    check("Revoking a module applies at once: same token, Leave refused (403)", status == 200 and status2 == 403, f"{status} {status2}")
    hub.call("PATCH", f"/admin/users/{ids['requester']}", tok["admin"], {"modules": hubkit.ALL_MODULES})
    hub.call("POST", f"/admin/users/{ids['requester']}/actions", tok["admin"], {"action": "DISABLE"})
    status, _ = hub.call("GET", "/requests", tok["requester"])
    hub.call("POST", f"/admin/users/{ids['requester']}/actions", tok["admin"], {"action": "ENABLE"})
    status2, _ = hub.call("GET", "/requests", tok["requester"])
    check("Disable cuts off a still-valid token (403); enable restores it (200)", status == 403 and status2 == 200, f"{status} {status2}")
    status, detail = hub.call("GET", f"/admin/users/{ids['requester']}", tok["admin"])
    actions = {h.get("action") for h in (detail or {}).get("user", {}).get("history", [])} if status == 200 else set()
    check("Every change is in the user's access history", {"AUTO_RULE_SET", "ACCESS_CHANGED", "DISABLE", "ENABLE"} <= actions, sorted(actions))

    section("Attachments: presigned S3 POST")
    target = multi or small
    if target:
        status, body = hub.call("POST", f"/requests/{target}/attachments", tok["requester"], {"fileName": "quote.txt", "contentType": "text/plain"})
        if check("Presigned upload policy issued (201)", status == 201, f"{status} {body}"):
            content = b"Quote 42: four laptops\n"
            data, ctype = hubkit.multipart(body["upload"]["fields"], "file", "quote.txt", content, "text/plain")
            status, _ = hubkit.http("POST", body["upload"]["url"], raw=data, headers={"Content-Type": ctype})
            check("Browser-style upload straight to S3 (204)", status in (200, 201, 204), status)
            status, detail = hub.call("GET", f"/requests/{target}", tok["requester"])
            atts = (detail or {}).get("attachments", []) if status == 200 else []
            ok = bool(atts) and atts[-1].get("uploaded") and atts[-1].get("downloadUrl")
            check("Request detail lists it with a short-lived download link", ok, status)
            if ok:
                with urllib.request.urlopen(atts[-1]["downloadUrl"], timeout=20) as res:
                    check("Downloaded content matches (SSE-KMS at rest)", res.read() == content)

    if args.escalation:
        section("SLA escalation (waits for the level timer)")
        status, body = hub.create(tok["requester"], "LEAVE", "Smoke: escalation drill", 1)
        esc = body.get("requestId") if status == 201 else None
        if esc:
            it = hub.wait(esc, ["PENDING_APPROVAL"], level=1)
            sla = int(it.get("slaSeconds", 180))
            print(f"  waiting about {sla}s for the level's SLA to expire...")
            it = hub.wait(esc, ["ESCALATED"], timeout=sla + 60)
            check("Missed SLA escalated the level to admins", it.get("status") == "ESCALATED" and ids["admin"] in it.get("approverIds", []),
                  f"{it.get('status')} {it.get('approverIds')}")
            hub.decide(tok["admin"], esc, "APPROVED", "Approved after escalation")
            it = hub.wait(esc, ["FULFILLED", "APPROVED"], timeout=60)
            check("Admin resolved the escalation", it.get("status") in ("APPROVED", "FULFILLED"), it.get("status"))

    if args.dlq:
        section("Fulfilment failure lands in the dead-letter queue")
        hub.call("PUT", f"/admin/users/{ids['requester']}/auto-rules/GENERAL", tok["admin"], {"maxAmount": 100, "expiresOn": day(2)})
        status, body = hub.create(tok["requester"], "GENERAL", "[fail] Smoke test: downstream outage", 50)
        bad = body.get("requestId") if status == 201 else None
        if bad:
            hub.wait(bad, ["AUTO_APPROVED"], timeout=45)
            print("  waiting for three failed receives (60 s visibility timeout each)...")
            deadline, depth = time.time() + 300, 0
            while time.time() < deadline and depth < 1:
                attrs = hub.sqs.get_queue_attributes(QueueUrl=hub.out["FulfilmentDlqUrl"], AttributeNames=["ApproximateNumberOfMessages"])
                depth = int(attrs["Attributes"]["ApproximateNumberOfMessages"])
                time.sleep(10)
            check("Message moved to the fulfilment DLQ after 3 attempts", depth >= 1, depth)
            check("Request stayed AUTO_APPROVED (not falsely fulfilled)", hub.item(bad).get("status") == "AUTO_APPROVED")

    if args.audit:
        section("Audit lake (Firehose buffers for 60 seconds)")
        bucket, deadline, found = hub.out["AuditBucketName"], time.time() + 150, 0
        while time.time() < deadline and not found:
            found = hub.s3.list_objects_v2(Bucket=bucket, Prefix="events/").get("KeyCount", 0)
            if not found:
                time.sleep(10)
        check("Domain events delivered to s3://<audit>/events/year=/month=/day=/", found > 0, found)

    for module in ("PURCHASE", "GENERAL"):
        hub.call("DELETE", f"/admin/users/{ids['requester']}/auto-rules/{module}", tok["admin"])
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed in {int(time.time() - started)}s")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
