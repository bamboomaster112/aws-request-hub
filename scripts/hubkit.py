"""Shared helpers for the CloudShell test scripts: stack outputs, Cognito sign-in
with TOTP MFA, and small HTTP utilities. Standard library + boto3 only."""
import base64
import hashlib
import hmac
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover
    sys.exit("boto3 is required: pip3 install --user boto3")

STACK = os.environ.get("STACK", "request-hub")
REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"

session = boto3.session.Session(region_name=REGION)
cognito = session.client("cognito-idp")
cfn = session.client("cloudformation")


def outputs():
    stack = cfn.describe_stacks(StackName=STACK)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


# ---------------- TOTP (RFC 6238) ----------------
def totp(secret_b32, for_time=None, step=30, digits=6):
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8), casefold=True)
    counter = int((for_time or time.time()) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def wait_for_fresh_window(step=30):
    """Cognito rejects a TOTP code that was already used, so start in a new 30-second window."""
    remaining = step - (time.time() % step)
    time.sleep(remaining + 1)


# ---------------- test user store ----------------
# The playground denies secretsmanager:GetSecretValue to the console user, so test-user
# credentials live in a private file in the CloudShell home directory (mode 600).
# In a real account, keep them in Secrets Manager instead.
USERS_FILE = os.path.expanduser(f"~/.{STACK}-test-users.json")


def load_users():
    try:
        with open(USERS_FILE) as fh:
            return json.load(fh)
    except FileNotFoundError:
        sys.exit(f"No test users yet. Run: python3 scripts/seed-users.py  ({USERS_FILE} not found)")


def save_users(users):
    fd = os.open(USERS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(users, fh)


# ---------------- Cognito sign-in ----------------
_last_window = {}


def sign_in(out, user):
    """ADMIN_USER_PASSWORD_AUTH + SOFTWARE_TOKEN_MFA. Returns the ID token."""
    for attempt in range(2):
        resp = cognito.admin_initiate_auth(
            UserPoolId=out["UserPoolId"], ClientId=out["WebClientId"], AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": user["username"], "PASSWORD": user["password"]},
        )
        if "AuthenticationResult" in resp:
            return resp["AuthenticationResult"]["IdToken"]
        if resp.get("ChallengeName") != "SOFTWARE_TOKEN_MFA":
            raise RuntimeError(f"Unexpected challenge {resp.get('ChallengeName')} for {user['username']}")
        window = int(time.time() // 30)
        if _last_window.get(user["username"]) == window:
            wait_for_fresh_window()
        try:
            result = cognito.admin_respond_to_auth_challenge(
                UserPoolId=out["UserPoolId"], ClientId=out["WebClientId"], ChallengeName="SOFTWARE_TOKEN_MFA",
                Session=resp["Session"],
                ChallengeResponses={"USERNAME": user["username"], "SOFTWARE_TOKEN_MFA_CODE": totp(user["totpSecret"])},
            )
            _last_window[user["username"]] = int(time.time() // 30)
            return result["AuthenticationResult"]["IdToken"]
        except ClientError as exc:
            if attempt == 0 and exc.response["Error"]["Code"] in ("CodeMismatchException", "ExpiredCodeException",
                                                                  "NotAuthorizedException"):
                wait_for_fresh_window()
                continue
            raise
    raise RuntimeError(f"Could not sign in {user['username']}")


# ---------------- HTTP ----------------
def http(method, url, token=None, body=None, headers=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", token)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            text = res.read().decode("utf-8", "replace")
            status = res.status
    except urllib.error.HTTPError as err:
        text = err.read().decode("utf-8", "replace")
        status = err.code
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = text
    return status, parsed


def multipart(fields, file_field, file_name, content, content_type):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode() + content + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
