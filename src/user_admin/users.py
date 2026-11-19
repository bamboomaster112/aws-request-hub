"""Users, roles, module access, reporting lines and auto-approval rules."""
import datetime
import re

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import common
from common import ApiError
from defaults import MODULES, ROLES

ACTIONS = ("DISABLE", "ENABLE", "RESET_PASSWORD", "RESEND_INVITE", "RESET_MFA", "SIGN_OUT")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MANAGER_ROLES = ("approvers", "admins")
MAX_DIRECTORY_PAGES = 20
MAX_IMPORT_ROWS = 500
MAX_RULE_DAYS = 366


# ---------------------------------------------------------------- small helpers
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


def describe_modules(role, modules):
    if role == "admins":
        return "every module (admin)"
    if not modules:
        return "no modules"
    return "modules " + ", ".join(MODULES[m] for m in sorted(modules))


def snapshot(profile):
    return {k: common.plain(profile.get(k)) for k in
            ("email", "name", "role", "modules", "status", "managerId", "department", "jobTitle")}


def build_profile(base, **changes):
    item = {**base, **changes}
    modules = item.get("modules") or []
    item["modules"] = set(modules) if modules else None
    item["PK"], item["SK"] = f"USER#{item['userId']}", "PROFILE"
    item["entity"] = "USER_PROFILE"
    item["GSI2PK"], item["GSI2SK"] = common.DIRECTORY_KEY, int(item.get("createdAt") or common.now_ms())
    for key in ("managerId", "department", "jobTitle"):
        if item.get(key) in ("", None):
            item[key] = None
    return item


def user_view(profile, cognito_user=None, reports=0):
    profile = profile or {}
    cu = cognito_user or {}
    attrs = cognito_attrs(cu)
    user_id = profile.get("userId") or attrs.get("sub") or cu.get("Username")
    view = {
        "userId": user_id,
        "username": profile.get("username") or cu.get("Username") or user_id,
        "email": profile.get("email") or attrs.get("email", ""),
        "name": profile.get("name") or attrs.get("name", ""),
        "role": profile.get("role"),
        "modules": sorted(profile.get("modules") or []),
        "status": profile.get("status") or "NO_PROFILE",
        "managerId": profile.get("managerId"),
        "department": profile.get("department") or "",
        "jobTitle": profile.get("jobTitle") or "",
        "reports": reports,
        "managed": bool(profile),
        "version": profile.get("version"),
        "createdAt": profile.get("createdAt"),
        "updatedAt": profile.get("updatedAt"),
        "updatedByEmail": profile.get("updatedByEmail"),
        "cognitoStatus": cu.get("UserStatus", "MISSING" if profile else "UNKNOWN"),
        "enabled": cu.get("Enabled", profile.get("status") == "ACTIVE"),
    }
    created = cu.get("UserCreateDate")
    if created is not None and not view["createdAt"]:
        view["createdAt"] = int(created.timestamp() * 1000) if hasattr(created, "timestamp") else created
    return view


def access_event(action, user, actor, before=None):
    before = before or {}
    common.publish("request-hub.identity", "User Access Changed", {
        "version": "1",
        "action": action,
        "userId": user.get("userId"),
        "email": user.get("email"),
        "role": user.get("role"),
        "previousRole": before.get("role"),
        "modules": common.plain(user.get("modules")) or [],
        "previousModules": common.plain(before.get("modules")) or [],
        "managerId": user.get("managerId"),
        "status": user.get("status"),
        "actorId": actor["userId"],
        "actorEmail": actor.get("email", ""),
        "privileged": user.get("role") == "admins" and before.get("role") != "admins" and action != "DELETED",
    })


def group_memberships(user_id, groups=None):
    groups = groups if groups is not None else common.config_items("GROUP#")
    return [g for g in groups if user_id in (g.get("members") or [])]


