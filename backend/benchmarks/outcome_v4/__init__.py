"""Deterministic Layer B outcome benchmark foundation.

This package provides the smallest complete development-mode harness for the
accepted Layer B benchmark. It keeps public scenario inputs separate from
private evaluators, reuses the production work-graph executor for society-mode
runs, persists durable evidence for every attempt, and hard-refuses official
runs until the explicit promotion gates are satisfied.
"""

from .models import (
    BENCHMARK_MODELS,
    EVALUATOR_VERSION,
    REQUIRED_MODEL,
    SUITE_VERSION,
    AttemptRecord,
    BenchmarkMode,
    BudgetCaps,
    EvaluatorResult,
    RetryPolicy,
    RunMode,
)

__all__ = [
    "AttemptRecord",
    "BenchmarkMode",
    "BENCHMARK_MODELS",
    "BudgetCaps",
    "EVALUATOR_VERSION",
    "EvaluatorResult",
    "REQUIRED_MODEL",
    "RetryPolicy",
    "RunMode",
    "SUITE_VERSION",
]
