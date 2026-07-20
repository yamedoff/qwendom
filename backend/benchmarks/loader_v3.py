"""Public-only prompt builder for benchmark v3."""

from __future__ import annotations

import json

from benchmarks.suite_v3 import TASK, required_output_example


def build_prompt() -> str:
    """Build the prompt without importing evidence records or private answers."""

    return (
        "Use only the public task and read-only tools. Return only valid JSON.\n\n"
        f"PUBLIC TASK:\n{TASK.model_dump_json(indent=2)}\n\n"
        "The harness has called each evidence surface exactly once and supplies the results below. "
        "Use lookup_security_record only when a returned record needs clarification. selected_ids "
        "contains every supported release blocker ID; ordered_ids contains release stage IDs. "
        "Every evidence_citations value must be an array containing only exact returned record IDs, "
        "never prose or tool names.\n\n"
        f"REQUIRED OUTPUT SHAPE:\n{json.dumps(required_output_example(), indent=2)}"
    )
