"""Safe read-only tools shared by both v2 benchmark modes."""

from __future__ import annotations

import ast
import math
import operator
from typing import Any

from benchmarks.suite_v2 import TASKS

TOOL_NAMES = ("lookup_record", "lookup_dataset", "calculate")


def lookup_record(task_id: str, record_id: str) -> dict[str, Any]:
    """Return a public record or a structured error an agent can correct."""

    task = TASKS.get(task_id)
    if task is None:
        return {
            "record_id": record_id,
            "found": False,
            "error": "unknown_task_id",
            "valid_task_ids": sorted(TASKS),
        }
    if record_id not in task.records:
        return {
            "record_id": record_id,
            "found": False,
            "error": "unknown_record_id",
            "valid_record_ids": sorted(task.records),
        }
    return {"record_id": record_id, "found": True, "text": task.records[record_id]}


def lookup_dataset(task_id: str, ids: list[str]) -> list[dict[str, Any]]:
    """Return requested public records in request order."""
    return [lookup_record(task_id, record_id) for record_id in ids]


_OPS: dict[type[ast.AST], Any] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
}


def calculate(expression: str) -> float:
    """Evaluate arithmetic only; names, calls, attributes, and powers are forbidden."""
    if len(expression) > 100:
        raise ValueError("expression is too long")
    tree = ast.parse(expression, mode="eval")

    def visit(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            value = float(node.value)
            if not math.isfinite(value) or abs(value) > 1_000_000_000:
                raise ValueError("numeric literal is outside the allowed range")
            return value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            value = float(_OPS[type(node.op)](visit(node.left), visit(node.right)))
            if not math.isfinite(value) or abs(value) > 1_000_000_000_000:
                raise ValueError("calculation result is outside the allowed range")
            return value
        raise ValueError("only basic arithmetic is allowed")

    return visit(tree)
