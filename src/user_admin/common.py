"""Shared plumbing for the admin API: clients, errors, serialisation, history and events."""
import json
import os
import re
import time
import uuid
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

DIRECTORY_KEY = "DIRECTORY#USERS"
AUTORULES_KEY = "DIRECTORY#AUTORULES"
CONFIG_PK = "CONFIG"
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
LINE_CHARS = re.compile(r"[\x00-\x1f\x7f]")

_clients = {}
_serializer = TypeSerializer()


class ApiError(Exception):
    def __init__(self, status, message, details=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


# ---------------------------------------------------------------- clients
def _client(name):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def cognito():
    return _client("cognito-idp")


def ddb():
    return _client("dynamodb")


def events():
    return _client("events")


def table():
    if "table" not in _clients:
        _clients["table"] = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _clients["table"]


def pool():
    return os.environ["USER_POOL_ID"]


# ---------------------------------------------------------------- helpers
def response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body, default=str),
    }


def now_ms():
    return int(time.time() * 1000)


def plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (set, frozenset)):
        return sorted(plain(v) for v in value)
    if isinstance(value, list):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    return value


def to_ddb(value):
    """Python value -> something boto3 can store (floats become Decimal)."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_ddb(v) for v in value]
    return value


def clean_text(value, limit, multiline=False):
    pattern = CONTROL_CHARS if multiline else LINE_CHARS
    return pattern.sub("", str(value or "")).strip()[:limit]


def strip_keys(item, *keys):
    item = dict(item)
    for key in ("PK", "SK", "entity", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK") + keys:
        item.pop(key, None)
    return item


def serialise(item):
    out = {}
    for k, v in item.items():
        if v is None or v == set() or (isinstance(v, set) and not v):
            continue
        out[k] = _serializer.serialize(to_ddb(v))
    return out


# ---------------------------------------------------------------- directory reads
def profile_key(user_id):
    return {"PK": f"USER#{user_id}", "SK": "PROFILE"}


def load_profile(user_id):
    item = table().get_item(Key=profile_key(user_id), ConsistentRead=True).get("Item")
    return plain(item) if item else None


def query_all(**kwargs):
    items = []
    while True:
        page = table().query(**kwargs)
        items.extend(plain(i) for i in page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def list_profiles():
    return query_all(IndexName="GSI2", KeyConditionExpression=Key("GSI2PK").eq(DIRECTORY_KEY))


def config_items(prefix):
    return query_all(KeyConditionExpression=Key("PK").eq(CONFIG_PK) & Key("SK").begins_with(prefix), ConsistentRead=True)


def load_config(sk):
    item = table().get_item(Key={"PK": CONFIG_PK, "SK": sk}, ConsistentRead=True).get("Item")
    return plain(item) if item else None


# ---------------------------------------------------------------- writes
def put_op(item, condition=None, names=None, values=None):
    op = {"TableName": os.environ["TABLE_NAME"], "Item": serialise(item)}
    if condition:
        op["ConditionExpression"] = condition
    if names:
        op["ExpressionAttributeNames"] = names
    if values:
        op["ExpressionAttributeValues"] = {k: _serializer.serialize(to_ddb(v)) for k, v in values.items()}
    return {"Put": op}


def versioned_put(item, expected_version, create=False):
    """Optimistic locking: the write only lands on the version the admin was looking at."""
    if create:
        return put_op(item, "attribute_not_exists(PK)")
    return put_op(item, "#v = :v", {"#v": "version"}, {":v": int(expected_version or 0)})


def delete_op(key, condition=None):
    op = {"TableName": os.environ["TABLE_NAME"], "Key": serialise(key)}
    if condition:
        op["ConditionExpression"] = condition
    return {"Delete": op}


def audit_op(pk, actor, action, summary, before=None, after=None, subject=None):
    at = now_ms()
    item = {
        "PK": pk,
        "SK": f"AUDIT#{at:013d}#{uuid.uuid4().hex[:6]}",
        "entity": "ACCESS_AUDIT",
        "action": action,
        "summary": summary,
        "actorId": actor["userId"],
        "actorEmail": actor.get("email", ""),
        "at": at,
        "subject": subject,
        "before": json.dumps(before, default=str) if before is not None else None,
        "after": json.dumps(after, default=str) if after is not None else None,
    }
    if pk.startswith("USER#"):
        item["userId"] = pk[5:]
    return put_op(item, "attribute_not_exists(PK)")


def transact(ops, conflict_message="Someone else changed this a moment ago. Reload and try again."):
    """All writes succeed together or none do."""
    try:
        ddb().transact_write_items(TransactItems=ops)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            raise ApiError(409, conflict_message) from exc
        raise


def history(pk, limit=25):
    page = table().query(
        KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with("AUDIT#"),
        ScanIndexForward=False,
        Limit=limit,
    )
    out = []
    for item in page.get("Items", []):
        entry = strip_keys(plain(item))
        for key in ("before", "after"):
            if isinstance(entry.get(key), str):
                try:
                    entry[key] = json.loads(entry[key])
                except ValueError:
                    pass
        out.append(entry)
    return out


def publish(source, detail_type, detail):
    try:
        result = events().put_events(Entries=[{
            "Source": source,
            "DetailType": detail_type,
            "Detail": json.dumps(detail, default=str),
            "EventBusName": os.environ.get("EVENT_BUS_NAME", "default"),
        }])
        if result.get("FailedEntryCount"):
            print(json.dumps({"level": "WARN", "message": "event not published", "result": result.get("Entries")}))
    except ClientError as exc:
        # DynamoDB history is the system of record; the event is a copy for the audit lake and alerts.
        print(json.dumps({"level": "WARN", "message": "event not published", "error": str(exc)}))