# ---------------------------------------------------------------- reporting lines
def validate_manager(user_id, manager_id, profiles_by_id):
    """None clears the manager. Raises ApiError when the change would break the hierarchy."""
    if manager_id in (None, ""):
        return None
    if manager_id == user_id:
        raise ApiError(400, "A person cannot be their own manager")
    manager = profiles_by_id.get(manager_id)
    if not manager:
        raise ApiError(400, "The chosen manager does not exist")
    who = manager.get("email") or manager_id
    if manager.get("status") != "ACTIVE":
        raise ApiError(400, f"{who} is disabled, so they cannot approve for anyone")
    if manager.get("role") not in MANAGER_ROLES:
        raise ApiError(400, f"{who} is a Requester. Make them an Approver or Admin before people report to them.")
    seen, current = set(), manager_id
    while current and current not in seen:
        if current == user_id:
            raise ApiError(400, f"That would create a reporting loop: {who} already reports, directly or indirectly, to this person")
        seen.add(current)
        current = (profiles_by_id.get(current) or {}).get("managerId")
    return manager_id


def clean_org_text(body, key, current):
    if key not in body:
        return current or ""
    return common.clean_text(body.get(key), 60)


# ---------------------------------------------------------------- directory
def list_cognito_users():
    users, token = [], None
    for _ in range(MAX_DIRECTORY_PAGES):
        kwargs = {"UserPoolId": common.pool(), "Limit": 60}
        if token:
            kwargs["PaginationToken"] = token
        page = common.cognito().list_users(**kwargs)
        users.extend(page.get("Users", []))
        token = page.get("PaginationToken")
        if not token:
            break
    return users


def active_admin_ids(profiles=None):
    profiles = profiles if profiles is not None else common.list_profiles()
    return {p["userId"] for p in profiles if p.get("role") == "admins" and p.get("status") == "ACTIVE"}


def list_users():
    profile_list = common.list_profiles()
    profiles = {p["userId"]: p for p in profile_list}
    reports = {}
    for p in profile_list:
        if p.get("managerId"):
            reports[p["managerId"]] = reports.get(p["managerId"], 0) + 1
    seen, users = set(), []
    for cu in list_cognito_users():
        sub = cognito_attrs(cu).get("sub") or cu.get("Username")
        seen.add(sub)
        users.append(user_view(profiles.get(sub), cu, reports.get(sub, 0)))
    users.extend(user_view(p, reports=reports.get(uid, 0)) for uid, p in profiles.items() if uid not in seen)
    users.sort(key=lambda u: (u.get("email") or "").lower())
    summary = {
        "total": len(users),
        "active": sum(1 for u in users if u["status"] == "ACTIVE"),
        "disabled": sum(1 for u in users if u["status"] == "DISABLED"),
        "invitesPending": sum(1 for u in users if u["cognitoStatus"] == "FORCE_CHANGE_PASSWORD"),
        "admins": sum(1 for u in users if u["role"] == "admins" and u["status"] == "ACTIVE"),
        "noManager": sum(1 for u in users if u["status"] == "ACTIVE" and not u["managerId"]),
    }
    return common.response(200, {"items": users, "summary": summary, **catalogue()})


def catalogue():
    return {
        "roles": [{"id": k, "label": v} for k, v in ROLES.items()],
        "modules": [{"id": k, "label": v} for k, v in MODULES.items()],
        "actions": list(ACTIONS),
    }


# ---------------------------------------------------------------- create, read, update, delete
def create_user(body, actor):
    email = common.clean_text(body.get("email"), 254).lower()
    if not EMAIL_RE.match(email):
        raise ApiError(400, "Enter a valid email address")
    name = common.clean_text(body.get("name"), 80)
    role = normalise_role(body.get("role"))
    modules = normalise_modules(body.get("modules")) or []
    send_invite = body.get("sendInvite", True) is not False
    department = clean_org_text(body, "department", "")
    job_title = clean_org_text(body, "jobTitle", "")
    manager_id = body.get("managerId") or None
    if manager_id:
        profiles_by_id = {p["userId"]: p for p in common.list_profiles()}
        validate_manager("new-user", manager_id, profiles_by_id)

    attributes = [{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}]
    if name:
        attributes.append({"Name": "name", "Value": name})
    kwargs = {"UserPoolId": common.pool(), "Username": email, "UserAttributes": attributes, "DesiredDeliveryMediums": ["EMAIL"]}
    if not send_invite:
        kwargs["MessageAction"] = "SUPPRESS"
    try:
        created = common.cognito().admin_create_user(**kwargs)["User"]
    except ClientError as exc:
        raise cognito_error(exc) from exc

    user_id = cognito_attrs(created).get("sub") or created["Username"]
    username = created["Username"]
    try:
        common.cognito().admin_add_user_to_group(UserPoolId=common.pool(), Username=username, GroupName=role)
        ts = common.now_ms()
        profile = build_profile({}, userId=user_id, username=username, email=email, name=name, role=role,
                                modules=modules, status="ACTIVE", version=1, createdAt=ts, createdBy=actor["userId"],
                                createdByEmail=actor.get("email", ""), updatedAt=ts, updatedBy=actor["userId"],
                                updatedByEmail=actor.get("email", ""), managerId=manager_id, department=department,
                                jobTitle=job_title)
        summary = f"Created as {ROLES[role]} with {describe_modules(role, modules)}"
        summary += "; invitation emailed" if send_invite else "; invitation not sent yet"
        common.transact([common.versioned_put(profile, None, create=True),
                         common.audit_op(f"USER#{user_id}", actor, "CREATED", summary, after=snapshot(profile))])
    except Exception:
        # Never leave a Cognito identity behind without an access profile.
        try:
            common.cognito().admin_delete_user(UserPoolId=common.pool(), Username=username)
        except ClientError:
            pass
        raise

    access_event("CREATED", profile, actor)
    return common.response(201, {"user": user_view(profile, created), "invited": send_invite})


