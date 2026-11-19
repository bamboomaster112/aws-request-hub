"""User management API for admins.

GET    /admin/users                     directory: every user with role, modules and status
POST   /admin/users                     invite a user (Cognito emails a temporary password)
GET    /admin/users/{userId}            one user with sign-in details and access history
PATCH  /admin/users/{userId}            change name, role or module access
DELETE /admin/users/{userId}            remove the user (their requests and history stay)
POST   /admin/users/{userId}/actions    DISABLE | ENABLE | RESET_PASSWORD | RESEND_INVITE | RESET_MFA | SIGN_OUT

Identity (password, MFA, sign-in) lives in Cognito. Authorisation lives in a PROFILE
item in DynamoDB (role, modules, status) that every API path checks on every call,
so a grant or a revocation takes effect immediately instead of when a token expires.
Every change writes the profile and an access-history item in one transaction and
publishes a "User Access Changed" event (audit lake, privileged-access alert).
"""
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

ROLES = {"requesters": "Requester", "approvers": "Approver", "admins": "Admin"}
MODULES = {
    "PURCHASE": "Purchasing",
    "ACCESS": "Access management",
    "LEAVE": "Leave",
    "CHANGE": "Change management",
    "GENERAL": "General requests",
}
ACTIONS = ("DISABLE", "ENABLE", "RESET_PASSWORD", "RESEND_INVITE", "RESET_MFA", "SIGN_OUT")
DIRECTORY_KEY = "DIRECTORY#USERS"
EVENT_SOURCE = "request-hub.identity"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USER_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
MAX_DIRECTORY_PAGES = 20

_clients = {}
_serializer = TypeSerializer()


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


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


def clean_text(value, limit):
    return CONTROL_CHARS.sub("", str(value or "")).strip()[:limit]


def normalise_modules(raw):
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ApiError(400, "modules must be a list")
    picked = {str(m).strip().upper() for m in raw}
    unknown = picked - set(MODULES)
    if unknown:
        raise ApiError(400, f"Unknown module(s): {', '.join(sorted(unknown))}")
    return sorted(picked)


def normalise_role(raw):
    role = str(raw or "").strip().lower()
    if role not in ROLES:
        raise ApiError(400, f"role must be one of: {', '.join(ROLES)}")
    return role


def cognito_attrs(user):
    return {a["Name"]: a["Value"] for a in user.get("Attributes") or user.get("UserAttributes") or []}


def cognito_error(exc):
    code = exc.response.get("Error", {}).get("Code", "")
    message = exc.response.get("Error", {}).get("Message", "Cognito rejected the change")
    mapping = {
        "UserNotFoundException": (404, "That user no longer exists in Cognito"),
        "UsernameExistsException": (409, "A user with this email already exists"),
        "AliasExistsException": (409, "A user with this email already exists"),
        "InvalidParameterException": (400, message),
        "NotAuthorizedException": (409, message),
        "LimitExceededException": (429, "Cognito is rate limiting these changes, try again in a minute"),
        "TooManyRequestsException": (429, "Cognito is rate limiting these changes, try again in a minute"),
        "CodeDeliveryFailureException": (502, "Cognito could not send the email"),
    }
    status, text = mapping.get(code, (502, f"Cognito error: {code or message}"))
    return ApiError(status, text)


# ---------------------------------------------------------------- data access
def profile_key(user_id):
    return {"PK": f"USER#{user_id}", "SK": "PROFILE"}


def load_profile(user_id):
    item = table().get_item(Key=profile_key(user_id), ConsistentRead=True).get("Item")
    return plain(item) if item else None


