"""POST /requests/{requestId}/decision

Checks that the caller may decide (active account, named as an approver of the
current level, not their own request, and not someone who already decided an
earlier level of it), then resumes the paused Step Functions execution with the
task token stored on the request. Step Functions accepts a token only once, which makes
double-clicks and races with the SLA timer safe without extra locking.
"""
import json
import os
import time

import boto3
from boto3.dynamodb.conditions import Key
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


def load_access(sub):
    """Live role and status from the PROFILE item that the user admin API maintains."""
    item = table().get_item(Key={"PK": f"USER#{sub}", "SK": "PROFILE"}, ConsistentRead=True).get("Item")
    if not item or item.get("status") != "ACTIVE":
        return None
    return {"role": item.get("role")}


def response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body),
    }


def prior_deciders(request_id):
    result = table().query(
        KeyConditionExpression=Key("PK").eq(f"REQ#{request_id}") & Key("SK").begins_with("APPROVAL#"),
        ConsistentRead=True,
    )
    return {str(i.get("decidedBy")) for i in result.get("Items", [])}


def authorise(item, sub, deciders):
    """Returns an error response, or None when the caller may decide."""
    if item is None:
        return response(404, {"message": "Request not found"})
    if item.get("status") not in DECISION_STATUSES:
        return response(409, {"message": f"Request is {item.get('status')}, so it is no longer waiting for a decision"})
    if item.get("requesterId") == sub:
        return response(403, {"message": "You cannot decide on your own request (segregation of duties)"})
    if sub not in {str(i) for i in (item.get("approverIds") or [])}:
        return response(403, {"message": f"This step is waiting for {item.get('approverGroup', 'another approver')}, not you"})
    if sub in deciders:
        return response(403, {"message": "You already decided an earlier level of this request, so someone else must decide this one"})
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
    if not sub:
        return response(401, {"message": "Unauthorized"})
    access = load_access(sub)
    if access is None:
        return response(403, {"message": "Your account is disabled or has no access set up yet. Ask an admin for access."})

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
    denied = authorise(item, sub, prior_deciders(request_id) if item else set())
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
