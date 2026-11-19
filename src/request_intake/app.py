"""POST /requests: validate a submission against its module's form and store it.

The caller must have an active access profile that holds the module (admins hold
every module). The body is checked field by field against the module's current
form version, so whatever the browser does, only valid requests are stored.
The request keeps a snapshot of the fields (labels and values) and the form
version, so later form edits never change what an approver sees.

An Idempotency-Key header makes retries safe: the same key from the same user
always maps to the same request ID, so a double-click cannot create two requests.
"""
import datetime
import json
import os
import re
import time
import uuid
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

MODULES = {"PURCHASE", "ACCESS", "LEAVE", "CHANGE", "GENERAL"}
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
LINE_CHARS = re.compile(r"[\x00-\x1f\x7f]")
TEXT_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
NAMESPACE = uuid.UUID("6c1d7f3e-0d55-4c9b-9a8e-6a1f2f3a9b10")

_table = None


def table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body, default=str),
    }


def _num(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def validate(form, values):
    """Returns (clean_fields, errors). clean_fields is a list of {key, label, type, value}."""
    if not isinstance(values, dict):
        return [], {"_form": "fields must be an object"}
    definitions = form.get("fields") or []
    known = {f["key"] for f in definitions}
    errors = {k: "This field is not on the form" for k in values if k not in known}
    clean = []
    for field in definitions:
        key, ftype, label = field["key"], field["type"], field["label"]
        raw = values.get(key)
        empty = raw is None or (isinstance(raw, str) and not raw.strip())
        if ftype == "checkbox":
            if raw is not None and not isinstance(raw, bool):
                errors[key] = "Must be true or false"
                continue
            checked = raw is True
            if field.get("required") and not checked:
                errors[key] = "Please confirm this"
                continue
            clean.append({"key": key, "label": label, "type": ftype, "value": checked})
            continue
        if empty:
            if field.get("required"):
                errors[key] = "This field is required"
            continue
        if ftype in ("text", "textarea"):
            text = (TEXT_CHARS if ftype == "textarea" else LINE_CHARS).sub("", str(raw)).strip()
            limit = int(_num(field.get("maxLength") or (120 if ftype == "text" else 2000)))
            if len(text) > limit:
                errors[key] = f"Keep this to {limit} characters"
                continue
            clean.append({"key": key, "label": label, "type": ftype, "value": text})
        elif ftype in ("number", "amount"):
            if isinstance(raw, bool):
                errors[key] = "Enter a number"
                continue
            try:
                number = Decimal(str(raw))
            except Exception:  # noqa: BLE001 - any parse failure is a validation error
                errors[key] = "Enter a number"
                continue
            if not number.is_finite():
                errors[key] = "Enter a number"
                continue
            low, high = Decimal(str(_num(field.get("min", 0)))), Decimal(str(_num(field.get("max", 1_000_000))))
            if number < low or number > high:
                errors[key] = f"Enter a value between {_num(field.get('min', 0)):,} and {_num(field.get('max', 1_000_000)):,}"
                continue
            if ftype == "amount":
                number = number.quantize(Decimal("0.01"))
            clean.append({"key": key, "label": label, "type": ftype, "value": number})
        elif ftype == "date":
            text = str(raw).strip()
            try:
                if not DATE_RE.match(text):
                    raise ValueError
                datetime.date.fromisoformat(text)
            except ValueError:
                errors[key] = "Enter a date as YYYY-MM-DD"
                continue
            clean.append({"key": key, "label": label, "type": ftype, "value": text})
        elif ftype == "select":
            if str(raw) not in (field.get("options") or []):
                errors[key] = "Choose one of the listed options"
                continue
            clean.append({"key": key, "label": label, "type": ftype, "value": str(raw)})
        else:
            errors[key] = "This field type is not supported"
    return clean, errors


def handler(event, _context):
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    sub, email = claims.get("sub"), claims.get("email", "")
    if not sub:
        return response(401, {"message": "Unauthorized"})
    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        return response(400, {"message": "Body must be JSON"})
    if not isinstance(body, dict):
        return response(400, {"message": "Body must be a JSON object"})

    module = str(body.get("type", "")).upper()
    if module not in MODULES:
        return response(400, {"message": "Unknown request type"})
    try:
        profile = table().get_item(Key={"PK": f"USER#{sub}", "SK": "PROFILE"}, ConsistentRead=True).get("Item")
        if not profile or profile.get("status") != "ACTIVE":
            return response(403, {"message": "Your account is disabled or has no access set up yet. Ask an admin for access."})
        if profile.get("role") != "admins" and module not in (profile.get("modules") or set()):
            return response(403, {"message": "You do not have access to this request module. Ask an admin for access."})
        form = table().get_item(Key={"PK": "CONFIG", "SK": f"FORM#{module}"}, ConsistentRead=True).get("Item")
    except ClientError as exc:
        print(json.dumps({"level": "ERROR", "message": "dynamodb error", "error": str(exc)}))
        return response(500, {"message": "Could not check your access, try again"})
    if not form:
        return response(409, {"message": "This module has no form yet. Ask an admin to set it up."})

    title = LINE_CHARS.sub("", str(body.get("title") or "")).strip()
    errors = {}
    if not 3 <= len(title) <= 120:
        errors["title"] = "Give the request a title of 3 to 120 characters"
    fields, field_errors = validate(form, body.get("fields") or {})
    errors.update(field_errors)
    currency = str(body.get("currency") or "USD").upper()
    if not CURRENCY_RE.match(currency):
        errors["currency"] = "Use a three-letter currency code"
    if errors:
        return response(400, {"message": "Please fix the highlighted fields", "fieldErrors": errors})

    approval_key = form.get("approvalField")
    approval = next((f for f in fields if f["key"] == approval_key), None)
    amount = approval["value"] if approval else Decimal(0)

    key = (event.get("headers") or {}).get("Idempotency-Key") or (event.get("headers") or {}).get("idempotency-key")
    request_id = str(uuid.uuid5(NAMESPACE, f"{sub}:{key}")) if key and IDEMPOTENCY_RE.match(key) else str(uuid.uuid4())
    now = int(time.time() * 1000)
    item = {
        "PK": f"REQ#{request_id}",
        "SK": "META",
        "entity": "REQUEST",
        "requestId": request_id,
        "type": module,
        "title": title,
        "fields": fields,
        "formVersion": int(_num(form.get("version") or 0)),
        "amount": amount,
        "approvalField": approval["label"] if approval else None,
        "currency": currency,
        "requesterId": sub,
        "requesterEmail": profile.get("email") or email,
        "requesterName": profile.get("name") or "",
        "status": "SUBMITTED",
        "riskTier": "UNASSESSED",
        "approverGroup": "none",
        "createdAt": now,
        "GSI1PK": f"USER#{sub}",
        "GSI1SK": now,
        "GSI2PK": "STATUS#SUBMITTED",
        "GSI2SK": now,
    }
    item = {k: v for k, v in item.items() if v is not None}
    try:
        table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # Same idempotency key: the first submission already stored it.
            return response(200, {"requestId": request_id, "status": "SUBMITTED", "duplicate": True})
        print(json.dumps({"level": "ERROR", "message": "dynamodb error", "error": str(exc)}))
        return response(500, {"message": "Storage is unavailable, try again"})
    print(json.dumps({"level": "INFO", "message": "request stored", "requestId": request_id, "type": module,
                      "formVersion": item["formVersion"], "fields": len(fields)}))
    return response(201, {"requestId": request_id, "status": "SUBMITTED"})