def list_profiles():
    profiles, kwargs = [], {}
    while True:
        page = table().query(IndexName="GSI2", KeyConditionExpression=Key("GSI2PK").eq(DIRECTORY_KEY), **kwargs)
        profiles.extend(plain(i) for i in page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return profiles
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def list_cognito_users():
    users, token = [], None
    for _ in range(MAX_DIRECTORY_PAGES):
        kwargs = {"UserPoolId": pool(), "Limit": 60}
        if token:
            kwargs["PaginationToken"] = token
        page = cognito().list_users(**kwargs)
        users.extend(page.get("Users", []))
        token = page.get("PaginationToken")
        if not token:
            break
    return users


def active_admin_ids():
    return {p["userId"] for p in list_profiles() if p.get("role") == "admins" and p.get("status") == "ACTIVE"}


def history(user_id, limit=25):
    page = table().query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("AUDIT#"),
        ScanIndexForward=False,
        Limit=limit,
    )
    out = []
    for item in page.get("Items", []):
        entry = plain(item)
        for key in ("PK", "SK", "entity"):
            entry.pop(key, None)
        for key in ("before", "after"):
            if isinstance(entry.get(key), str):
                try:
                    entry[key] = json.loads(entry[key])
                except ValueError:
                    pass
        out.append(entry)
    return out


def _serialise(item):
    return {k: _serializer.serialize(v) for k, v in item.items() if v is not None and v != set() and v != []}


def build_profile(base, **changes):
    item = {**base, **changes}
    modules = item.get("modules") or []
    item["modules"] = set(modules) if modules else None
    item["PK"], item["SK"] = f"USER#{item['userId']}", "PROFILE"
    item["entity"] = "USER_PROFILE"
    item["GSI2PK"], item["GSI2SK"] = DIRECTORY_KEY, int(item.get("createdAt") or now_ms())
    return item


def audit_item(user_id, actor, action, summary, before=None, after=None):
    at = now_ms()
    return {
        "PK": f"USER#{user_id}",
        "SK": f"AUDIT#{at:013d}#{uuid.uuid4().hex[:6]}",
        "entity": "ACCESS_AUDIT",
        "userId": user_id,
        "action": action,
        "summary": summary,
        "actorId": actor["userId"],
        "actorEmail": actor.get("email", ""),
        "at": at,
        "before": json.dumps(before, default=str) if before is not None else None,
        "after": json.dumps(after, default=str) if after is not None else None,
    }


def write(profile=None, audit=None, expected_version=None, create=False, delete_key=None):
    """Profile change and its history entry succeed or fail together."""
    name = os.environ["TABLE_NAME"]
    items = []
    if profile is not None:
        put = {"TableName": name, "Item": _serialise(profile)}
        if create:
            put["ConditionExpression"] = "attribute_not_exists(PK)"
        elif expected_version is not None:
            put["ConditionExpression"] = "#v = :v"
            put["ExpressionAttributeNames"] = {"#v": "version"}
            put["ExpressionAttributeValues"] = {":v": {"N": str(expected_version)}}
        items.append({"Put": put})
    if delete_key is not None:
        items.append({"Delete": {"TableName": name, "Key": _serialise(delete_key)}})
    if audit is not None:
        items.append({"Put": {"TableName": name, "Item": _serialise(audit),
                              "ConditionExpression": "attribute_not_exists(PK)"}})
    try:
        ddb().transact_write_items(TransactItems=items)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            raise ApiError(409, "Someone else changed this user a moment ago. Reload and try again.") from exc
        raise


def publish(action, user, actor, before=None):
    before = before or {}
    detail = {
        "version": "1",
        "action": action,
        "userId": user.get("userId"),
        "email": user.get("email"),
        "role": user.get("role"),
        "previousRole": before.get("role"),
        "modules": plain(user.get("modules")) or [],
        "previousModules": plain(before.get("modules")) or [],
        "status": user.get("status"),
        "actorId": actor["userId"],
        "actorEmail": actor.get("email", ""),
        "privileged": user.get("role") == "admins" and before.get("role") != "admins" and action != "DELETED",
    }
    try:
        result = events().put_events(Entries=[{
            "Source": EVENT_SOURCE,
            "DetailType": "User Access Changed",
            "Detail": json.dumps(detail),
            "EventBusName": os.environ.get("EVENT_BUS_NAME", "default"),
        }])
        if result.get("FailedEntryCount"):
            print(json.dumps({"level": "WARN", "message": "event not published", "result": result.get("Entries")}))
    except ClientError as exc:
        # The DynamoDB history is the system of record; the event is a copy for the audit lake and alerts.
        print(json.dumps({"level": "WARN", "message": "event not published", "error": str(exc)}))


