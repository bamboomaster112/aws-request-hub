"""Loads each Lambda's app.py under a unique module name (they all share the file name)."""
import importlib.util
import os
import pathlib

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TABLE_NAME", "test-table")
os.environ.setdefault("ATTACHMENTS_BUCKET", "test-bucket")

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"


def load(function_dir):
    path = SRC / function_dir / "app.py"
    spec = importlib.util.spec_from_file_location(f"{function_dir}_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
