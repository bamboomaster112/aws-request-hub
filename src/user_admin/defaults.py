"""Catalogue and starting configuration.

Admins change forms, workflows and approver groups at runtime from the web app.
These defaults are only written when a module has no configuration yet
(scripts/seed-config.py runs them at deploy time and never overwrites edits).
"""

MODULES = {
    "PURCHASE": "Purchasing",
    "ACCESS": "Access management",
    "LEAVE": "Leave",
    "CHANGE": "Change management",
    "GENERAL": "General requests",
}

ROLES = {"requesters": "Requester", "approvers": "Approver", "admins": "Admin"}

FIELD_TYPES = {
    "text": "Short text",
    "textarea": "Long text",
    "number": "Number",
    "amount": "Money amount",
    "date": "Date",
    "select": "Dropdown",
    "checkbox": "Checkbox",
}

LEVEL_KINDS = {
    "MANAGER": "Requester's line manager",
    "GROUP": "Approver group",
    "ADMINS": "Admins",
}

DEFAULT_GROUPS = [
    {"groupId": "finance", "name": "Finance", "description": "Approves purchases and spend above the finance threshold."},
    {"groupId": "it-security", "name": "IT security", "description": "Reviews requests for system and data access."},
    {"groupId": "hr", "name": "HR", "description": "Reviews long leave."},
    {"groupId": "change-board", "name": "Change advisory board", "description": "Reviews production changes."},
]


def _f(key, label, ftype, required=False, **extra):
    field = {"key": key, "label": label, "type": ftype, "required": required}
    field.update(extra)
    return field


DEFAULT_FORMS = {
    "PURCHASE": {
        "approvalField": "amount",
        "fields": [
            _f("item", "What are you buying?", "text", True, maxLength=120),
            _f("vendor", "Vendor", "text", False, maxLength=120),
            _f("category", "Category", "select", True, options=["Hardware", "Software", "Services", "Travel", "Other"]),
            _f("amount", "Total cost", "amount", True, min=0, max=1000000, help="Including tax and delivery."),
            _f("neededBy", "Needed by", "date", False),
            _f("justification", "Business justification", "textarea", True, maxLength=2000),
        ],
    },
    "ACCESS": {
        "approvalField": "durationDays",
        "fields": [
            _f("system", "System", "select", True, options=["AWS console", "GitHub", "Jira", "Production database", "VPN", "Other"]),
            _f("accessLevel", "Access level", "select", True, options=["Read only", "Read and write", "Administrator"]),
            _f("durationDays", "Duration (days)", "number", True, min=1, max=365, help="Access is removed automatically after this."),
            _f("justification", "Why do you need it?", "textarea", True, maxLength=2000),
        ],
    },
    "LEAVE": {
        "approvalField": "days",
        "fields": [
            _f("leaveType", "Leave type", "select", True, options=["Annual", "Sick", "Parental", "Unpaid", "Other"]),
            _f("startDate", "First day", "date", True),
            _f("endDate", "Last day", "date", True),
            _f("days", "Working days", "number", True, min=0.5, max=90),
            _f("handover", "Handover notes", "textarea", False, maxLength=2000),
        ],
    },
    "CHANGE": {
        "approvalField": "riskScore",
        "fields": [
            _f("system", "System or service", "text", True, maxLength=120),
            _f("environment", "Environment", "select", True, options=["Production", "Staging", "Development"]),
            _f("changeWindow", "Change window", "date", True),
            _f("riskScore", "Risk score (1 to 10)", "number", True, min=1, max=10, help="7 or more goes to admins as well."),
            _f("rollbackPlan", "Rollback plan", "textarea", True, maxLength=2000),
            _f("tested", "Tested in staging", "checkbox", False),
        ],
    },
    "GENERAL": {
        "approvalField": "amount",
        "fields": [
            _f("amount", "Value (if any)", "amount", True, min=0, max=1000000, help="Enter 0 if nothing is being spent."),
            _f("details", "Details", "textarea", True, maxLength=2000),
        ],
    },
}

DEFAULT_WORKFLOWS = {
    "PURCHASE": [
        {"kind": "MANAGER", "minAmount": 0},
        {"kind": "GROUP", "groupId": "finance", "minAmount": 1000},
        {"kind": "ADMINS", "minAmount": 10000},
    ],
    "ACCESS": [
        {"kind": "MANAGER", "minAmount": 0},
        {"kind": "GROUP", "groupId": "it-security", "minAmount": 0},
    ],
    "LEAVE": [
        {"kind": "MANAGER", "minAmount": 0},
        {"kind": "GROUP", "groupId": "hr", "minAmount": 15},
    ],
    "CHANGE": [
        {"kind": "MANAGER", "minAmount": 0},
        {"kind": "GROUP", "groupId": "change-board", "minAmount": 0},
        {"kind": "ADMINS", "minAmount": 7},
    ],
    "GENERAL": [
        {"kind": "MANAGER", "minAmount": 0},
        {"kind": "ADMINS", "minAmount": 10000},
    ],
}
