"""Read side of the Request Hub API.

GET /requests               the caller's own requests (GSI1)
GET /requests/{requestId}   one request plus attachment download links
GET /approvals              the approval inbox for approvers and admins (GSI2)

Authorisation is enforced here from the Cognito claims that API Gateway has
already validated. The approval task token never leaves this function.
"""
import json
import os
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-east-1")
HIDDEN_FIELDS = {"PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK", "approvalToken", "entity"}
DECISION_STATUSES = ("PENDING_APPROVAL", "ESCALATED")
DOWNLOAD_URL_SECONDS = 300

_table = None
_s3 = None


def table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client(
            "s3",
            region_name=REGION,
            endpoint_url=f"https://s3.{REGION}.amazonaws.com",
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
    return _s3


def parse_groups(raw):
    """Cognito groups arrive as a list, a JSON-ish "[a, b]" string, or "a,b"."""
    if isinstance(raw, (list, tuple, set)):
        return {str(g).strip() for g in raw if str(g).strip()}
    text = str(raw or "").strip().strip("[]")
    return {g.strip() for g in text.replace(",", " ").split() if g.strip()}


def caller(event):
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    return claims.get("sub"), claims.get("email", ""), parse_groups(claims.get("cognito:groups"))


def _plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def clean(item):
    out = {k: _plain(v) for k, v in item.items() if k not in HIDDEN_FIELDS}
    reasons = out.get("policyReasons")
    if isinstance(reasons, str):
        try:
            out["policyReasons"] = json.loads(reasons)
        except ValueError:
            out["policyReasons"] = [reasons]
    return out


def can_decide(item, sub, groups):
    if item.get("status") not in DECISION_STATUSES or item.get("requesterId") == sub:
        return False
    return "admins" in groups or item.get("approverGroup") in groups


def response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body, default=str),
    }


def list_mine(sub):
    result = table().query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(f"USER#{sub}"),
        ScanIndexForward=False,
        Limit=50,
    )
    return response(200, {"items": [clean(i) for i in result.get("Items", [])]})


def get_one(request_id, sub, groups):
    result = table().query(KeyConditionExpression=Key("PK").eq(f"REQ#{request_id}"), ConsistentRead=True)
    items = result.get("Items", [])
    meta = next((i for i in items if i.get("SK") == "META"), None)
    if meta is None:
        return response(404, {"message": "Request not found"})

    is_owner = meta.get("requesterId") == sub
    is_reviewer = bool(groups & {"approvers", "admins"})
    if not (is_owner or is_reviewer):
        # Same answer as "not found" so request IDs cannot be probed.
        return response(404, {"message": "Request not found"})

    attachments = []
    for att in sorted((i for i in items if str(i.get("SK", "")).startswith("ATT#")), key=lambda a: a.get("createdAt", 0)):
        entry = clean(att)
        entry.pop("key", None)
        try:
            s3().head_object(Bucket=os.environ["ATTACHMENTS_BUCKET"], Key=att["key"])
            entry["uploaded"] = True
            entry["downloadUrl"] = s3().generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": os.environ["ATTACHMENTS_BUCKET"],
                    "Key": att["key"],
                    "ResponseContentDisposition": f'attachment; filename="{att.get("fileName", "file")}"',
                },
                ExpiresIn=DOWNLOAD_URL_SECONDS,
            )
        except ClientError:
            entry["uploaded"] = False
        attachments.append(entry)

    body = clean(meta)
    body["attachments"] = attachments
    body["canDecide"] = can_decide(meta, sub, groups)
    body["isOwner"] = is_owner
    return response(200, body)


def list_approvals(sub, groups):
    if not groups & {"approvers", "admins"}:
        return response(403, {"message": "Only approvers and admins have an approval inbox"})
    statuses = ["PENDING_APPROVAL"] + (["ESCALATED"] if "admins" in groups else [])
    items = []
    for status in statuses:
        result = table().query(
            IndexName="GSI2",
            KeyConditionExpression=Key("GSI2PK").eq(f"STATUS#{status}"),
            ScanIndexForward=True,
            Limit=50,
        )
        items.extend(result.get("Items", []))
    inbox = [clean(i) for i in items if can_decide(i, sub, groups)]
    return response(200, {"items": inbox})


def handler(event, _context):
    sub, _email, groups = caller(event)
    if not sub:
        return response(401, {"message": "Unauthorized"})
    resource, method = event.get("resource"), event.get("httpMethod")
    try:
        if resource == "/requests" and method == "GET":
            return list_mine(sub)
        if resource == "/requests/{requestId}" and method == "GET":
            return get_one(event["pathParameters"]["requestId"], sub, groups)
        if resource == "/approvals" and method == "GET":
            return list_approvals(sub, groups)
    except ClientError as exc:
        print(json.dumps({"level": "ERROR", "message": "dynamodb error", "error": str(exc)}))
        return response(500, {"message": "Could not read requests, try again"})
    return response(404, {"message": "Not found"})
