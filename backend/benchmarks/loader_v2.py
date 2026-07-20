"""Public-only prompt construction for benchmark suite v2."""

from __future__ import annotations

import json

from benchmarks.suite_v2 import BenchmarkTask, get_task


def build_task_prompt(task_id: str) -> str:
    """Build a model prompt without importing or revealing answer keys."""
    task: BenchmarkTask = get_task(task_id)
    schema = {
        "selected_ids": ["candidate IDs"], "ordered_ids": ["candidate IDs in order"],
        "evidence_citations": {label: ["record IDs"] for label in task.required_claim_labels},
        "constraint_ids": ["satisfied constraint IDs"],
        "numeric_answers": {label: 0 for label in task.required_numeric_labels},
        "governance_activity": ["optional unscored steps"],
    }
    public = task.model_dump()
    return (
        "Solve this benchmark task using only its public records. You may use the provided "
        "read-only lookup and calculator tools. Return only valid JSON.\n\n"
        f"PUBLIC TASK:\n{json.dumps(public, sort_keys=True, indent=2)}\n\n"
        f"REQUIRED OUTPUT:\n{json.dumps(schema, sort_keys=True, indent=2)}"
    )
