#!/usr/bin/env python3
"""Writes the starting forms, approval workflows and approver groups.

Safe to re-run: an item is only written when it does not exist yet, so anything an
admin has changed in the web app is never overwritten. deploy.sh runs this after
every stack deployment. The defaults live in src/user_admin/defaults.py, the same
module the admin API uses, so there is one source of truth.

  python3 scripts/seed-config.py
"""
import json
import os
import pathlib
import sys
import time
from decimal import Decimal

from botocore.exceptions import ClientError

import hubkit

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src" / "user_admin"))
import defaults  # noqa: E402

ACTOR = "seed-config.py"
TIMER = int(os.environ.get("SLA_SECONDS", "180"))


def decimals(value):
    return json.loads(json.dumps(value), parse_float=Decimal)


def put_if_missing(table, item, label):
    try:
        table.put_item(Item=decimals(item), ConditionExpression="attribute_not_exists(PK)")
        print(f"  created {label}")
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        print(f"  kept    {label} (already configured)")
        return False


def main():
    out = hubkit.outputs()
    table = hubkit.session.resource("dynamodb").Table(out["TableName"])
    now = int(time.time() * 1000)
    stamp = {"updatedAt": now, "updatedBy": ACTOR, "updatedByEmail": ACTOR}
    print(f"Seeding configuration in {out['TableName']}")
    created = 0
    for group in defaults.DEFAULT_GROUPS:
        created += put_if_missing(table, {"PK": "CONFIG", "SK": f"GROUP#{group['groupId']}", "entity": "APPROVER_GROUP",
                                          **group, "members": [], "version": 1, **stamp}, f"approver group {group['name']}")
    for module, form in defaults.DEFAULT_FORMS.items():
        item = {"PK": "CONFIG", "SK": f"FORM#{module}", "entity": "FORM", "module": module, **form, "version": 1, **stamp}
        if put_if_missing(table, item, f"{defaults.MODULES[module]} form"):
            created += 1
            put_if_missing(table, {**item, "SK": f"FORMVER#{module}#000001", "entity": "FORM_VERSION"},
                           f"{defaults.MODULES[module]} form version 1 snapshot")
    for module, levels in defaults.DEFAULT_WORKFLOWS.items():
        created += put_if_missing(table, {"PK": "CONFIG", "SK": f"WORKFLOW#{module}", "entity": "WORKFLOW", "module": module,
                                          "levels": levels, "slaSeconds": TIMER, "escalationSeconds": TIMER, "version": 1,
                                          **stamp}, f"{defaults.MODULES[module]} workflow")
    print(f"Done: {created} item(s) created. Admins change all of this from Admin > Workflows, Forms and Groups.")
    if created:
        print("Approver groups start empty, so their levels fall back to admins until you add members.")


if __name__ == "__main__":
    try:
        main()
    except ClientError as exc:
        sys.exit(f"AWS error: {exc}")