def auto_rules_for(user_id):
    items = common.query_all(KeyConditionExpression=Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("AUTORULE#"))
    return [rule_view(i) for i in items]


def rule_view(item):
    view = common.strip_keys(item)
    view["active"] = int(item.get("expiresAt") or 0) > common.now_ms()
    return view


def get_user(user_id):
    profile = common.load_profile(user_id)
    username = (profile or {}).get("username") or user_id
    try:
        cu = common.cognito().admin_get_user(UserPoolId=common.pool(), Username=username)
        cu["Attributes"] = cu.get("UserAttributes", [])
        groups = [g["GroupName"] for g in common.cognito().admin_list_groups_for_user(
            UserPoolId=common.pool(), Username=username).get("Groups", [])]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "UserNotFoundException":
            raise cognito_error(exc) from exc
        if not profile:
            raise ApiError(404, "User not found") from exc
        cu, groups = None, []
    profiles = common.list_profiles()
    reports = [{"userId": p["userId"], "email": p.get("email"), "name": p.get("name", "")}
               for p in profiles if p.get("managerId") == user_id]
    view = user_view(profile, cu, len(reports))
    view["groups"] = groups
    view["directReports"] = reports
    view["approverGroups"] = [{"groupId": g["groupId"], "name": g.get("name")} for g in group_memberships(user_id)]
    mfa = (cu or {}).get("UserMFASettingList") or []
    view["mfa"] = "Authenticator app" if "SOFTWARE_TOKEN_MFA" in mfa else "Not enrolled yet"
    view["lastModified"] = (cu or {}).get("UserLastModifiedDate")
    view["autoRules"] = auto_rules_for(user_id)
    view["history"] = common.history(f"USER#{user_id}")
    return common.response(200, {"user": view, **catalogue()})


def adopt(user_id, actor):
    """A Cognito user created outside this API (console, CLI) gets a profile on first edit."""
    try:
        cu = common.cognito().admin_get_user(UserPoolId=common.pool(), Username=user_id)
    except ClientError as exc:
        raise cognito_error(exc) from exc
    attrs = cognito_attrs({"Attributes": cu.get("UserAttributes", [])})
    groups = [g["GroupName"] for g in common.cognito().admin_list_groups_for_user(
        UserPoolId=common.pool(), Username=cu["Username"]).get("Groups", [])]
    role = next((r for r in ("admins", "approvers", "requesters") if r in groups), "requesters")
    ts = common.now_ms()
    return build_profile({}, userId=attrs.get("sub", user_id), username=cu["Username"], email=attrs.get("email", ""),
                         name=attrs.get("name", ""), role=role, modules=[],
                         status="ACTIVE" if cu.get("Enabled", True) else "DISABLED",
                         version=0, createdAt=ts, createdBy=actor["userId"], createdByEmail=actor.get("email", ""))


