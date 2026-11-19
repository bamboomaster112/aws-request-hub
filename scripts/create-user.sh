#!/usr/bin/env bash
# Bootstraps a web app user from CloudShell: Cognito identity + role group + access profile.
# Cognito emails a temporary password; on first sign-in the hosted UI asks for a new
# password and shows a QR code to enrol MFA.
#
# Use it for the FIRST admin. After that, admins invite and manage everyone from the
# Users page in the web app (which also records who changed what).
#
#   ./scripts/create-user.sh you@yourdomain.com admins
#   ./scripts/create-user.sh ana@yourdomain.com approvers PURCHASE,ACCESS
#   ./scripts/create-user.sh raj@yourdomain.com requesters            # every module
#
# Roles: requesters | approvers | admins
# Modules: PURCHASE, ACCESS, LEAVE, CHANGE, GENERAL (comma separated; admins get all of them)
set -euo pipefail
EMAIL="${1:?usage: create-user.sh <email> [requesters|approvers|admins] [MODULE,MODULE,...]}"
ROLE="${2:-requesters}"
MODULES="${3:-PURCHASE,ACCESS,LEAVE,CHANGE,GENERAL}"
cd "$(dirname "$0")"

python3 - "$EMAIL" "$ROLE" "$MODULES" <<'PY'
import sys
import hubkit

email, role, modules = sys.argv[1].strip().lower(), sys.argv[2], [m.strip().upper() for m in sys.argv[3].split(",") if m.strip()]
if role not in hubkit.ROLES:
    sys.exit(f"role must be one of: {', '.join(hubkit.ROLES)}")
out = hubkit.outputs()
pool = out["UserPoolId"]
created = hubkit.cognito.admin_create_user(
    UserPoolId=pool, Username=email, DesiredDeliveryMediums=["EMAIL"],
    UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}],
)["User"]
hubkit.cognito.admin_add_user_to_group(UserPoolId=pool, Username=created["Username"], GroupName=role)
user_id = {a["Name"]: a["Value"] for a in created["Attributes"]}.get("sub", created["Username"])
hubkit.put_profile(out, user_id, created["Username"], email, role, modules, actor="create-user.sh")
what = "every module" if role == "admins" else ", ".join(sorted(set(modules))) or "no modules"
print(f"Created {email} as {role} with {what}. Cognito has emailed a temporary password (check spam).")
print(f"Sign in at: {out['AppUrl']}")
PY
