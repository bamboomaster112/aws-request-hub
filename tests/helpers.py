"""Loads each Lambda's app.py under a unique module name (they all share the file name)."""
import importlib.util
import os
import pathlib

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TABLE_NAME", "test-table")
os.environ.setdefault("ATTACHMENTS_BUCKET", "test-bucket")
os.environ.setdefault("USER_POOL_ID", "us-east-1_test")
os.environ.setdefault("POLICY_PARAMETER", "/test/policy")
os.environ.setdefault("EVENT_BUS_NAME", "default")

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"


def load(function_dir):
    """Imports src/<function_dir>/app.py. The function's folder goes on sys.path, as it is in Lambda."""
    import sys
    folder = str(SRC / function_dir)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    path = SRC / function_dir / "app.py"
    spec = importlib.util.spec_from_file_location(f"{function_dir}_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeTable:
    """Just enough of a boto3 Table for handler tests: items keyed by (PK, SK)."""

    def __init__(self, items=(), query_items=None):
        self.items = {(i["PK"], i["SK"]): dict(i) for i in items}
        self.query_items = query_items
        self.put_item = __import__("unittest.mock", fromlist=["Mock"]).Mock()
        self.update_item = __import__("unittest.mock", fromlist=["Mock"]).Mock()

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803 (boto3 argument names)
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item else {}

    def query(self, **kwargs):
        if self.query_items is not None:
            return {"Items": list(self.query_items)}
        return {"Items": [dict(i) for i in self.items.values() if _matches(i, kwargs)]}


def _matches(item, kwargs):
    """Evaluates the boto3 Key condition objects used by the handlers (eq and begins_with, ANDed)."""
    from boto3.dynamodb.conditions import And, BeginsWith, Equals

    def check(cond):
        if isinstance(cond, And):
            return all(check(c) for c in cond._values)
        name, value = cond._values[0].name, cond._values[1]
        if isinstance(cond, Equals):
            return item.get(name) == value
        if isinstance(cond, BeginsWith):
            return str(item.get(name, "")).startswith(value)
        raise NotImplementedError(type(cond))
    return check(kwargs["KeyConditionExpression"])


def profile(sub, role="requesters", modules=("PURCHASE", "ACCESS", "LEAVE", "CHANGE", "GENERAL"), status="ACTIVE",
            email=None, version=1):
    item = {"PK": f"USER#{sub}", "SK": "PROFILE", "entity": "USER_PROFILE", "userId": sub, "username": sub,
            "email": email or f"{sub}@example.com", "name": sub.title(), "role": role, "status": status,
            "version": version, "createdAt": 1, "GSI2PK": "DIRECTORY#USERS", "GSI2SK": 1}
    if modules:
        item["modules"] = set(modules)
    return item