def guard_last_admin(target, actor, losing_admin, profiles=None):
    if not losing_admin:
        return
    if target["userId"] == actor["userId"]:
        raise ApiError(400, "You cannot remove your own admin access. Ask another admin to do it.")
    if active_admin_ids(profiles) <= {target["userId"]}:
        raise ApiError(409, "This is the last active admin. Make someone else an admin first.")


def guard_approval_duties(user_id, profiles, verb):
    """People who approve for others must hand that over before they lose the ability to approve."""
    reports = [p.get("email") or p["userId"] for p in profiles if p.get("managerId") == user_id]
    groups = [g.get("name") or g["groupId"] for g in group_memberships(user_id)]
    if reports or groups:
        parts = []
        if reports:
            parts.append(f"{len(reports)} direct report(s) ({', '.join(sorted(reports)[:3])}{'...' if len(reports) > 3 else ''})")
        if groups:
            parts.append("membership of " + ", ".join(groups))
        raise ApiError(409, f"Before you {verb} this person, reassign their " + " and ".join(parts) + ".")


def update_user(user_id, body, actor):
    current = common.load_profile(user_id)
    adopting = current is None
    if adopting:
        current = adopt(user_id, actor)
    expected = body.get("version")
    if expected is not None and not adopting and int(expected) != int(current.get("version") or 0):
        raise ApiError(409, "Someone else changed this user while you were editing. Reload and try again.")

    profiles = common.list_profiles()
    profiles_by_id = {p["userId"]: p for p in profiles}
    role = normalise_role(body["role"]) if "role" in body else current["role"]
    modules = normalise_modules(body.get("modules")) if "modules" in body else sorted(current.get("modules") or [])
    name = common.clean_text(body["name"], 80) if "name" in body else current.get("name", "")
    department = clean_org_text(body, "department", current.get("department"))
    job_title = clean_org_text(body, "jobTitle", current.get("jobTitle"))
    manager_id = current.get("managerId")
    if "managerId" in body:
        manager_id = validate_manager(user_id, body.get("managerId"), profiles_by_id)

    was_admin = current.get("role") == "admins" and current.get("status") == "ACTIVE"
    guard_last_admin(current, actor, was_admin and role != "admins", profiles)
    if role == "requesters" and current.get("role") in MANAGER_ROLES:
        guard_approval_duties(user_id, profiles, "make a Requester of")

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
    if (manager_id or None) != (current.get("managerId") or None):
        manager = profiles_by_id.get(manager_id) or {}
        changes.append(f"manager set to {manager.get('email')}" if manager_id else "manager removed")
    if department != (current.get("department") or ""):
        changes.append(f"department set to {department or 'none'}")
    if job_title != (current.get("jobTitle") or ""):
        changes.append("job title updated")
    if name != current.get("name", ""):
        changes.append("name updated")
    if not changes and not adopting:
        return common.response(200, {"user": user_view(current), "changed": False})

    username = current.get("username") or user_id
    try:
        if role != current.get("role") or adopting:
            common.cognito().admin_add_user_to_group(UserPoolId=common.pool(), Username=username, GroupName=role)
            for other in ROLES:
                if other != role:
                    try:
                        common.cognito().admin_remove_user_from_group(UserPoolId=common.pool(), Username=username, GroupName=other)
                    except ClientError as exc:
                        if exc.response.get("Error", {}).get("Code") not in ("UserNotFoundException", "ResourceNotFoundException"):
                            raise
        if name != current.get("name", ""):
            common.cognito().admin_update_user_attributes(UserPoolId=common.pool(), Username=username,
                                                          UserAttributes=[{"Name": "name", "Value": name}])
    except ClientError as exc:
        raise cognito_error(exc) from exc

    ts = common.now_ms()
    updated = build_profile(current, role=role, modules=modules, name=name, managerId=manager_id, department=department,
                            jobTitle=job_title, updatedAt=ts, updatedBy=actor["userId"],
                            updatedByEmail=actor.get("email", ""), version=int(current.get("version") or 0) + 1)
    action = "ACCESS_SET_UP" if adopting else "ACCESS_CHANGED"
    summary = "; ".join(changes) if changes else f"Access profile created as {ROLES[role]}"
    common.transact([
        common.versioned_put(updated, current.get("version"), create=adopting),
        common.audit_op(f"USER#{user_id}", actor, action, summary,
                        before=None if adopting else snapshot(current), after=snapshot(updated)),
    ], "Someone else changed this user a moment ago. Reload and try again.")
    access_event(action, updated, actor, before=None if adopting else current)
    return common.response(200, {"user": user_view(updated), "changed": True})