# ---------------------------------------------------------------- views
def user_view(profile, cognito_user=None):
    profile = profile or {}
    attrs = cognito_attrs(cognito_user or {})
    user_id = profile.get("userId") or attrs.get("sub") or (cognito_user or {}).get("Username")
    view = {
        "userId": user_id,
        "username": profile.get("username") or (cognito_user or {}).get("Username") or user_id,
        "email": profile.get("email") or attrs.get("email", ""),
        "name": profile.get("name") or attrs.get("name", ""),
        "role": profile.get("role"),
        "modules": sorted(profile.get("modules") or []),
        "status": profile.get("status") or "NO_PROFILE",
        "managed": bool(profile),
        "version": profile.get("version"),
        "createdAt": profile.get("createdAt"),
        "updatedAt": profile.get("updatedAt"),
        "updatedByEmail": profile.get("updatedByEmail"),
        "cognitoStatus": (cognito_user or {}).get("UserStatus", "MISSING" if profile else "UNKNOWN"),
        "enabled": (cognito_user or {}).get("Enabled", profile.get("status") == "ACTIVE"),
    }
    created = (cognito_user or {}).get("UserCreateDate")
    if created is not None and not view["createdAt"]:
        view["createdAt"] = int(created.timestamp() * 1000) if hasattr(created, "timestamp") else created
    return view


def catalogue():
    return {
        "roles": [{"id": k, "label": v} for k, v in ROLES.items()],
        "modules": [{"id": k, "label": v} for k, v in MODULES.items()],
        "actions": list(ACTIONS),
    }


# ---------------------------------------------------------------- operations
def list_users():
    profiles = {p["userId"]: p for p in list_profiles()}
    seen, users = set(), []
    for cu in list_cognito_users():
        sub = cognito_attrs(cu).get("sub") or cu.get("Username")
        seen.add(sub)
        users.append(user_view(profiles.get(sub), cu))
    users.extend(user_view(p) for uid, p in profiles.items() if uid not in seen)
    users.sort(key=lambda u: (u.get("email") or "").lower())
    summary = {
        "total": len(users),
        "active": sum(1 for u in users if u["status"] == "ACTIVE"),
        "disabled": sum(1 for u in users if u["status"] == "DISABLED"),
        "invitesPending": sum(1 for u in users if u["cognitoStatus"] == "FORCE_CHANGE_PASSWORD"),
        "admins": sum(1 for u in users if u["role"] == "admins" and u["status"] == "ACTIVE"),
    }
    return response(200, {"items": users, "summary": summary, **catalogue()})


def create_user(body, actor):
    email = clean_text(body.get("email"), 254).lower()
    if not EMAIL_RE.match(email):
        raise ApiError(400, "Enter a valid email address")
    name = clean_text(body.get("name"), 80)
    role = normalise_role(body.get("role"))
    modules = normalise_modules(body.get("modules")) or []
    send_invite = body.get("sendInvite", True) is not False

    attributes = [{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}]
    if name:
        attributes.append({"Name": "name", "Value": name})
    kwargs = {"UserPoolId": pool(), "Username": email, "UserAttributes": attributes, "DesiredDeliveryMediums": ["EMAIL"]}
    if not send_invite:
        kwargs["MessageAction"] = "SUPPRESS"
    try:
        created = cognito().admin_create_user(**kwargs)["User"]
    except ClientError as exc:
        raise cognito_error(exc) from exc

    user_id = cognito_attrs(created).get("sub") or created["Username"]
    username = created["Username"]
    try:
        cognito().admin_add_user_to_group(UserPoolId=pool(), Username=username, GroupName=role)
        ts = now_ms()
        profile = build_profile({}, userId=user_id, username=username, email=email, name=name, role=role,
                                modules=modules, status="ACTIVE", version=1, createdAt=ts, createdBy=actor["userId"],
                                createdByEmail=actor.get("email", ""), updatedAt=ts, updatedBy=actor["userId"],
                                updatedByEmail=actor.get("email", ""))
        summary = f"Created as {ROLES[role]} with {describe_modules(role, modules)}"
        summary += "; invitation emailed" if send_invite else "; invitation not sent yet"
        write(profile, audit_item(user_id, actor, "CREATED", summary, after=snapshot(profile)), create=True)
    except Exception:
        # Never leave a Cognito identity behind without an access profile.
        try:
            cognito().admin_delete_user(UserPoolId=pool(), Username=username)
        except ClientError:
            pass
        raise

    publish("CREATED", profile, actor)
    return response(201, {"user": user_view(profile, created), "invited": send_invite})


