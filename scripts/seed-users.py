#!/usr/bin/env python3
"""Creates the test users (requester, approver and two admins), sets strong random
passwords, enrols each one in TOTP MFA, and stores the credentials in a
private file in the CloudShell home directory for the smoke and UI tests.

The second admin exists because one person never approves two levels of the same
request: a Change request at risk 8 goes to the line manager, then the Change
advisory board (admins stand in while it has no members), then admins, and that
needs two different admins after the manager.

Nobody types a password: they are generated here and never printed. Safe to re-run: the test users are recreated from scratch,
because an existing TOTP enrolment cannot be reset through the admin API. With --missing-only, users that
already exist are kept and only the missing ones are created.

  python3 scripts/seed-users.py                  # recreate every test user
  python3 scripts/seed-users.py --missing-only   # add any test user that is missing, keep the rest
"""
import argparse
import json
import secrets as pysecrets
import string
import sys

from botocore.exceptions import ClientError

import hubkit

SYMBOLS = "!@#%^*-_=+"
# A small reporting line for the smoke test: requester -> approver -> admin, plus a second admin.
TEST_USERS = [
    ("admin", "admin@example.com", ["admins"], None),
    ("admin2", "admin2@example.com", ["admins"], None),
    ("approver", "approver@example.com", ["approvers"], "admin"),
    ("requester", "requester@example.com", ["requesters"], "approver"),
]


def strong_password(length=20):
    alphabet = string.ascii_letters + string.digits + SYMBOLS
    while True:
        pw = "".join(pysecrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in SYMBOLS for c in pw)):
            return pw


def recreate_user(out, email, groups, manager_id=None):
    pool = out["UserPoolId"]
    previous = hubkit.user_id_for(out, email)
    if previous:
        hubkit.delete_profile(out, previous)
        hubkit.cognito.admin_delete_user(UserPoolId=pool, Username=email)
        print(f"  removed previous {email}")
    created = hubkit.cognito.admin_create_user(
        UserPoolId=pool, Username=email, MessageAction="SUPPRESS",
        UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}],
    )["User"]
    for group in groups:
        hubkit.cognito.admin_add_user_to_group(UserPoolId=pool, Username=email, GroupName=group)
    password = strong_password()
    hubkit.cognito.admin_set_user_password(UserPoolId=pool, Username=email, Password=password, Permanent=True)
    user_id = {a["Name"]: a["Value"] for a in created["Attributes"]}.get("sub", created["Username"])
    # Test users hold every module so the smoke test can raise any request type.
    hubkit.put_profile(out, user_id, created["Username"], email, groups[0], hubkit.ALL_MODULES,
                       name=f"Test {groups[0][:-1]}", actor="seed-users.py", manager_id=manager_id, department="Smoke test")
    print(f"  created {email} in {', '.join(groups)} with an access profile")
    return password, user_id


def enrol_mfa(out, email, password):
    """With MFA set to ON, the first sign-in returns MFA_SETUP: associate a TOTP secret and verify it."""
    resp = hubkit.cognito.admin_initiate_auth(
        UserPoolId=out["UserPoolId"], ClientId=out["WebClientId"], AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": email, "PASSWORD": password},
    )
    if resp.get("ChallengeName") != "MFA_SETUP":
        raise RuntimeError(f"Expected MFA_SETUP for {email}, got {resp.get('ChallengeName')}")
    assoc = hubkit.cognito.associate_software_token(Session=resp["Session"])
    secret = assoc["SecretCode"]
    verify = hubkit.cognito.verify_software_token(
        Session=assoc["Session"], UserCode=hubkit.totp(secret), FriendlyDeviceName="cloudshell-tests")
    if verify.get("Status") != "SUCCESS":
        raise RuntimeError(f"TOTP verification failed for {email}")
    done = hubkit.cognito.admin_respond_to_auth_challenge(
        UserPoolId=out["UserPoolId"], ClientId=out["WebClientId"], ChallengeName="MFA_SETUP",
        Session=verify["Session"], ChallengeResponses={"USERNAME": email},
    )
    if "AuthenticationResult" not in done:
        raise RuntimeError(f"MFA setup did not complete for {email}: {done.get('ChallengeName')}")
    return secret


def stored_users():
    try:
        with open(hubkit.USERS_FILE) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def main():
    parser = argparse.ArgumentParser(description="Create the test users with MFA enrolled.")
    parser.add_argument("--missing-only", action="store_true",
                        help="keep test users that already exist and only create the missing ones")
    args = parser.parse_args()
    out = hubkit.outputs()
    stored = stored_users() if args.missing_only else {}
    users = {}
    print(f"Seeding test users in {out['UserPoolId']}")
    for role, email, groups, manager in TEST_USERS:
        kept = stored.get(role)
        if kept and kept.get("userId") and kept.get("username") == email and hubkit.user_id_for(out, email) == kept["userId"]:
            users[role] = kept
            print(f"  {role}: kept {email}")
            continue
        manager_id = users[manager]["userId"] if manager else None
        password, user_id = recreate_user(out, email, groups, manager_id)
        users[role] = {"username": email, "password": password, "groups": groups, "userId": user_id,
                       "totpSecret": enrol_mfa(out, email, password)}
        print(f"  {role}: MFA enrolled" + (f", reports to {manager}" if manager else ""))
    hubkit.save_users(users)
    print(f"Stored credentials (mode 600) in {hubkit.USERS_FILE}")
    print("For the web app, create your own admin (Cognito emails a temporary password and you enrol MFA")
    print("with your authenticator app):  ./scripts/create-user.sh you@yourdomain.com admins")
    print("Then invite everyone else from the Users page in the web app.")


if __name__ == "__main__":
    try:
        main()
    except ClientError as exc:
        sys.exit(f"AWS error: {exc}")
