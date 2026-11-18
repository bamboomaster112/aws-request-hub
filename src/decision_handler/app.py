"""POST /requests/{requestId}/decision

Checks that the caller may decide (right group, not their own request, still
pending), then resumes the paused Step Functions execution with the task token
stored on the request. Step Functions accepts a token only once, which makes
double-clicks and races with the SLA timer safe without extra locking.
"""
import json
import os
import time

import boto3
from botocore.exceptions import ClientError

DECISION_STATUSES = ("PENDING_APPROVAL", "ESCALATED")
CLOSED_TOKEN_ERRORS = {"TaskTimedOut", "InvalidToken", "TaskDoesNotExist"}

_table = None
_sfn = None


def table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def sfn():
    global _sfn
    if _sfn is None:
        _sfn = boto3.client("stepfunctions")
    return _sfn


def parse_groups(raw):
    if isinstance(raw, (list, tuple, set)):
        return {str(g).strip() for g in raw if str(g).strip()}
    text = str(raw or "").strip().strip("[]")
    return {g.strip() for g in text.replace(",", " ").split() if g.strip()}


def response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body),
    }


def authorise(item, sub, groups):
    """Returns an error response, or None when the caller may decide."""
    if item is None:
        return response(404, {"message": "Request not found"})
    if item.get("status") not in DECISION_STATUSES:
        return response(409, {"message": f"Request is {item.get('status')}, so it is no longer waiting for a decision"})
    if item.get("requesterId") == sub:
        return response(403, {"message": "You cannot decide on your own request (segregation of duties)"})
    required = item.get("approverGroup", "approvers")
    if "admins" not in groups and required not in groups:
        return response(403, {"message": f"This request needs someone in the '{required}' group"})
    if not item.get("approvalToken"):
        return response(409, {"message": "The approval window is not open yet, refresh in a moment"})
    return None


def _emit_metric(decision):
    print(json.dumps({
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": os.environ.get("METRICS_NAMESPACE", "RequestHub"),
                "Dimensions": [["Decision"]],
                "Metrics": [{"Name": "Decisions", "Unit": "Count"}],
            }],
        },
        "Decision": decision,
        "Decisions": 1,
    }))


def handler(event, _context):
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    sub, email = claims.get("sub"), claims.get("email", "")
    groups = parse_groups(claims.get("cognito:groups"))
    if not sub:
        return response(401, {"message": "Unauthorized"})

    request_id = (event.get("pathParameters") or {}).get("requestId", "")
    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        return response(400, {"message": "Body must be JSON"})

    decision = str(body.get("decision", "")).upper()
    comment = str(body.get("comment") or "").strip()[:500]
    if decision not in ("APPROVED", "REJECTED"):
        return response(400, {"message": "decision must be APPROVED or REJECTED"})
    if decision == "REJECTED" and not comment:
        return response(400, {"message": "Please add a comment explaining the rejection"})

    item = table().get_item(Key={"PK": f"REQ#{request_id}", "SK": "META"}, ConsistentRead=True).get("Item")
    denied = authorise(item, sub, groups)
    if denied:
        return denied

    output = {"decision": decision, "decidedBy": sub, "decidedByEmail": email, "comment": comment}
    try:
        sfn().send_task_success(taskToken=item["approvalToken"], output=json.dumps(output))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in CLOSED_TOKEN_ERRORS:
            return response(409, {
                "message": "This approval window has closed. It may have been escalated or decided already, so refresh and try again."
            })
        raise

    _emit_metric(decision)
    print(json.dumps({"level": "INFO", "message": "decision recorded", "requestId": request_id,
                      "decision": decision, "decidedBy": sub}))
    return response(202, {"requestId": request_id, "decision": decision, "status": "DECISION_RECORDED"})