def snapshot(profile):
    return {k: plain(profile.get(k)) for k in ("email", "name", "role", "modules", "status")}


def describe_modules(role, modules):
    if role == "admins":
        return "every module (admin)"
    if not modules:
        return "no modules"
    return "modules " + ", ".join(MODULES[m] for m in sorted(modules))


def get_user(user_id):
    profile = load_profile(user_id)
    username = (profile or {}).get("username") or user_id
    try:
        cu = cognito().admin_get_user(UserPoolId=pool(), Username=username)
        cu["Attributes"] = cu.get("UserAttributes", [])
        groups = [g["GroupName"] for g in cognito().admin_list_groups_for_user(UserPoolId=pool(), Username=username).get("Groups", [])]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "UserNotFoundException":
            raise cognito_error(exc) from exc
        if not profile:
            raise ApiError(404, "User not found") from exc
        cu, groups = None, []
    view = user_view(profile, cu)
    view["groups"] = groups
    mfa = (cu or {}).get("UserMFASettingList") or []
    view["mfa"] = "Authenticator app" if "SOFTWARE_TOKEN_MFA" in mfa else "Not enrolled yet"
    view["lastModified"] = (cu or {}).get("UserLastModifiedDate")
    view["history"] = history(user_id)
    return response(200, {"user": view, **catalogue()})


def require_profile(user_id):
    profile = load_profile(user_id)
    if profile is None:
        raise ApiError(409, "This user has no access profile yet. Set their role and modules first.")
    return profile


def adopt(user_id, actor):
    """A Cognito user created outside this API (console, CLI) gets a profile on first edit."""
    try:
        cu = cognito().admin_get_user(UserPoolId=pool(), Username=user_id)
    except ClientError as exc:
        raise cognito_error(exc) from exc
    attrs = cognito_attrs({"Attributes": cu.get("UserAttributes", [])})
    groups = [g["GroupName"] for g in cognito().admin_list_groups_for_user(UserPoolId=pool(), Username=cu["Username"]).get("Groups", [])]
    role = next((r for r in ("admins", "approvers", "requesters") if r in groups), "requesters")
    ts = now_ms()
    return build_profile({}, userId=attrs.get("sub", user_id), username=cu["Username"], email=attrs.get("email", ""),
                         name=attrs.get("name", ""), role=role, modules=[], status="ACTIVE" if cu.get("Enabled", True) else "DISABLED",
                         version=0, createdAt=ts, createdBy=actor["userId"], createdByEmail=actor.get("email", ""))


def guard_last_admin(target, actor, losing_admin):
    if not losing_admin:
        return
    if target["userId"] == actor["userId"]:
        raise ApiError(400, "You cannot remove your own admin access. Ask another admin to do it.")
    if active_admin_ids() <= {target["userId"]}:
        raise ApiError(409, "This is the last active admin. Make someone else an admin first.")


