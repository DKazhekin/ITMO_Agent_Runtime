"""Check fixed expectations and preserved fixture data."""
from __future__ import annotations

import csv
from io import StringIO
import json
from pathlib import Path

import yaml

from check_support import exact_text, guarded_file, path_set, read_text, unchanged
from code_check import check_code


def check_artifact(task: Path, work: Path) -> None:
    specification = json.loads((task / "expect.json").read_text(encoding="utf-8"))
    path = specification["path"]
    kind = specification["kind"]
    expected = specification.get("expected")
    unchanged(task, work, {path} if kind in {"edit", "workbook"} else set())
    if kind == "text":
        exact_text(work, path, expected)
    elif kind == "edit":
        if guarded_file(work, path).read_bytes() != expected.encode("utf-8"):
            raise ValueError(f"Unexpected bytes in {path}")
    elif kind == "set":
        path_set(work, path, set(expected))
    elif kind == "json":
        actual = json.loads(read_text(work, path))
        if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
            raise ValueError("JSON differs from the expected values or types")
    elif kind == "yaml":
        actual = yaml.safe_load(read_text(work, path))
        if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
            raise ValueError("YAML differs from the expected values or types")
    elif kind == "csv":
        if list(csv.reader(StringIO(read_text(work, path)))) != expected:
            raise ValueError("CSV differs from the expected rows")
    elif kind == "code":
        check_code(work, path, expected, preserve_arguments=specification.get("preserve_arguments", False))
    elif kind == "workbook":
        from baf_workbook import check_workbook

        check_workbook(task / "fixture" / path, guarded_file(work, path), specification)
    else:
        raise ValueError(f"Unknown checker kind: {kind}")
