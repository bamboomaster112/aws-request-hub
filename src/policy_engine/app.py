"""Policy engine: decides auto-approval and builds the approval plan for one request.

Nothing is auto-approved by default. A request is auto-approved only when an admin
has granted its requester an auto-approval rule for that module, the rule has not
expired, and the request's approval value is within the rule's limit.

Everything else gets an approval plan from the module's workflow (configured by
admins in the web app):

  level 1   the requester's line manager (the "approver's approver" for approvers)
  level 2+  named approver groups, each switched on by an amount threshold
  fallback  admins, whenever a level has nobody eligible

Rules that hold for every plan: the requester never approves their own request;
a person who is the only approver of an earlier level is not counted for a later
one (the same person cannot approve twice); disabled people are skipped. Step
Functions calls this once per request.
"""
import json
import os
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

MANAGER_ROLES = ("approvers", "admins")
DEFAULT_WORKFLOW = {"levels": [{"kind": "MANAGER", "minAmount": 0}]}
CACHE_SECONDS = 60
MIN_TIMER_SECONDS = 60

_clients = {}
_cache = {"defaults": None, "loaded_at": 0.0}


def table():
    if "table" not in _clients:
        _clients["table"] = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _clients["table"]


def ssm():
    if "ssm" not in _clients:
        _clients["ssm"] = boto3.client("ssm")
    return _clients["ssm"]


def load_defaults():
    """Global timers from Parameter Store, cached for a minute per warm container."""
    now = time.time()
    if _cache["defaults"] is None or now - _cache["loaded_at"] > CACHE_SECONDS:
        raw = ssm().get_parameter(Name=os.environ["POLICY_PARAMETER"])["Parameter"]["Value"]
        _cache["defaults"] = json.loads(raw)
        _cache["loaded_at"] = now
    return _cache["defaults"]


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt(value):
    number = _num(value)
    return f"{int(number):,}" if number.is_integer() else f"{number:,.2f}"


def who(profile):
    return (profile or {}).get("name") or (profile or {}).get("email") or "unknown"


def active_approver(profile):
    return bool(profile) and profile.get("status") == "ACTIVE" and profile.get("role") in MANAGER_ROLES


def auto_approval(rule, requester, amount, now_ms):
    """Returns the reason string when the rule allows auto-approval, otherwise None."""
    if not rule or not requester or requester.get("status") != "ACTIVE":
        return None
    if int(_num(rule.get("expiresAt"))) <= now_ms:
        return None
    if amount > _num(rule.get("maxAmount"), -1):
        return None
    return (f"Auto-approved: an admin allowed {requester.get('email')} to self-approve {rule.get('module')} requests "
            f"up to {_fmt(rule.get('maxAmount'))} until {rule.get('expiresOn')}; this one is {_fmt(amount)}")


