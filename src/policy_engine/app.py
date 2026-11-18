"""Policy engine for the Request Hub approval workflow.

Decides the risk tier, who must approve, and the approval SLA for a request.
The rules live in SSM Parameter Store so thresholds can change without a
deployment. Step Functions calls this once per request, which keeps the
function far below the playground's 300 invocations per hour limit.
"""
import json
import os
import time

import boto3

VALID_TYPES = {"PURCHASE", "ACCESS", "LEAVE", "CHANGE", "GENERAL"}
CACHE_SECONDS = 60
MIN_TIMER_SECONDS = 60

_ssm = None
_cache = {"rules": None, "loaded_at": 0.0}


def _ssm_client():
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm")
    return _ssm


def load_rules():
    """Read the rules document, cached for a minute per warm container."""
    now = time.time()
    if _cache["rules"] is None or now - _cache["loaded_at"] > CACHE_SECONDS:
        raw = _ssm_client().get_parameter(Name=os.environ["POLICY_PARAMETER"])["Parameter"]["Value"]
        _cache["rules"] = json.loads(raw)
        _cache["loaded_at"] = now
    return _cache["rules"]


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def evaluate(request, rules):
    """Pure function: request attributes + rules -> policy decision."""
    req_type = str(request.get("type", "GENERAL")).upper()
    if req_type not in VALID_TYPES:
        req_type = "GENERAL"
    amount = _as_float(request.get("amount"))

    policy = dict(rules.get("default", {}))
    policy.update(rules.get("types", {}).get(req_type, {}))

    auto_max = policy.get("autoApproveMax", -1)
    high_min = policy.get("highRiskMin")
    reasons = []

    if high_min is not None and amount >= _as_float(high_min):
        tier, group, auto = "HIGH", policy.get("highRiskApproverGroup", "admins"), False
        reasons.append(
            f"{req_type} value {amount:g} is at or above the high-risk threshold of {_as_float(high_min):g}, "
            f"so it needs the {group} group"
        )
    elif auto_max is not None and _as_float(auto_max, -1) >= 0 and amount <= _as_float(auto_max):
        tier, group, auto = "LOW", "policy", True
        reasons.append(f"{req_type} value {amount:g} is within the auto-approval limit of {_as_float(auto_max):g}")
    else:
        tier, group, auto = "MEDIUM", policy.get("approverGroup", "approvers"), False
        if auto_max is None or _as_float(auto_max, -1) < 0:
            reasons.append(f"{req_type} requests always need a human approver")
        else:
            reasons.append(
                f"{req_type} value {amount:g} is above the auto-approval limit of {_as_float(auto_max):g}"
            )

    return {
        "riskTier": tier,
        "autoApprove": auto,
        "approverGroup": group,
        "slaSeconds": max(MIN_TIMER_SECONDS, int(policy.get("slaSeconds", 300))),
        "escalationSeconds": max(MIN_TIMER_SECONDS, int(policy.get("escalationSeconds", 300))),
        "reasons": reasons,
        "rulesVersion": str(rules.get("version", "unversioned")),
    }


def _emit_metric(tier, auto_approved):
    """CloudWatch Embedded Metric Format: a business KPI with no PutMetricData call."""
    print(json.dumps({
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": os.environ.get("METRICS_NAMESPACE", "RequestHub"),
                "Dimensions": [["RiskTier"]],
                "Metrics": [{"Name": "PolicyEvaluations", "Unit": "Count"}],
            }],
        },
        "RiskTier": tier,
        "PolicyEvaluations": 1,
        "autoApproved": auto_approved,
    }))


def handler(event, _context):
    request = event.get("request", event)
    result = evaluate(request, load_rules())
    _emit_metric(result["riskTier"], result["autoApprove"])
    print(json.dumps({
        "level": "INFO",
        "message": "policy evaluated",
        "requestId": request.get("requestId"),
        "executionId": event.get("executionId"),
        **result,
    }))
    return result
