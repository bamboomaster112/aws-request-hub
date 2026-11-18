#!/usr/bin/env python3
"""End-to-end checks for Request Hub, run from CloudShell after seed-users.py.

Goes through the real edge (WAF -> API Gateway -> Cognito authorizer) and
watches the asynchronous parts (Pipes, EventBridge, Step Functions, SQS,
Lambda) by reading DynamoDB directly, so waiting costs no Lambda invocations.

  python3 scripts/smoke-test.py                 # core scenarios, about 1-2 minutes
  python3 scripts/smoke-test.py --audit         # also wait for the Firehose audit lake
  python3 scripts/smoke-test.py --escalation    # also wait out the approval SLA (about 4 minutes)
  python3 scripts/smoke-test.py --dlq           # also prove the fulfilment dead-letter path (about 4 minutes)
"""
import argparse
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


class Hub:
    def __init__(self):
        self.out = hubkit.outputs()
        self.api = self.out["ApiBaseUrl"]
        self.table = self.out["TableName"]
        self.ddb = hubkit.session.client("dynamodb")
        self.sqs = hubkit.session.client("sqs")
        self.s3 = hubkit.session.client("s3")

    def item(self, request_id):
        res = self.ddb.get_item(TableName=self.table, ConsistentRead=True,
                                Key={"PK": {"S": f"REQ#{request_id}"}, "SK": {"S": "META"}})
        return {k: next(iter(v.values())) for k, v in res.get("Item", {}).items()}

    def wait(self, request_id, statuses, timeout=60):
        wanted, deadline, current = set(statuses), time.time() + timeout, {}
        while time.time() < deadline:
            current = self.item(request_id)
            if current.get("status") in wanted:
                return current
            time.sleep(2)
        return current

    def create(self, token, **payload):
        return hubkit.http("POST", f"{self.api}/requests", token, payload)

    def decide(self, token, request_id, decision, comment=""):
        return hubkit.http("POST", f"{self.api}/requests/{request_id}/decision", token,
                           {"decision": decision, "comment": comment})