def delete_user(user_id, actor):
    profile = common.load_profile(user_id)
    target = profile or {"userId": user_id}
    if user_id == actor["userId"]:
        raise ApiError(400, "You cannot delete your own account")
    profiles = common.list_profiles()
    guard_last_admin(target, actor, (profile or {}).get("role") == "admins" and (profile or {}).get("status") == "ACTIVE", profiles)
    guard_approval_duties(user_id, profiles, "delete")
    username = (profile or {}).get("username") or user_id
    try:
        common.cognito().admin_delete_user(UserPoolId=common.pool(), Username=username)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "UserNotFoundException":
            raise cognito_error(exc) from exc
    summary = f"Deleted {(profile or {}).get('email') or 'user'}; their requests and this history are kept"
    ops = [common.audit_op(f"USER#{user_id}", actor, "DELETED", summary, before=snapshot(profile) if profile else None)]
    if profile:
        ops.insert(0, common.delete_op(common.profile_key(user_id)))
    for rule in auto_rules_for(user_id):
        ops.insert(0, common.delete_op({"PK": f"USER#{user_id}", "SK": f"AUTORULE#{rule['module']}"}))
    common.transact(ops)
    access_event("DELETED", {**target, "status": "DELETED"}, actor, before=profile)
    return common.response(200, {"deleted": True, "userId": user_id})


def run_action(user_id, body, actor):
    action = str(body.get("action", "")).upper()
    if action not in ACTIONS:
        raise ApiError(400, f"action must be one of: {', '.join(ACTIONS)}")
    profile = common.load_profile(user_id)
    username = (profile or {}).get("username") or user_id
    target = profile or {"userId": user_id}
    if action == "DISABLE" and user_id == actor["userId"]:
        raise ApiError(400, "You cannot disable your own account")
    if action == "DISABLE":
        guard_last_admin(target, actor, (profile or {}).get("role") == "admins" and (profile or {}).get("status") == "ACTIVE")

    pool = common.pool()
    try:
        status = common.cognito().admin_get_user(UserPoolId=pool, Username=username).get("UserStatus")
        if action == "DISABLE":
            common.cognito().admin_disable_user(UserPoolId=pool, Username=username)
            common.cognito().admin_user_global_sign_out(UserPoolId=pool, Username=username)
            summary = "Disabled: sign-in blocked and API access revoked immediately"
        elif action == "ENABLE":
            common.cognito().admin_enable_user(UserPoolId=pool, Username=username)
            summary = "Enabled: sign-in and API access restored"
        elif action == "RESET_PASSWORD":
            if status == "FORCE_CHANGE_PASSWORD":
                raise ApiError(409, "This user has not signed in yet. Resend the invitation instead.")
            common.cognito().admin_reset_user_password(UserPoolId=pool, Username=username)
            summary = "Password reset: Cognito emailed a verification code"
        elif action == "RESEND_INVITE":
            if status != "FORCE_CHANGE_PASSWORD":
                raise ApiError(409, "This user has already signed in. Use Reset password instead.")
            email = (profile or {}).get("email") or username
            common.cognito().admin_create_user(UserPoolId=pool, Username=email, MessageAction="RESEND",
                                               DesiredDeliveryMediums=["EMAIL"])
            summary = "Invitation re-sent with a new temporary password"
        elif action == "RESET_MFA":
            common.cognito().admin_set_user_mfa_preference(
                UserPoolId=pool, Username=username, SoftwareTokenMfaSettings={"Enabled": False, "PreferredMfa": False})
            common.cognito().admin_user_global_sign_out(UserPoolId=pool, Username=username)
            summary = "Authenticator reset: the user enrols a new device at next sign-in"
        else:
            common.cognito().admin_user_global_sign_out(UserPoolId=pool, Username=username)
            summary = "Signed out of every session (refresh tokens revoked)"
    except ClientError as exc:
        raise cognito_error(exc) from exc

    updated = profile
    if profile and action in ("DISABLE", "ENABLE"):
        updated = build_profile(profile, status="DISABLED" if action == "DISABLE" else "ACTIVE", updatedAt=common.now_ms(),
                                updatedBy=actor["userId"], updatedByEmail=actor.get("email", ""),
                                version=int(profile.get("version") or 0) + 1)
        common.transact([common.versioned_put(updated, profile.get("version")),
                         common.audit_op(f"USER#{user_id}", actor, action, summary, before=snapshot(profile),
                                         after=snapshot(updated))])
        access_event(action, updated, actor, before=profile)
    else:
        common.transact([common.audit_op(f"USER#{user_id}", actor, action, summary)])
    return common.response(200, {"userId": user_id, "action": action, "message": summary,
                                 "user": user_view(updated) if updated else None})


