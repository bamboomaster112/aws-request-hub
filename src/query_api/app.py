"""Read side of the Request Hub API.

GET /requests               the caller's own requests (GSI1)
GET /requests/{requestId}   one request plus attachment download links
GET /approvals              requests waiting for the caller at their current approval level
GET /me                     the caller's live access profile (role, modules, status)
GET /forms                  the request forms the caller may use, with each module's approval route

API Gateway has already validated the Cognito token. Authorisation then uses the
caller's live PROFILE item (role, modules, status), which admins manage, so a
revoked module or a disabled account stops working on the very next call.
Who may decide is explicit on the request: the current level's approverIds,
written by the workflow from the approval plan. The task token never leaves AWS.
"""
import json
import os
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-east-1")
HIDDEN_FIELDS = {"PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK", "approvalToken", "entity", "executionArn"}
DECISION_STATUSES = ("PENDING_APPROVAL", "ESCALATED")
DOWNLOAD_URL_SECONDS = 300
MODULE_LABELS = {
    "PURCHASE": "Purchasing",
    "ACCESS": "Access management",
    "LEAVE": "Leave",
    "CHANGE": "Change management",
    "GENERAL": "General requests",
}
NO_ACCESS = "Your account is disabled or has no access set up yet. Ask an admin for access."

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


def load_access(sub):
    """The caller's PROFILE item: role, modules and status, maintained by the user admin API."""
    item = table().get_item(Key={"PK": f"USER#{sub}", "SK": "PROFILE"}, ConsistentRead=True).get("Item")
    if not item:
        return None
    return {
        "role": item.get("role"),
        "modules": {str(m) for m in (item.get("modules") or [])},
        "status": item.get("status"),
        "email": item.get("email", ""),
        "name": item.get("name", ""),
        "managerId": item.get("managerId"),
        "department": item.get("department", ""),
    }


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


def approver_ids(item):
    return {str(i) for i in (item.get("approverIds") or [])}


def can_decide(item, sub, deciders=()):
    """Only the people named on the current level, never the requester, never someone who decided an earlier level."""
    if item.get("status") not in DECISION_STATUSES or item.get("requesterId") == sub:
        return False
    return sub in approver_ids(item) and sub not in set(deciders)


def parse_json(text, default):
    if not isinstance(text, str):
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def planned_people(plan):
    people = set()
    for level in (plan or {}).get("levels", []):
        people.update(level.get("approverIds") or [])
    people.update((plan or {}).get("escalateTo") or [])
    return people


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
    plan = parse_json(meta.get("approvalPlan"), {})
    approvals = sorted((clean(i) for i in items if str(i.get("SK", "")).startswith("APPROVAL#")),
                       key=lambda a: (a.get("level", 0), a.get("at", "")))
    deciders = [a.get("decidedBy") for a in approvals]
    is_reviewer = ("admins" in groups or sub in approver_ids(meta) or sub in planned_people(plan) or sub in deciders)
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
    body.pop("approverIds", None)
    body["approvalPlan"] = plan
    body["approvals"] = approvals
    body["attachments"] = attachments
    body["canDecide"] = can_decide(meta, sub, deciders)
    body["isOwner"] = is_owner
    return response(200, body)


def list_approvals(sub, groups):
    if not groups & {"approvers", "admins"}:
        return response(403, {"message": "Only approvers and admins have an approval inbox"})
    statuses = ["PENDING_APPROVAL", "ESCALATED"]
    items = []
    for status in statuses:
        result = table().query(
            IndexName="GSI2",
            KeyConditionExpression=Key("GSI2PK").eq(f"STATUS#{status}"),
            ScanIndexForward=True,
            Limit=50,
        )
        items.extend(result.get("Items", []))
    inbox = []
    for item in items:
        if can_decide(item, sub):
            entry = clean(item)
            entry.pop("approverIds", None)
            inbox.append(entry)
    return response(200, {"items": inbox})


def describe_route(workflow, form, groups, manager):
    field_label = next((f.get("label") for f in form.get("fields", []) if f.get("key") == form.get("approvalField")), "the value")
    steps = []
    for level in workflow.get("levels", []):
        kind = level.get("kind")
        if kind == "MANAGER":
            who = f"your line manager ({manager.get('name') or manager.get('email')})" if manager else "your line manager (none set, so admins)"
        elif kind == "GROUP":
            who = (groups.get(level.get("groupId")) or {}).get("name", level.get("groupId"))
        else:
            who = "Admins"
        minimum = _plain(level.get("minAmount") or 0)
        steps.append({
            "text": f"{who[0].upper()}{who[1:]}" + (f", when {field_label} is {minimum:,} or more" if minimum else ""),
            "minAmount": minimum,
        })
    return steps