def section(title):
    print(f"\n\033[1m{title}\033[0m")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--escalation", action="store_true")
    parser.add_argument("--dlq", action="store_true")
    args = parser.parse_args()

    started = time.time()
    hub = Hub()
    users = hubkit.load_users()
    print(f"API: {hub.api}")
    print("Signing in test users with password + TOTP MFA...")
    tok = {role: hubkit.sign_in(hub.out, users[role]) for role in ("requester", "approver", "admin")}

    section("Edge security")
    status, _ = hubkit.http("GET", f"{hub.api}/requests")
    check("No token is rejected by the Cognito authorizer (401)", status == 401, status)
    status, body = hub.create(tok["requester"], type="PURCHASE", amount=10)
    check("Schema validation rejects a body without a title (400)", status == 400, f"{status} {body}")
    status, _ = hub.create(tok["requester"], type="GENERAL", title="x' UNION SELECT username, password FROM users --")
    check("WAF blocks SQL injection (403)", status == 403, status)
    status, _ = hub.create(tok["requester"], type="GENERAL", title="hello", description="<script>alert(document.cookie)</script>")
    check("WAF blocks cross-site scripting (403)", status == 403, status)
    status, page = hubkit.http("GET", f"{hub.api}/")
    check("Web app is served over HTTPS from S3 via API Gateway", status == 200 and "Request Hub" in str(page), status)

    section("Low risk: auto-approved and fulfilled with no human")
    status, body = hub.create(tok["requester"], type="PURCHASE", title="Smoke test: USB-C dock", amount=250, currency="USD")
    check("Functionless create returns 201", status == 201, f"{status} {body}")
    low = body["requestId"] if status == 201 else None
    if low:
        it = hub.wait(low, ["FULFILLED", "FAILED"], timeout=60)
        check("Pipes -> EventBridge -> Step Functions auto-approved it (LOW risk)", it.get("riskTier") == "LOW", it.get("riskTier"))
        check("EventBridge -> SQS -> worker fulfilled it with a PO reference",
              it.get("status") == "FULFILLED" and str(it.get("fulfilmentRef", "")).startswith("PO-"), it.get("status"))

    section("Medium risk: human approval with task token")
    status, body = hub.create(tok["requester"], type="PURCHASE", title="Smoke test: conference ticket", amount=1200, currency="USD")
    med = body.get("requestId") if status == 201 else None
    if check("Created", med, f"{status} {body}"):
        it = hub.wait(med, ["PENDING_APPROVAL", "FAILED"], timeout=45)
        check("Workflow paused in PENDING_APPROVAL for the approvers group",
              it.get("status") == "PENDING_APPROVAL" and it.get("approverGroup") == "approvers", f"{it.get('status')} {it.get('approverGroup')}")
        status, _ = hub.decide(tok["requester"], med, "APPROVED")
        check("Requester cannot approve (403)", status == 403, status)
        status, inbox = hubkit.http("GET", f"{hub.api}/approvals", tok["approver"])
        ids = [i.get("requestId") for i in (inbox or {}).get("items", [])] if status == 200 else []
        check("It appears in the approver's inbox", med in ids, f"{status}")
        status, body = hub.decide(tok["approver"], med, "APPROVED", "Within the training budget")
        check("Approver's decision accepted (202)", status == 202, f"{status} {body}")
        status, body = hub.decide(tok["approver"], med, "APPROVED", "double click")
        check("A second decision is refused because the token was already used (409)", status == 409, f"{status} {body}")
        it = hub.wait(med, ["FULFILLED", "FAILED"], timeout=60)
        check("Approved, then fulfilled", it.get("status") == "FULFILLED" and it.get("decidedByEmail") == users["approver"]["username"],
              f"{it.get('status')} {it.get('decidedByEmail')}")

    section("Rejection needs a reason")
    status, body = hub.create(tok["requester"], type="ACCESS", title="Smoke test: production database read access", amount=30)
    rej = body.get("requestId") if status == 201 else None
    if check("Created", rej, f"{status} {body}"):
        hub.wait(rej, ["PENDING_APPROVAL"], timeout=45)
        status, _ = hub.decide(tok["approver"], rej, "REJECTED", "")
        check("Rejecting without a comment is refused (400)", status == 400, status)
        status, _ = hub.decide(tok["approver"], rej, "REJECTED", "Use the read replica instead")
        it = hub.wait(rej, ["REJECTED", "FAILED"], timeout=45)
        check("Rejected with the approver's note", it.get("status") == "REJECTED" and it.get("decisionComment") == "Use the read replica instead",
              it.get("status"))

    section("High risk and segregation of duties")
    status, body = hub.create(tok["requester"], type="CHANGE", title="Smoke test: rotate production TLS certificates", amount=0)
    high = body.get("requestId") if status == 201 else None
    if check("Created", high, f"{status} {body}"):
        it = hub.wait(high, ["PENDING_APPROVAL", "FAILED"], timeout=45)
        check("Routed to admins as HIGH risk", it.get("riskTier") == "HIGH" and it.get("approverGroup") == "admins",
              f"{it.get('riskTier')} {it.get('approverGroup')}")
        status, _ = hub.decide(tok["approver"], high, "APPROVED")
        check("An approver (not admin) is refused (403)", status == 403, status)
        status, _ = hub.decide(tok["admin"], high, "APPROVED", "Change window confirmed")
        it = hub.wait(high, ["FULFILLED", "FAILED"], timeout=60)
        check("Admin approved and it was fulfilled (CHG reference)",
              it.get("status") == "FULFILLED" and str(it.get("fulfilmentRef", "")).startswith("CHG-"), it.get("status"))
    status, body = hub.create(tok["approver"], type="ACCESS", title="Smoke test: approver asks for own access", amount=5)
    own = body.get("requestId") if status == 201 else None
    if own:
        hub.wait(own, ["PENDING_APPROVAL"], timeout=45)
        status, _ = hub.decide(tok["approver"], own, "APPROVED")
        check("An approver cannot approve their own request (403)", status == 403, status)
        hub.decide(tok["admin"], own, "REJECTED", "Smoke test clean-up")

    section("Attachments: presigned S3 POST")
    if low:
        status, body = hubkit.http("POST", f"{hub.api}/requests/{low}/attachments", tok["requester"],
                                   {"fileName": "quote.txt", "contentType": "text/plain"})
        if check("Presigned upload policy issued (201)", status == 201, f"{status} {body}"):
            content = b"Quote 42: one USB-C dock, 250 USD\n"
            data, ctype = hubkit.multipart(body["upload"]["fields"], "file", "quote.txt", content, "text/plain")
            status, _ = hubkit.http("POST", body["upload"]["url"], raw=data, headers={"Content-Type": ctype})
            check("Browser-style upload straight to S3 (204)", status in (200, 201, 204), status)
            status, detail = hubkit.http("GET", f"{hub.api}/requests/{low}", tok["requester"])
            atts = (detail or {}).get("attachments", []) if status == 200 else []
            ok = bool(atts) and atts[-1].get("uploaded") and atts[-1].get("downloadUrl")
            check("Request detail lists it with a short-lived download link", ok, f"{status}")
            if ok:
                with urllib.request.urlopen(atts[-1]["downloadUrl"], timeout=20) as res:
                    check("Downloaded content matches (SSE-KMS at rest)", res.read() == content)
        status, body = hubkit.http("POST", f"{hub.api}/requests/{low}/attachments", tok["approver"],
                                   {"fileName": "x.txt", "contentType": "text/plain"})
        check("Someone else cannot attach files to the request (404)", status == 404, status)

    if args.escalation:
        section("SLA escalation (waits for the approval timer)")
        status, body = hub.create(tok["requester"], type="ACCESS", title="Smoke test: escalation drill", amount=1)
        esc = body.get("requestId") if status == 201 else None
        if esc:
            it = hub.wait(esc, ["PENDING_APPROVAL"], timeout=45)
            sla = int(it.get("slaSeconds", 180))
            print(f"  waiting about {sla}s for the SLA to expire...")
            it = hub.wait(esc, ["ESCALATED"], timeout=sla + 60)
            check("Missed SLA escalated the request to admins", it.get("status") == "ESCALATED" and it.get("approverGroup") == "admins",
                  it.get("status"))
            status, _ = hub.decide(tok["admin"], esc, "APPROVED", "Approved after escalation")
            it = hub.wait(esc, ["FULFILLED", "APPROVED"], timeout=60)
            check("Admin resolved the escalation", it.get("status") in ("APPROVED", "FULFILLED"), it.get("status"))

    if args.dlq:
        section("Fulfilment failure lands in the dead-letter queue")
        status, body = hub.create(tok["requester"], type="GENERAL", title="[fail] Smoke test: downstream outage", amount=50)
        bad = body.get("requestId") if status == 201 else None
        if bad:
            hub.wait(bad, ["AUTO_APPROVED"], timeout=45)
            print("  waiting for three failed receives (60 s visibility timeout each)...")
            deadline, depth = time.time() + 300, 0
            while time.time() < deadline and depth < 1:
                attrs = hub.sqs.get_queue_attributes(QueueUrl=hub.out["FulfilmentDlqUrl"],
                                                     AttributeNames=["ApproximateNumberOfMessages"])
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

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed in {int(time.time() - started)}s")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
