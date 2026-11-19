#!/usr/bin/env python3
"""Sets up a hands-on demo org for testing the web app yourself.

Creates four people from one mailbox with plus addressing (Cognito emails each a temporary
password), sets their reporting lines through the admin API, makes the Finance person the
member of every approver group, relaxes the SLA timers so there is time to switch users,
and removes the scripted @example.com test users so every approval lands with someone you
can sign in as. Run it after the smoke test (it uses the test admin to call the admin API).

  python3 scripts/setup-demo-users.py you@gmail.com        # you+admin@, you+manager@, ...
  python3 scripts/setup-demo-users.py you@gmail.com --keep-test-users --timer 900
"""
import argparse
import os
import sys

import hubkit

# key, role, name, department, job title, reports to
PEOPLE = [
    ("admin", "admins", "Demo Admin", "IT", "Platform owner", None),
    ("manager", "approvers", "Demo Manager", "Engineering", "Engineering manager", "admin"),
    ("finance", "approvers", "Demo Finance", "Finance", "Finance controller", "admin"),
    ("requester", "requesters", "Demo Requester", "Engineering", "Software engineer", "manager"),
]


def plus(email, tag):
    local, domain = email.split("@", 1)
    return f"{local.split('+')[0]}+{tag}@{domain}".lower()


def ensure_user(out, email, role):
    user_id = hubkit.user_id_for(out, email)
    if user_id:
        print(f"  {email} already exists; updating it")
        return user_id
    created = hubkit.cognito.admin_create_user(
        UserPoolId=out["UserPoolId"], Username=email, DesiredDeliveryMediums=["EMAIL"],
        UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}],
    )["User"]
    hubkit.cognito.admin_add_user_to_group(UserPoolId=out["UserPoolId"], Username=created["Username"], GroupName=role)
    user_id = {a["Name"]: a["Value"] for a in created["Attributes"]}["sub"]
    hubkit.put_profile(out, user_id, created["Username"], email, role, hubkit.ALL_MODULES, actor="setup-demo-users.py")
    print(f"  created {email}; Cognito has emailed a temporary password")
    return user_id


def must(result, what):
    status, body = result
    if status >= 300:
        sys.exit(f"{what} failed: {status} {body}")
    return body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("email", help="your mailbox, e.g. you@gmail.com")
    parser.add_argument("--timer", type=int, default=1800, help="SLA and escalation seconds per level (default 1800)")
    parser.add_argument("--keep-test-users", action="store_true", help="keep the @example.com smoke test users")
    args = parser.parse_args()

    out = hubkit.outputs()
    api = out["ApiBaseUrl"]
    test_users = hubkit.load_users()
    token = hubkit.sign_in(out, test_users["admin"])

    def call(method, path, body=None):
        return hubkit.http(method, f"{api}{path}", token, body)

    print("People")
    ids = {}
    for key, role, name, department, title, manager in PEOPLE:
        email = plus(args.email, key)
        ids[key] = ensure_user(out, email, role)
        body = {"role": role, "name": name, "department": department, "jobTitle": title,
                "modules": hubkit.ALL_MODULES, "managerId": ids[manager] if manager else None}
        must(call("PATCH", f"/admin/users/{ids[key]}", body), f"Updating {email}")

    print("Approver groups and workflows")
    config = must(call("GET", "/admin/config"), "Reading the configuration")
    for group in config["groups"]:
        body = {"name": group["name"], "description": group.get("description", ""), "members": [ids["finance"]]}
        if group.get("unsaved"):
            must(call("POST", "/admin/groups", {**body, "groupId": group["groupId"]}), f"Creating {group['name']}")
        else:
            must(call("PUT", f"/admin/groups/{group['groupId']}", {**body, "version": group["version"]}), f"Saving {group['name']}")
        print(f"  {group['name']}: {plus(args.email, 'finance')}")
    for module, workflow in config["workflows"].items():
        levels = [{k: v for k, v in level.items() if k in ("kind", "minAmount", "groupId")} for level in workflow["levels"]]
        body = {"levels": levels, "slaSeconds": args.timer, "escalationSeconds": args.timer}
        if not workflow.get("unsaved"):
            body["version"] = workflow["version"]
        saved = must(call("PUT", f"/admin/workflows/{module}", body), f"Saving the {module} workflow")
        print(f"  {saved.get('message') or module} ({args.timer // 60} min per level)")

    if not args.keep_test_users:
        print("Removing the @example.com smoke test users")
        for user in test_users.values():
            try:
                hubkit.cognito.admin_delete_user(UserPoolId=out["UserPoolId"], Username=user["username"])
            except hubkit.ClientError as exc:
                if exc.response["Error"]["Code"] != "UserNotFoundException":
                    raise
            hubkit.delete_profile(out, user["userId"])
            print(f"  removed {user['username']}")
        os.remove(hubkit.USERS_FILE)
        print("  (run scripts/seed-users.py again before the next smoke test)")

    print(f"\nSign in at {out['AppUrl']}")
    for key, role, _, _, _, manager in PEOPLE:
        line = f"  {plus(args.email, key):<40} {role:<11}"
        print(line + (f" reports to {plus(args.email, manager)}" if manager else ""))


if __name__ == "__main__":
    main()
