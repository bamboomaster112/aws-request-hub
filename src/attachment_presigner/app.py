"""POST /requests/{requestId}/attachments

Returns a presigned S3 POST policy so the browser uploads straight to S3.
A POST policy (rather than a presigned PUT) lets S3 itself enforce the file
size limit and the content type. Attachment metadata is a child item
(SK=ATT#...), so it never triggers the request's status-change events.
"""
import json
import os
import re
import time
import uuid

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-east-1")
MAX_BYTES = 10 * 1024 * 1024
UPLOAD_WINDOW_SECONDS = 300
ALLOWED_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "text/plain",
    "text/csv",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

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
    if isinstance(raw, (list, tuple, set)):
        return {str(g).strip() for g in raw if str(g).strip()}
    text = str(raw or "").strip().strip("[]")
    return {g.strip() for g in text.replace(",", " ").split() if g.strip()}


def load_access(sub):
    """Live role and status from the PROFILE item that the user admin API maintains."""
    item = table().get_item(Key={"PK": f"USER#{sub}", "SK": "PROFILE"}, ConsistentRead=True).get("Item")
    if not item or item.get("status") != "ACTIVE":
        return None
    return {"role": item.get("role")}


def safe_file_name(name):
    base = os.path.basename(str(name or "file"))
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._") or "file"
    return cleaned[:100]


def response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body),
    }


def handler(event, _context):
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    sub, email = claims.get("sub"), claims.get("email", "")
    if not sub:
        return response(401, {"message": "Unauthorized"})
    access = load_access(sub)
    if access is None:
        return response(403, {"message": "Your account is disabled or has no access set up yet. Ask an admin for access."})
    groups = {access["role"]}

    request_id = (event.get("pathParameters") or {}).get("requestId", "")
    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        return response(400, {"message": "Body must be JSON"})

    content_type = str(body.get("contentType", "")).lower()
    if content_type not in ALLOWED_TYPES:
        return response(400, {"message": "Allowed types: PDF, PNG, JPEG, TXT, CSV, DOCX, XLSX"})
    file_name = safe_file_name(body.get("fileName"))

    meta = table().get_item(Key={"PK": f"REQ#{request_id}", "SK": "META"}).get("Item")
    if meta is None or (meta.get("requesterId") != sub and "admins" not in groups):
        return response(404, {"message": "Request not found"})

    attachment_id = uuid.uuid4().hex[:12]
    key = f"requests/{request_id}/{attachment_id}-{file_name}"
    upload = s3().generate_presigned_post(
        Bucket=os.environ["ATTACHMENTS_BUCKET"],
        Key=key,
        Fields={"Content-Type": content_type},
        Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, MAX_BYTES]],
        ExpiresIn=UPLOAD_WINDOW_SECONDS,
    )
    table().put_item(Item={
        "PK": f"REQ#{request_id}",
        "SK": f"ATT#{attachment_id}",
        "entity": "ATTACHMENT",
        "attachmentId": attachment_id,
        "key": key,
        "fileName": file_name,
        "contentType": content_type,
        "uploadedBy": email,
        "createdAt": int(time.time() * 1000),
    })
    return response(201, {
        "attachmentId": attachment_id,
        "fileName": file_name,
        "maxBytes": MAX_BYTES,
        "expiresInSeconds": UPLOAD_WINDOW_SECONDS,
        "upload": upload,
    })