def build_plan(request, requester, workflow, profiles, groups, rule, now_ms, timers):
    """Pure function: everything in, the decision and the ordered approval levels out."""
    amount = _num(request.get("amount"))
    requester_id = request.get("requesterId")
    module = request.get("type")
    reasons, levels = [], []
    sla = max(MIN_TIMER_SECONDS, int(_num(workflow.get("slaSeconds"), timers.get("slaSeconds", 180))))
    escalation = max(MIN_TIMER_SECONDS, int(_num(workflow.get("escalationSeconds"), timers.get("escalationSeconds", 180))))
    version = f"workflow v{int(_num(workflow.get('version'), 0))}" if workflow.get("version") else "default workflow"
    base = {"slaSeconds": sla, "escalationSeconds": escalation, "rulesVersion": version}

    auto_reason = auto_approval(rule, requester, amount, now_ms)
    if auto_reason:
        return {**base, "autoApprove": True, "routable": True, "riskTier": "LOW", "reasons": [auto_reason],
                "levels": [], "levelCount": 0, "lastIndex": 0, "escalateTo": [], "escalateToDdb": [], "planJson": "{}"}
    if rule and int(_num(rule.get("expiresAt"))) <= now_ms:
        reasons.append(f"The requester's auto-approval for {module} expired on {rule.get('expiresOn')}")
    elif rule:
        reasons.append(f"{_fmt(amount)} is above the requester's auto-approval limit of {_fmt(rule.get('maxAmount'))}")
    else:
        reasons.append("No auto-approval rule applies, so the request follows its approval chain")

    admins = sorted(uid for uid, p in profiles.items() if p.get("role") == "admins" and p.get("status") == "ACTIVE")
    certain = set()  # people who will certainly decide an earlier level

    def eligible(candidates):
        return [c for c in candidates if c != requester_id and c not in certain and active_approver(profiles.get(c))]

    for config in workflow.get("levels") or DEFAULT_WORKFLOW["levels"]:
        kind, minimum = config.get("kind"), _num(config.get("minAmount"))
        group = groups.get(config.get("groupId")) or {}
        if kind == "MANAGER":
            label = "Line manager"
        elif kind == "GROUP":
            label = group.get("name") or config.get("groupId") or "Approver group"
        else:
            label = "Admins"
        if amount < minimum:
            reasons.append(f"{label} not needed: {_fmt(amount)} is below {_fmt(minimum)}")
            continue

        if kind == "MANAGER":
            manager_id = (requester or {}).get("managerId")
            ids = eligible([manager_id] if manager_id else [])
            if ids:
                label = f"Line manager: {who(profiles[ids[0]])}"
            elif not manager_id:
                reasons.append("The requester has no manager set, so admins approve the first level")
            else:
                reasons.append(f"The requester's manager ({who(profiles.get(manager_id))}) cannot approve "
                               f"(disabled, not an approver, or already approving), so admins step in")
        elif kind == "GROUP":
            ids = eligible(group.get("members") or [])
            if not group:
                reasons.append(f"Approver group '{config.get('groupId')}' no longer exists, so admins step in")
            elif not ids:
                reasons.append(f"{label} has no eligible member for this request, so admins step in")
        else:
            ids = eligible(admins)

        fallback = False
        if not ids:
            ids = eligible(admins)
            fallback = kind != "ADMINS"
            if fallback:
                label = f"{label} (admins standing in)"
        if not ids:
            return {**base, "autoApprove": False, "routable": False, "riskTier": "HIGH",
                    "reasons": reasons + [f"Nobody is eligible to approve level {len(levels) + 1} ({label}). "
                                          "An admin needs to set a manager, add group members or add another admin."],
                    "levels": [], "levelCount": 0, "lastIndex": 0, "escalateTo": [], "escalateToDdb": [], "planJson": "{}"}
        if len(ids) == 1:
            certain.add(ids[0])
        levels.append({
            "index": len(levels) + 1,
            "kind": kind,
            "label": label,
            "groupId": config.get("groupId"),
            "minAmount": minimum,
            "fallback": fallback,
            "approverIds": ids,
            "approverIdsDdb": [{"S": i} for i in ids],
            "approverNames": [who(profiles.get(i)) for i in ids],
        })

    escalate_to = [a for a in admins if a != requester_id]
    tier = "MEDIUM" if len(levels) == 1 else "HIGH"
    reasons.append(f"Approval chain: {' -> '.join(l['label'] for l in levels)}")
    plan = {
        "levels": [{k: l[k] for k in ("index", "kind", "label", "fallback", "approverIds", "approverNames", "minAmount")}
                   for l in levels],
        "escalateTo": escalate_to,
        "escalateToNames": [who(profiles.get(a)) for a in escalate_to],
        "slaSeconds": sla,
        "escalationSeconds": escalation,
        "version": version,
    }
    return {**base, "autoApprove": False, "routable": True, "riskTier": tier, "reasons": reasons, "levels": levels,
            "levelCount": len(levels), "lastIndex": len(levels) - 1, "escalateTo": escalate_to, "escalateToDdb": [{"S": a} for a in escalate_to],
            "planJson": json.dumps(plan)}


def _plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v) for v in value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _query_all(**kwargs):
    items = []
    while True:
        page = table().query(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def load_context(request):
    module, requester_id = request.get("type"), request.get("requesterId")
    profiles = {p["userId"]: _plain(p) for p in _query_all(
        IndexName="GSI2", KeyConditionExpression=Key("GSI2PK").eq("DIRECTORY#USERS"))}
    config = [_plain(i) for i in _query_all(KeyConditionExpression=Key("PK").eq("CONFIG") & Key("SK").begins_with("GROUP#"))]
    groups = {g["groupId"]: g for g in config}
    workflow = table().get_item(Key={"PK": "CONFIG", "SK": f"WORKFLOW#{module}"}, ConsistentRead=True).get("Item")
    rule = table().get_item(Key={"PK": f"USER#{requester_id}", "SK": f"AUTORULE#{module}"}, ConsistentRead=True).get("Item")
    return profiles.get(requester_id), _plain(workflow) if workflow else dict(DEFAULT_WORKFLOW), profiles, groups, \
        (_plain(rule) if rule else None)


def _emit_metric(tier, auto_approved, levels):
    """CloudWatch Embedded Metric Format: a business KPI with no PutMetricData call."""
    print(json.dumps({
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": os.environ.get("METRICS_NAMESPACE", "RequestHub"),
                "Dimensions": [["RiskTier"]],
                "Metrics": [{"Name": "PolicyEvaluations", "Unit": "Count"}, {"Name": "ApprovalLevels", "Unit": "Count"}],
            }],
        },
        "RiskTier": tier,
        "PolicyEvaluations": 1,
        "ApprovalLevels": levels,
        "autoApproved": auto_approved,
    }))


def handler(event, _context):
    request = event.get("request", event)
    requester, workflow, profiles, groups, rule = load_context(request)
    result = build_plan(request, requester, workflow, profiles, groups, rule, int(time.time() * 1000), load_defaults())
    _emit_metric(result["riskTier"], result["autoApprove"], result["levelCount"])
    print(json.dumps({
        "level": "INFO", "message": "approval plan built", "requestId": request.get("requestId"),
        "executionId": event.get("executionId"), "autoApprove": result["autoApprove"], "routable": result["routable"],
        "levels": [l["label"] for l in result["levels"]], "reasons": result["reasons"],
    }))
    return result