# ---------------------------------------------------------------- auto-approval rules
def parse_expiry(raw):
    text = str(raw or "").strip()
    if not DATE_RE.match(text):
        raise ApiError(400, "expiresOn must be a date (YYYY-MM-DD)")
    try:
        day = datetime.date.fromisoformat(text)
    except ValueError as exc:
        raise ApiError(400, "expiresOn is not a real date") from exc
    today = datetime.datetime.now(datetime.timezone.utc).date()
    if day <= today:
        raise ApiError(400, "The expiry date must be in the future")
    if (day - today).days > MAX_RULE_DAYS:
        raise ApiError(400, f"Auto-approval can be granted for at most {MAX_RULE_DAYS} days at a time")
    end_of_day = datetime.datetime.combine(day, datetime.time(23, 59, 59), datetime.timezone.utc)
    return text, int(end_of_day.timestamp() * 1000) + 999


def set_auto_rule(user_id, module, body, actor):
    module = str(module or "").upper()
    if module not in MODULES:
        raise ApiError(404, "Unknown module")
    if user_id == actor["userId"]:
        raise ApiError(403, "You cannot grant yourself auto-approval. Another admin has to do it.")
    profile = common.load_profile(user_id)
    if not profile or profile.get("status") != "ACTIVE":
        raise ApiError(409, "Auto-approval can only be granted to an active user with an access profile")
    if profile.get("role") != "admins" and module not in (profile.get("modules") or []):
        raise ApiError(409, f"{profile.get('email')} cannot raise {MODULES[module]} requests. Grant the module first.")
    try:
        limit = float(body.get("maxAmount"))
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "maxAmount must be a number") from exc
    if not 0 < limit <= 1_000_000_000:
        raise ApiError(400, "maxAmount must be greater than 0")
    expires_on, expires_at = parse_expiry(body.get("expiresOn"))
    note = common.clean_text(body.get("note"), 200)
    existing = common.table().get_item(Key={"PK": f"USER#{user_id}", "SK": f"AUTORULE#{module}"}).get("Item")
    ts = common.now_ms()
    rule = {
        "PK": f"USER#{user_id}", "SK": f"AUTORULE#{module}", "entity": "AUTO_RULE",
        "userId": user_id, "email": profile.get("email"), "name": profile.get("name", ""), "module": module,
        "maxAmount": int(limit) if float(limit).is_integer() else limit, "expiresOn": expires_on, "expiresAt": expires_at,
        "note": note or None, "createdAt": ts, "createdBy": actor["userId"], "createdByEmail": actor.get("email", ""),
        "GSI2PK": common.AUTORULES_KEY, "GSI2SK": ts,
    }
    summary = f"Auto-approval for {MODULES[module]} up to {rule['maxAmount']:,} until {expires_on}"
    if existing:
        summary = "Changed: " + summary
    common.transact([common.put_op(rule), common.audit_op(f"USER#{user_id}", actor, "AUTO_RULE_SET", summary,
                                                          before=common.strip_keys(common.plain(existing)) if existing else None,
                                                          after=common.strip_keys(rule))])
    common.publish("request-hub.identity", "Auto Approval Changed", {
        "version": "1", "action": "SET", "userId": user_id, "email": profile.get("email"), "module": module,
        "maxAmount": rule["maxAmount"], "expiresOn": expires_on, "actorId": actor["userId"], "actorEmail": actor.get("email", ""),
    })
    return common.response(200, {"rule": rule_view(rule), "message": summary})