def update_user(user_id, body, actor):
    current = load_profile(user_id)
    adopting = current is None
    if adopting:
        current = adopt(user_id, actor)
    expected = body.get("version")
    if expected is not None and not adopting and int(expected) != int(current.get("version") or 0):
        raise ApiError(409, "Someone else changed this user while you were editing. Reload and try again.")

    role = normalise_role(body["role"]) if "role" in body else current["role"]
    modules = normalise_modules(body.get("modules")) if "modules" in body else sorted(current.get("modules") or [])
    name = clean_text(body["name"], 80) if "name" in body else current.get("name", "")

    was_admin = current.get("role") == "admins" and current.get("status") == "ACTIVE"
    guard_last_admin(current, actor, was_admin and role != "admins")

    changes = []
    if role != current.get("role"):
        changes.append(f"role {ROLES.get(current.get('role'), current.get('role'))} -> {ROLES[role]}")
    old_modules = sorted(current.get("modules") or [])
    if modules != old_modules:
        added, removed = sorted(set(modules) - set(old_modules)), sorted(set(old_modules) - set(modules))
        if added:
            changes.append("granted " + ", ".join(MODULES[m] for m in added))
        if removed:
            changes.append("revoked " + ", ".join(MODULES[m] for m in removed))
    if name != current.get("name", ""):
        changes.append("name updated")
    if not changes and not adopting:
        return response(200, {"user": user_view(current), "changed": False})

    username = current.get("username") or user_id
    try:
        if role != current.get("role") or adopting:
            cognito().admin_add_user_to_group(UserPoolId=pool(), Username=username, GroupName=role)
            for other in ROLES:
                if other != role:
                    try:
                        cognito().admin_remove_user_from_group(UserPoolId=pool(), Username=username, GroupName=other)
                    except ClientError as exc:
                        if exc.response.get("Error", {}).get("Code") not in ("UserNotFoundException", "ResourceNotFoundException"):
                            raise
        if name != current.get("name", ""):
            cognito().admin_update_user_attributes(UserPoolId=pool(), Username=username,
                                                   UserAttributes=[{"Name": "name", "Value": name}])
    except ClientError as exc:
        raise cognito_error(exc) from exc

    ts = now_ms()
    updated = build_profile(current, role=role, modules=modules, name=name, updatedAt=ts, updatedBy=actor["userId"],
                            updatedByEmail=actor.get("email", ""), version=int(current.get("version") or 0) + 1)
    action = "ACCESS_SET_UP" if adopting else "ACCESS_CHANGED"
    summary = "; ".join(changes) if changes else f"Access profile created as {ROLES[role]}"
    write(updated, audit_item(user_id, actor, action, summary, before=None if adopting else snapshot(current),
                              after=snapshot(updated)),
          expected_version=None if adopting else int(current.get("version") or 0), create=adopting)
    publish(action, updated, actor, before=None if adopting else current)
    return response(200, {"user": user_view(updated), "changed": True})


def delete_user(user_id, actor):
    profile = load_profile(user_id)
    target = profile or {"userId": user_id}
    if user_id == actor["userId"]:
        raise ApiError(400, "You cannot delete your own account")
    guard_last_admin(target, actor, (profile or {}).get("role") == "admins" and (profile or {}).get("status") == "ACTIVE")
    username = (profile or {}).get("username") or user_id
    try:
        cognito().admin_delete_user(UserPoolId=pool(), Username=username)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "UserNotFoundException":
            raise cognito_error(exc) from exc
    summary = f"Deleted {(profile or {}).get('email') or 'user'}; their requests and this history are kept"
    write(audit=audit_item(user_id, actor, "DELETED", summary, before=snapshot(profile) if profile else None),
          delete_key=profile_key(user_id) if profile else None)
    publish("DELETED", {**target, "status": "DELETED"}, actor, before=profile)
    return response(200, {"deleted": True, "userId": user_id})


