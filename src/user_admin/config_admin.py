"""Module forms (versioned), approval workflows and approver groups."""
import re

import common
from common import ApiError
from defaults import DEFAULT_FORMS, DEFAULT_GROUPS, DEFAULT_WORKFLOWS, FIELD_TYPES, LEVEL_KINDS, MODULES
import users

KEY_RE = re.compile(r"^[a-z][a-zA-Z0-9_]{0,39}$")
GROUP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
RESERVED_KEYS = {"title", "type", "currency", "requestid", "status"}
MAX_FIELDS = 20
MAX_LEVELS = 5
MAX_OPTIONS = 30
MIN_TIMER, MAX_TIMER = 60, 7 * 24 * 3600
DEFAULT_TIMER = 180


def config_event(action, subject, actor, detail):
    common.publish("request-hub.config", "Configuration Changed", {
        "version": "1", "action": action, "subject": subject,
        "actorId": actor["userId"], "actorEmail": actor.get("email", ""), **detail,
    })


def _number(value, name):
    if isinstance(value, bool) or value is None:
        raise ApiError(400, f"{name} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, f"{name} must be a number") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ApiError(400, f"{name} must be a number")
    return int(number) if number.is_integer() else number


# ---------------------------------------------------------------- forms
def validate_field(raw, index):
    if not isinstance(raw, dict):
        raise ApiError(400, f"Field {index} must be an object")
    where = f"Field {index}"
    key = str(raw.get("key", "")).strip()
    if not KEY_RE.match(key) or key.lower() in RESERVED_KEYS:
        raise ApiError(400, f"{where}: key '{key}' must start with a letter, use letters, digits or _ "
                            f"(40 at most), and not be one of {', '.join(sorted(RESERVED_KEYS))}")
    label = common.clean_text(raw.get("label"), 60)
    if not label:
        raise ApiError(400, f"{where}: a label is required")
    ftype = str(raw.get("type", "")).strip()
    if ftype not in FIELD_TYPES:
        raise ApiError(400, f"{where} ({label}): type must be one of {', '.join(FIELD_TYPES)}")
    field = {"key": key, "label": label, "type": ftype, "required": raw.get("required") is True}
    help_text = common.clean_text(raw.get("help"), 160)
    if help_text:
        field["help"] = help_text
    if ftype in ("text", "textarea"):
        limit = raw.get("maxLength", 120 if ftype == "text" else 2000)
        limit = int(_number(limit, f"{label} maxLength"))
        if not 1 <= limit <= (200 if ftype == "text" else 4000):
            raise ApiError(400, f"{where} ({label}): maxLength is out of range")
        field["maxLength"] = limit
    if ftype in ("number", "amount"):
        low = _number(raw.get("min", 0), f"{label} min")
        high = _number(raw.get("max", 1_000_000), f"{label} max")
        if low > high:
            raise ApiError(400, f"{where} ({label}): min must not be greater than max")
        if low < -1_000_000_000 or high > 1_000_000_000:
            raise ApiError(400, f"{where} ({label}): limits must be within one billion")
        field["min"], field["max"] = low, high
    if ftype == "select":
        options = raw.get("options")
        if not isinstance(options, list):
            raise ApiError(400, f"{where} ({label}): options must be a list")
        cleaned = []
        for option in options:
            text = common.clean_text(option, 60)
            if text and text not in cleaned:
                cleaned.append(text)
        if not 1 <= len(cleaned) <= MAX_OPTIONS:
            raise ApiError(400, f"{where} ({label}): give between 1 and {MAX_OPTIONS} options")
        field["options"] = cleaned
    return field


def validate_form(module, body):
    fields_raw = body.get("fields")
    if not isinstance(fields_raw, list) or not fields_raw:
        raise ApiError(400, "A form needs at least one field")
    if len(fields_raw) > MAX_FIELDS:
        raise ApiError(400, f"A form can have at most {MAX_FIELDS} fields")
    fields = [validate_field(f, i) for i, f in enumerate(fields_raw, start=1)]
    keys = [f["key"] for f in fields]
    duplicates = {k for k in keys if keys.count(k) > 1}
    if duplicates:
        raise ApiError(400, f"Field keys must be unique: {', '.join(sorted(duplicates))}")
    approval_field = body.get("approvalField") or None
    if approval_field is not None:
        match = next((f for f in fields if f["key"] == approval_field), None)
        if not match or match["type"] not in ("number", "amount"):
            raise ApiError(400, "The approval value must be a Number or Money amount field on this form")
        if not match["required"]:
            raise ApiError(400, f"'{match['label']}' drives approval limits, so it must be required")
    return {"module": module, "fields": fields, "approvalField": approval_field}


def save_form(module, body, actor):
    module = str(module).upper()
    if module not in MODULES:
        raise ApiError(404, "Unknown module")
    form = validate_form(module, body)
    current = common.load_config(f"FORM#{module}")
    expected = body.get("version")
    if current and expected is not None and int(expected) != int(current.get("version") or 0):
        raise ApiError(409, "Another admin saved this form while you were editing. Reload to see their version.")
    if not form["approvalField"]:
        workflow = common.load_config(f"WORKFLOW#{module}")
        if workflow and any(float(level.get("minAmount") or 0) > 0 for level in workflow.get("levels", [])):
            raise ApiError(409, "This module's workflow uses amount thresholds, so the form needs an approval value field. "
                                "Remove the thresholds first or keep an approval value.")
    version = int((current or {}).get("version") or 0) + 1
    ts = common.now_ms()
    item = {"PK": common.CONFIG_PK, "SK": f"FORM#{module}", "entity": "FORM", **form, "version": version,
            "updatedAt": ts, "updatedBy": actor["userId"], "updatedByEmail": actor.get("email", "")}
    snapshot = {**item, "SK": f"FORMVER#{module}#{version:06d}", "entity": "FORM_VERSION"}
    summary = f"{MODULES[module]} form saved as version {version} ({len(form['fields'])} fields)"
    common.transact([
        common.versioned_put(item, (current or {}).get("version"), create=current is None),
        common.put_op(snapshot, "attribute_not_exists(PK)"),
        common.audit_op(common.CONFIG_PK, actor, "FORM_SAVED", summary, subject=module),
    ], "Another admin saved this form a moment ago. Reload to see their version.")
    config_event("FORM_SAVED", module, actor, {"formVersion": version})
    return common.response(200, {"form": form_view(item), "message": summary})


def form_view(item):
    view = common.strip_keys(item)
    view.setdefault("version", 0)
    return view


# ---------------------------------------------------------------- workflows
def validate_workflow(module, body, groups_by_id, form):
    levels_raw = body.get("levels")
    if not isinstance(levels_raw, list) or not levels_raw:
        raise ApiError(400, "A workflow needs at least one approval level")
    if len(levels_raw) > MAX_LEVELS:
        raise ApiError(400, f"A workflow can have at most {MAX_LEVELS} levels")
    levels, used_groups = [], set()
    for index, raw in enumerate(levels_raw, start=1):
        if not isinstance(raw, dict):
            raise ApiError(400, f"Level {index} must be an object")
        kind = str(raw.get("kind", "")).upper()
        if kind not in LEVEL_KINDS:
            raise ApiError(400, f"Level {index}: kind must be one of {', '.join(LEVEL_KINDS)}")
        if kind == "MANAGER" and index != 1:
            raise ApiError(400, "The line manager can only be level 1 (manager first, then groups)")
        minimum = _number(raw.get("minAmount", 0), f"Level {index} threshold")
        if minimum < 0:
            raise ApiError(400, f"Level {index}: the threshold cannot be negative")
        level = {"kind": kind, "minAmount": minimum}
        if kind == "GROUP":
            group_id = str(raw.get("groupId", ""))
            if group_id not in groups_by_id:
                raise ApiError(400, f"Level {index}: choose an existing approver group")
            if group_id in used_groups:
                raise ApiError(400, f"Level {index}: {groups_by_id[group_id].get('name')} is already a level")
            used_groups.add(group_id)
            level["groupId"] = group_id
        levels.append(level)
    if levels[0]["minAmount"] != 0:
        raise ApiError(400, "Level 1 must apply to every request (threshold 0), so nothing skips approval")
    if any(l["minAmount"] > 0 for l in levels) and not (form or {}).get("approvalField"):
        raise ApiError(400, "Thresholds need an approval value field on this module's form. Set one in the form builder first.")
    timers = {}
    for name in ("slaSeconds", "escalationSeconds"):
        value = int(_number(body.get(name, DEFAULT_TIMER), name))
        if not MIN_TIMER <= value <= MAX_TIMER:
            raise ApiError(400, f"{name} must be between {MIN_TIMER} seconds and 7 days")
        timers[name] = value
    return {"module": module, "levels": levels, **timers}


def describe_level(level, groups_by_id, field_label):
    if level["kind"] == "MANAGER":
        text = "Line manager"
    elif level["kind"] == "GROUP":
        text = (groups_by_id.get(level.get("groupId")) or {}).get("name", level.get("groupId"))
    else:
        text = "Admins"
    if level.get("minAmount"):
        text += f" when {field_label or 'the value'} is {level['minAmount']:,} or more"
    return text


def save_workflow(module, body, actor):
    module = str(module).upper()
    if module not in MODULES:
        raise ApiError(404, "Unknown module")
    groups_by_id = {g["groupId"]: g for g in common.config_items("GROUP#")}
    form = common.load_config(f"FORM#{module}") or DEFAULT_FORMS.get(module)
    workflow = validate_workflow(module, body, groups_by_id, form)
    current = common.load_config(f"WORKFLOW#{module}")
    expected = body.get("version")
    if current and expected is not None and int(expected) != int(current.get("version") or 0):
        raise ApiError(409, "Another admin saved this workflow while you were editing. Reload to see their version.")
    version = int((current or {}).get("version") or 0) + 1
    ts = common.now_ms()
    item = {"PK": common.CONFIG_PK, "SK": f"WORKFLOW#{module}", "entity": "WORKFLOW", **workflow, "version": version,
            "updatedAt": ts, "updatedBy": actor["userId"], "updatedByEmail": actor.get("email", "")}
    field_label = next((f["label"] for f in (form or {}).get("fields", []) if f["key"] == (form or {}).get("approvalField")), None)
    chain = " -> ".join(describe_level(l, groups_by_id, field_label) for l in workflow["levels"])
    summary = f"{MODULES[module]} workflow version {version}: {chain}"
    common.transact([
        common.versioned_put(item, (current or {}).get("version"), create=current is None),
        common.audit_op(common.CONFIG_PK, actor, "WORKFLOW_SAVED", summary, subject=module,
                        before=common.strip_keys(current) if current else None, after=common.strip_keys(item)),
    ], "Another admin saved this workflow a moment ago. Reload to see their version.")
    config_event("WORKFLOW_SAVED", module, actor, {"workflowVersion": version})
    return common.response(200, {"workflow": common.strip_keys(item), "message": summary})


# ---------------------------------------------------------------- approver groups
def validate_members(raw, profiles_by_id):
    if not isinstance(raw, list):
        raise ApiError(400, "members must be a list of user IDs")
    members = []
    for user_id in raw:
        profile = profiles_by_id.get(str(user_id))
        if not profile:
            raise ApiError(400, "A selected member does not exist")
        who = profile.get("email") or user_id
        if profile.get("status") != "ACTIVE":
            raise ApiError(400, f"{who} is disabled")
        if profile.get("role") not in users.MANAGER_ROLES:
            raise ApiError(400, f"{who} is a Requester. Only Approvers and Admins can be in an approver group.")
        if user_id not in members:
            members.append(user_id)
    if len(members) > 100:
        raise ApiError(400, "A group can have at most 100 members")
    return members


def save_group(group_id, body, actor, create):
    name = common.clean_text(body.get("name"), 60)
    if len(name) < 2:
        raise ApiError(400, "Give the group a name")
    if create:
        group_id = str(body.get("groupId") or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"))[:40]
    if not GROUP_ID_RE.match(group_id or ""):
        raise ApiError(400, "Group IDs use lower-case letters, digits and hyphens")
    profiles_by_id = {p["userId"]: p for p in common.list_profiles()}
    members = validate_members(body.get("members", []), profiles_by_id)
    current = common.load_config(f"GROUP#{group_id}")
    if create and current:
        raise ApiError(409, f"A group with the ID '{group_id}' already exists")
    if not create and not current:
        raise ApiError(404, "Group not found")
    expected = body.get("version")
    if current and expected is not None and int(expected) != int(current.get("version") or 0):
        raise ApiError(409, "Another admin changed this group while you were editing. Reload and try again.")
    version = int((current or {}).get("version") or 0) + 1
    item = {"PK": common.CONFIG_PK, "SK": f"GROUP#{group_id}", "entity": "APPROVER_GROUP", "groupId": group_id,
            "name": name, "description": common.clean_text(body.get("description"), 200), "members": members,
            "version": version, "updatedAt": common.now_ms(), "updatedBy": actor["userId"],
            "updatedByEmail": actor.get("email", "")}
    emails = [profiles_by_id[m].get("email") for m in members]
    summary = f"Approver group {name} {'created' if create else 'saved'} with {len(members)} member(s)"
    common.transact([
        common.versioned_put(item, (current or {}).get("version"), create=create),
        common.audit_op(common.CONFIG_PK, actor, "GROUP_SAVED", summary, subject=group_id,
                        before={"members": (current or {}).get("members", [])}, after={"members": emails}),
    ])
    config_event("GROUP_SAVED", group_id, actor, {"members": len(members)})
    return common.response(201 if create else 200, {"group": common.strip_keys(item), "message": summary})


def delete_group(group_id, actor):
    current = common.load_config(f"GROUP#{group_id}")
    if not current:
        raise ApiError(404, "Group not found")
    using = [MODULES.get(w["module"], w["module"]) for w in common.config_items("WORKFLOW#")
             if any(l.get("groupId") == group_id for l in w.get("levels", []))]
    if using:
        raise ApiError(409, f"{current.get('name')} is a level in the {', '.join(using)} workflow. Remove it there first.")
    common.transact([common.delete_op({"PK": common.CONFIG_PK, "SK": f"GROUP#{group_id}"}),
                     common.audit_op(common.CONFIG_PK, actor, "GROUP_DELETED", f"Approver group {current.get('name')} deleted",
                                     subject=group_id)])
    config_event("GROUP_DELETED", group_id, actor, {})
    return common.response(200, {"deleted": True, "groupId": group_id})


# ---------------------------------------------------------------- everything the admin pages need
def get_config():
    forms = {f["module"]: form_view(f) for f in common.config_items("FORM#")}
    workflows = {w["module"]: common.strip_keys(w) for w in common.config_items("WORKFLOW#")}
    groups = [common.strip_keys(g) for g in common.config_items("GROUP#")]
    for module in MODULES:
        if module not in forms:
            forms[module] = {"module": module, "version": 0, "unsaved": True, **DEFAULT_FORMS[module]}
        if module not in workflows:
            workflows[module] = {"module": module, "version": 0, "unsaved": True, "levels": DEFAULT_WORKFLOWS[module],
                                 "slaSeconds": DEFAULT_TIMER, "escalationSeconds": DEFAULT_TIMER}
    if not groups:
        groups = [{**g, "members": [], "version": 0, "unsaved": True} for g in DEFAULT_GROUPS]
    return common.response(200, {
        "modules": [{"id": k, "label": v} for k, v in MODULES.items()],
        "fieldTypes": [{"id": k, "label": v} for k, v in FIELD_TYPES.items()],
        "levelKinds": [{"id": k, "label": v} for k, v in LEVEL_KINDS.items()],
        "forms": forms,
        "workflows": workflows,
        "groups": sorted(groups, key=lambda g: g.get("name", "").lower()),
        "autoRules": users.list_auto_rules(),
        "history": common.history(common.CONFIG_PK, limit=30),
    })
