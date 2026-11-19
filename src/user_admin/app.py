"""Admin API (ANY /admin/{proxy+}). Admins only, checked against the live profile.

Users and access
  GET    /admin/users                              directory with role, modules, manager, status
  POST   /admin/users                              invite a user
  POST   /admin/users/import                       bulk reporting lines from CSV rows (dry run first)
  GET    /admin/users/{userId}                     one user, reports, groups, auto-approval rules, history
  PATCH  /admin/users/{userId}                     name, role, modules, manager, department, job title
  DELETE /admin/users/{userId}                     delete (requests and history are kept)
  POST   /admin/users/{userId}/actions             DISABLE | ENABLE | RESET_PASSWORD | RESEND_INVITE | RESET_MFA | SIGN_OUT
  PUT    /admin/users/{userId}/auto-rules/{module} grant auto-approval up to a limit until a date
  DELETE /admin/users/{userId}/auto-rules/{module} revoke it

Configuration
  GET    /admin/config                             forms, workflows, groups, auto-approval rules, history
  PUT    /admin/forms/{module}                     save a new form version
  PUT    /admin/workflows/{module}                 save approval levels and timers
  POST   /admin/groups                             create an approver group
  PUT    /admin/groups/{groupId}                   rename it or change members
  DELETE /admin/groups/{groupId}                   delete it (refused while a workflow uses it)
"""
import json
import re

from botocore.exceptions import ClientError

import common
import config_admin
import users
from common import ApiError

USER_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


def actor_from(event):
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    sub = claims.get("sub")
    if not sub:
        raise ApiError(401, "Unauthorized")
    profile = common.load_profile(sub)
    # The live profile decides, not the token: a demoted or disabled admin loses access at once.
    if not profile or profile.get("role") != "admins" or profile.get("status") != "ACTIVE":
        raise ApiError(403, "Only active admins can use the admin area")
    return {"userId": sub, "email": claims.get("email") or profile.get("email", "")}


def route_parts(event):
    proxy = (event.get("pathParameters") or {}).get("proxy")
    if proxy is None:
        path = event.get("path") or ""
        proxy = path.split("/admin/", 1)[1] if "/admin/" in path else ""
    return [p for p in proxy.strip("/").split("/") if p]


def user_id_or_404(value):
    if not USER_ID_RE.match(value or ""):
        raise ApiError(404, "User not found")
    return value


def dispatch(method, parts, body, actor):
    head = parts[0] if parts else ""
    n = len(parts)
    if head == "users":
        if n == 1 and method == "GET":
            return users.list_users()
        if n == 1 and method == "POST":
            return users.create_user(body, actor)
        if n == 2 and parts[1] == "import" and method == "POST":
            return users.import_hierarchy(body, actor)
        if n >= 2:
            user_id = user_id_or_404(parts[1])
            if n == 2 and method == "GET":
                return users.get_user(user_id)
            if n == 2 and method == "PATCH":
                return users.update_user(user_id, body, actor)
            if n == 2 and method == "DELETE":
                return users.delete_user(user_id, actor)
            if n == 3 and parts[2] == "actions" and method == "POST":
                return users.run_action(user_id, body, actor)
            if n == 4 and parts[2] == "auto-rules" and method == "PUT":
                return users.set_auto_rule(user_id, parts[3], body, actor)
            if n == 4 and parts[2] == "auto-rules" and method == "DELETE":
                return users.delete_auto_rule(user_id, parts[3], actor)
    if head == "config" and n == 1 and method == "GET":
        return config_admin.get_config()
    if head == "forms" and n == 2 and method == "PUT":
        return config_admin.save_form(parts[1], body, actor)
    if head == "workflows" and n == 2 and method == "PUT":
        return config_admin.save_workflow(parts[1], body, actor)
    if head == "groups":
        if n == 1 and method == "POST":
            return config_admin.save_group(None, body, actor, create=True)
        if n == 2 and method == "PUT":
            return config_admin.save_group(parts[1], body, actor, create=False)
        if n == 2 and method == "DELETE":
            return config_admin.delete_group(parts[1], actor)
    return common.response(404, {"message": "Not found"})


def handler(event, _context):
    try:
        actor = actor_from(event)
        try:
            body = json.loads(event.get("body") or "{}")
        except ValueError as exc:
            raise ApiError(400, "Body must be JSON") from exc
        if not isinstance(body, dict):
            raise ApiError(400, "Body must be a JSON object")
        return dispatch(str(event.get("httpMethod", "")).upper(), route_parts(event), body, actor)
    except ApiError as err:
        payload = {"message": err.message}
        if err.details is not None:
            payload["details"] = err.details
        return common.response(err.status, payload)
    except ClientError as exc:
        print(json.dumps({"level": "ERROR", "message": "aws error", "error": str(exc)}))
        return common.response(500, {"message": "The change could not be saved, try again"})