def run_action(user_id, body, actor):
    action = str(body.get("action", "")).upper()
    if action not in ACTIONS:
        raise ApiError(400, f"action must be one of: {', '.join(ACTIONS)}")
    profile = load_profile(user_id)
    username = (profile or {}).get("username") or user_id
    target = profile or {"userId": user_id}
    if action == "DISABLE" and user_id == actor["userId"]:
        raise ApiError(400, "You cannot disable your own account")
    if action == "DISABLE":
        guard_last_admin(target, actor, (profile or {}).get("role") == "admins" and (profile or {}).get("status") == "ACTIVE")

    try:
        status = cognito().admin_get_user(UserPoolId=pool(), Username=username).get("UserStatus")
        if action == "DISABLE":
            cognito().admin_disable_user(UserPoolId=pool(), Username=username)
            cognito().admin_user_global_sign_out(UserPoolId=pool(), Username=username)
            summary = "Disabled: sign-in blocked and API access revoked immediately"
        elif action == "ENABLE":
            cognito().admin_enable_user(UserPoolId=pool(), Username=username)
            summary = "Enabled: sign-in and API access restored"
        elif action == "RESET_PASSWORD":
            if status == "FORCE_CHANGE_PASSWORD":
                raise ApiError(409, "This user has not signed in yet. Resend the invitation instead.")
            cognito().admin_reset_user_password(UserPoolId=pool(), Username=username)
            summary = "Password reset: Cognito emailed a verification code"
        elif action == "RESEND_INVITE":
            if status != "FORCE_CHANGE_PASSWORD":
                raise ApiError(409, "This user has already signed in. Use Reset password instead.")
            email = (profile or {}).get("email") or username
            cognito().admin_create_user(UserPoolId=pool(), Username=email, MessageAction="RESEND",
                                        DesiredDeliveryMediums=["EMAIL"])
            summary = "Invitation re-sent with a new temporary password"
        elif action == "RESET_MFA":
            cognito().admin_set_user_mfa_preference(
                UserPoolId=pool(), Username=username,
                SoftwareTokenMfaSettings={"Enabled": False, "PreferredMfa": False})
            cognito().admin_user_global_sign_out(UserPoolId=pool(), Username=username)
            summary = "Authenticator reset: the user enrols a new device at next sign-in"
        else:
            cognito().admin_user_global_sign_out(UserPoolId=pool(), Username=username)
            summary = "Signed out of every session (refresh tokens revoked)"
    except ClientError as exc:
        raise cognito_error(exc) from exc

    updated = profile
    if profile and action in ("DISABLE", "ENABLE"):
        updated = build_profile(profile, status="DISABLED" if action == "DISABLE" else "ACTIVE", updatedAt=now_ms(),
                                updatedBy=actor["userId"], updatedByEmail=actor.get("email", ""),
                                version=int(profile.get("version") or 0) + 1)
        write(updated, audit_item(user_id, actor, action, summary, before=snapshot(profile), after=snapshot(updated)),
              expected_version=int(profile.get("version") or 0))
        publish(action, updated, actor, before=profile)
    else:
        write(audit=audit_item(user_id, actor, action, summary))
    return response(200, {"userId": user_id, "action": action, "message": summary,
                          "user": user_view(updated) if updated else None})


# ---------------------------------------------------------------- entry point
def actor_from(event):
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    sub = claims.get("sub")
    if not sub:
        raise ApiError(401, "Unauthorized")
    profile = load_profile(sub)
    # The live profile decides, not the token: a demoted or disabled admin loses access at once.
    if not profile or profile.get("role") != "admins" or profile.get("status") != "ACTIVE":
        raise ApiError(403, "Only active admins can manage users")
    return {"userId": sub, "email": claims.get("email") or profile.get("email", "")}


def handler(event, _context):
    try:
        actor = actor_from(event)
        resource, method = event.get("resource"), event.get("httpMethod")
        user_id = (event.get("pathParameters") or {}).get("userId")
        if user_id is not None and not USER_ID_RE.match(user_id):
            raise ApiError(404, "User not found")
        try:
            body = json.loads(event.get("body") or "{}")
        except ValueError as exc:
            raise ApiError(400, "Body must be JSON") from exc
        if not isinstance(body, dict):
            raise ApiError(400, "Body must be a JSON object")

        if resource == "/admin/users" and method == "GET":
            return list_users()
        if resource == "/admin/users" and method == "POST":
            return create_user(body, actor)
        if resource == "/admin/users/{userId}" and method == "GET":
            return get_user(user_id)
        if resource == "/admin/users/{userId}" and method == "PATCH":
            return update_user(user_id, body, actor)
        if resource == "/admin/users/{userId}" and method == "DELETE":
            return delete_user(user_id, actor)
        if resource == "/admin/users/{userId}/actions" and method == "POST":
            return run_action(user_id, body, actor)
        return response(404, {"message": "Not found"})
    except ApiError as err:
        return response(err.status, {"message": err.message})
    except ClientError as exc:
        print(json.dumps({"level": "ERROR", "message": "aws error", "error": str(exc)}))
        return response(500, {"message": "The change could not be saved, try again"})