def delete_auto_rule(user_id, module, actor):
    module = str(module or "").upper()
    key = {"PK": f"USER#{user_id}", "SK": f"AUTORULE#{module}"}
    existing = common.table().get_item(Key=key).get("Item")
    if not existing:
        raise ApiError(404, "There is no auto-approval rule for that module")
    summary = f"Auto-approval for {MODULES.get(module, module)} revoked"
    common.transact([common.delete_op(key), common.audit_op(f"USER#{user_id}", actor, "AUTO_RULE_REVOKED", summary,
                                                            before=common.strip_keys(common.plain(existing)))])
    common.publish("request-hub.identity", "Auto Approval Changed", {
        "version": "1", "action": "REVOKED", "userId": user_id, "email": existing.get("email"), "module": module,
        "actorId": actor["userId"], "actorEmail": actor.get("email", ""),
    })
    return common.response(200, {"revoked": True, "message": summary})


def list_auto_rules():
    rules = common.query_all(IndexName="GSI2", KeyConditionExpression=Key("GSI2PK").eq(common.AUTORULES_KEY))
    rules = [rule_view(r) for r in rules]
    rules.sort(key=lambda r: (not r["active"], (r.get("email") or "").lower(), r.get("module", "")))
    return rules


# ---------------------------------------------------------------- bulk hierarchy import
def import_hierarchy(body, actor):
    """Rows of {email, managerEmail, department, jobTitle}. dryRun validates without saving."""
    rows = body.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ApiError(400, "rows must be a non-empty list")
    if len(rows) > MAX_IMPORT_ROWS:
        raise ApiError(400, f"Import at most {MAX_IMPORT_ROWS} rows at a time")
    dry_run = body.get("dryRun", True) is not False
    profiles = common.list_profiles()
    by_email = {(p.get("email") or "").lower(): p for p in profiles}
    proposed = {p["userId"]: dict(p) for p in profiles}

    results, changes = [], []
    for number, row in enumerate(rows, start=1):
        email = common.clean_text((row or {}).get("email"), 254).lower()
        result = {"row": number, "email": email, "ok": False}
        target = by_email.get(email)
        if not target:
            result["error"] = "No user with this email"
            results.append(result)
            continue
        manager_email = common.clean_text(row.get("managerEmail"), 254).lower()
        manager = by_email.get(manager_email) if manager_email else None
        if manager_email and not manager:
            result["error"] = f"Manager {manager_email} not found"
            results.append(result)
            continue
        new = dict(proposed[target["userId"]])
        new["managerId"] = manager["userId"] if manager else None
        if "department" in row:
            new["department"] = common.clean_text(row.get("department"), 60)
        if "jobTitle" in row:
            new["jobTitle"] = common.clean_text(row.get("jobTitle"), 60)
        proposed[target["userId"]] = new
        changes.append((number, target, new))
        result.update(ok=True, managerEmail=manager_email or None)
        results.append(result)

    # Validate every manager against the final shape of the tree, so loops across rows are caught.
    for number, target, new in changes:
        try:
            validate_manager(target["userId"], new.get("managerId"), proposed)
        except ApiError as err:
            entry = next(r for r in results if r["row"] == number)
            entry.update(ok=False, error=err.message)
    failed = [r for r in results if not r["ok"]]
    if failed or dry_run:
        return common.response(200 if not failed else 422, {
            "dryRun": dry_run, "applied": 0, "results": results,
            "message": f"{len(failed)} row(s) need fixing" if failed else f"{len(changes)} row(s) ready to import",
        })

    applied = 0
    for _, target, new in changes:
        fields = ("managerId", "department", "jobTitle")
        if all((new.get(f) or None) == (target.get(f) or None) for f in fields):
            continue
        updated = build_profile(target, managerId=new.get("managerId"), department=new.get("department"),
                                jobTitle=new.get("jobTitle"), updatedAt=common.now_ms(), updatedBy=actor["userId"],
                                updatedByEmail=actor.get("email", ""), version=int(target.get("version") or 0) + 1)
        common.transact([common.versioned_put(updated, target.get("version")),
                         common.audit_op(f"USER#{target['userId']}", actor, "ACCESS_CHANGED",
                                         "Reporting line updated by CSV import", before=snapshot(target),
                                         after=snapshot(updated))])
        applied += 1
    return common.response(200, {"dryRun": False, "applied": applied, "results": results,
                                 "message": f"Updated {applied} user(s)"})
