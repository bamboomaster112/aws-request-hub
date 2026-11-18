"""Change-data-capture publisher (deployed with UseEventBridgePipes=false).

Turns DynamoDB stream records for request META items into the same
"Request Submitted" / "Request Status Changed" events that EventBridge Pipes
produces, so everything downstream is identical in both modes.

It also starts the approval workflow for new requests. In the KodeKloud
playground a compliance checker deletes EventBridge rules whose target is a
Step Functions state machine, so the workflow cannot be started by a rule.
The execution is named after the request ID, which makes the start
idempotent: a redelivered stream record cannot start a second workflow.
"""
import json
import os

import boto3
from botocore.exceptions import ClientError

SOURCE = "request-hub.requests"
_events = None
_sfn = None


def events():
    global _events
    if _events is None:
        _events = boto3.client("events")
    return _events


def sfn():
    global _sfn
    if _sfn is None:
        _sfn = boto3.client("stepfunctions")
    return _sfn


def _s(image, name, default=""):
    attr = image.get(name) or {}
    return attr.get("S", attr.get("N", default))


def to_entry(record):
    new = record.get("dynamodb", {}).get("NewImage") or {}
    old = record.get("dynamodb", {}).get("OldImage") or {}
    if _s(new, "SK") != "META":
        return None
    status, previous = _s(new, "status"), (_s(old, "status") if old else "NONE")
    if record.get("eventName") == "INSERT":
        detail_type = "Request Submitted"
    elif record.get("eventName") == "MODIFY" and status != previous:
        detail_type = "Request Status Changed"
    else:
        return None
    detail = {
        "version": "1",
        "requestId": _s(new, "requestId"),
        "type": _s(new, "type"),
        "amount": _s(new, "amount", "0"),
        "currency": _s(new, "currency"),
        "requesterId": _s(new, "requesterId"),
        "requesterEmail": _s(new, "requesterEmail"),
        "status": status,
        "previousStatus": previous,
        "riskTier": _s(new, "riskTier"),
        "approverGroup": _s(new, "approverGroup"),
    }
    return {"Source": SOURCE, "DetailType": detail_type, "Detail": json.dumps(detail),
            "EventBusName": os.environ["EVENT_BUS_NAME"]}


def start_workflow(entry):
    """Start the approval workflow with the same input shape an EventBridge rule would send."""
    arn = os.environ.get("STATE_MACHINE_ARN")
    if not arn or entry["DetailType"] != "Request Submitted":
        return False
    detail = json.loads(entry["Detail"])
    payload = {"source": SOURCE, "detail-type": entry["DetailType"], "detail": detail}
    try:
        sfn().start_execution(stateMachineArn=arn, name=detail["requestId"], input=json.dumps(payload))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ExecutionAlreadyExists":
            raise
    return True


def handler(event, _context):
    entries = [e for e in (to_entry(r) for r in event.get("Records", [])) if e]
    started = sum(1 for e in entries if start_workflow(e))
    for start in range(0, len(entries), 10):
        result = events().put_events(Entries=entries[start:start + 10])
        if result.get("FailedEntryCount"):
            raise RuntimeError(f"PutEvents failed for {result['FailedEntryCount']} entries")
    return {"published": len(entries), "workflowsStarted": started}
