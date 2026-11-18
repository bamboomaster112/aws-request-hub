"""Fulfilment worker (SQS consumer).

Receives "Request Status Changed" events for approved requests and fulfils
them against a simulated downstream system (an ERP, ITSM or HR tool, say).
Events are thin, so the worker reads the full item itself. Processing is
idempotent, and partial batch responses mean one bad message is retried on
its own and, after three receives, lands in the dead-letter queue.

Put "[fail]" in a request title to simulate a downstream outage.
"""
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

REFERENCE_PREFIX = {"PURCHASE": "PO", "ACCESS": "ACC", "LEAVE": "LV", "CHANGE": "CHG", "GENERAL": "GEN"}
SECRET_CACHE_SECONDS = 300

_table = None
_secrets = None
_secret_cache = {"value": None, "loaded_at": 0.0}


def table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def downstream_credentials():
    global _secrets
    now = time.time()
    if _secret_cache["value"] is None or now - _secret_cache["loaded_at"] > SECRET_CACHE_SECONDS:
        if _secrets is None:
            _secrets = boto3.client("secretsmanager")
        raw = _secrets.get_secret_value(SecretId=os.environ["DOWNSTREAM_SECRET_ARN"])["SecretString"]
        _secret_cache["value"] = json.loads(raw)
        _secret_cache["loaded_at"] = now
    return _secret_cache["value"]


class DownstreamError(RuntimeError):
    pass


def fulfil(item):
    """Simulated downstream call. Returns a reference and a signed receipt."""
    if "[fail]" in str(item.get("title", "")).lower():
        raise DownstreamError("Simulated downstream outage (the title contains [fail])")
    creds = downstream_credentials()
    reference = f"{REFERENCE_PREFIX.get(item.get('type'), 'GEN')}-{item['requestId'][:8].upper()}"
    receipt = hmac.new(creds["apiKey"].encode(), reference.encode(), hashlib.sha256).hexdigest()[:16]
    return reference, receipt


def process(message_body):
    event = json.loads(message_body)
    request_id = event.get("detail", {}).get("requestId")
    if not request_id:
        print(json.dumps({"level": "WARN", "message": "event without requestId, skipping"}))
        return
    key = {"PK": f"REQ#{request_id}", "SK": "META"}
    item = table().get_item(Key=key, ConsistentRead=True).get("Item")
    if item is None:
        print(json.dumps({"level": "WARN", "message": "request not found, skipping", "requestId": request_id}))
        return
    if item.get("status") == "FULFILLED":
        print(json.dumps({"level": "INFO", "message": "already fulfilled", "requestId": request_id}))
        return

    reference, receipt = fulfil(item)
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    try:
        table().update_item(
            Key=key,
            UpdateExpression="SET #s = :f, fulfilmentRef = :ref, fulfilmentReceipt = :rcpt, fulfilledAt = :now, "
                             "lastUpdated = :now, GSI2PK = :g2",
            ConditionExpression="#s IN (:a, :aa)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":f": "FULFILLED", ":ref": reference, ":rcpt": receipt, ":now": now,
                ":g2": "STATUS#FULFILLED", ":a": "APPROVED", ":aa": "AUTO_APPROVED",
            },
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            print(json.dumps({"level": "INFO", "message": "status moved on, nothing to do", "requestId": request_id}))
            return
        raise
    print(json.dumps({"level": "INFO", "message": "fulfilled", "requestId": request_id, "reference": reference}))


def handler(event, _context):
    failures = []
    for record in event.get("Records", []):
        try:
            process(record["body"])
        except Exception as exc:  # report this message only, keep the rest of the batch
            print(json.dumps({"level": "ERROR", "message": "fulfilment failed", "messageId": record.get("messageId"),
                              "receiveCount": record.get("attributes", {}).get("ApproximateReceiveCount"),
                              "error": str(exc)}))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