def list_forms(sub, access):
    allowed = set(MODULE_LABELS) if access.get("role") == "admins" else access["modules"] & set(MODULE_LABELS)
    config = table().query(KeyConditionExpression=Key("PK").eq("CONFIG"), ConsistentRead=True)
    items = config.get("Items", [])
    while "LastEvaluatedKey" in config:
        config = table().query(KeyConditionExpression=Key("PK").eq("CONFIG"), ConsistentRead=True,
                               ExclusiveStartKey=config["LastEvaluatedKey"])
        items.extend(config.get("Items", []))
    forms = {i["module"]: i for i in items if str(i.get("SK", "")).startswith("FORM#")}
    workflows = {i["module"]: i for i in items if str(i.get("SK", "")).startswith("WORKFLOW#")}
    groups = {i["groupId"]: i for i in items if str(i.get("SK", "")).startswith("GROUP#")}
    manager = None
    if access.get("managerId"):
        manager = table().get_item(Key={"PK": f"USER#{access['managerId']}", "SK": "PROFILE"}).get("Item")
        if manager and manager.get("status") != "ACTIVE":
            manager = None
    rules = table().query(KeyConditionExpression=Key("PK").eq(f"USER#{sub}") & Key("SK").begins_with("AUTORULE#")).get("Items", [])
    now = int(time.time() * 1000)
    auto = {r["module"]: r for r in rules if int(r.get("expiresAt") or 0) > now}
    out = []
    for module in MODULE_LABELS:
        if module not in allowed or module not in forms:
            continue
        form = forms[module]
        workflow = workflows.get(module) or {"levels": [{"kind": "MANAGER", "minAmount": 0}]}
        entry = {
            "module": module,
            "label": MODULE_LABELS[module],
            "version": _plain(form.get("version") or 0),
            "fields": _plain(form.get("fields") or []),
            "approvalField": form.get("approvalField"),
            "route": describe_route(workflow, form, groups, manager),
        }
        if module in auto:
            entry["autoApproval"] = {"maxAmount": _plain(auto[module]["maxAmount"]), "expiresOn": auto[module].get("expiresOn")}
        out.append(entry)
    return response(200, {"forms": out})


def me(sub, email, access):
    access = access or {"status": "NO_PROFILE", "modules": set()}
    role = access.get("role")
    modules = sorted(MODULE_LABELS) if role == "admins" else sorted(access["modules"] & set(MODULE_LABELS))
    return response(200, {
        "userId": sub,
        "email": access.get("email") or email,
        "name": access.get("name", ""),
        "role": role,
        "status": access.get("status"),
        "modules": modules,
        "moduleCatalogue": [{"id": k, "label": v} for k, v in MODULE_LABELS.items()],
        "canManageUsers": role == "admins" and access.get("status") == "ACTIVE",
        "hasManager": bool(access.get("managerId")),
        "department": access.get("department", ""),
    })


def handler(event, _context):
    sub, email, _token_groups = caller(event)
    if not sub:
        return response(401, {"message": "Unauthorized"})
    resource, method = event.get("resource"), event.get("httpMethod")
    try:
        access = load_access(sub)
        if resource == "/me" and method == "GET":
            return me(sub, email, access)
        if not access or access.get("status") != "ACTIVE":
            return response(403, {"message": NO_ACCESS})
        groups = {access["role"]}
        if resource == "/requests" and method == "GET":
            return list_mine(sub)
        if resource == "/requests/{requestId}" and method == "GET":
            return get_one(event["pathParameters"]["requestId"], sub, groups)
        if resource == "/approvals" and method == "GET":
            return list_approvals(sub, groups)
        if resource == "/forms" and method == "GET":
            return list_forms(sub, access)
    except ClientError as exc:
        print(json.dumps({"level": "ERROR", "message": "dynamodb error", "error": str(exc)}))
        return response(500, {"message": "Could not read requests, try again"})
    return response(404, {"message": "Not found"})
