"""Mode-neutral runner and refusal gates for the Layer B harness."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Literal

from .adapters import (
    DeterministicSingleAgentAdapter,
    DeterministicSocietyAdapter,
    ModeAdapter,
    ProviderAdapterRefusal,
    ProviderBackedSingleAgentAdapter,
    ProviderBackedSocietyAdapter,
    WorkspaceExportViolation,
)
from .artifacts import AttemptBundleWriter, hash_tree
from .budget import AtomicBudgetLedger
from .evaluators import evaluate_attempt
from .fixtures import load_public_scenario, load_seal_manifest, prepare_fresh_fixture_copy
from .models import (
    REQUIRED_MODEL,
    AttemptRecord,
    BenchmarkMode,
    BudgetCaps,
    PromotionGateReport,
    RetryPolicy,
    RunMode,
    stable_hash,
    utc_now_iso,
)


AdapterKind = Literal["deterministic", "provider"]
DEFAULT_DEVELOPMENT_TIMEOUT_SECONDS = 300.0
MIN_TIMEOUT_SECONDS = 1.0
MAX_TIMEOUT_SECONDS = 3600.0


def _public_fixture_manifest(scenario_id: str) -> list[dict[str, object]]:
    """Serialize the public fixture tree for byte-identical prompt handoff."""

    scenario = load_public_scenario(scenario_id)
    root = Path(scenario.fixture_source_dir)
    manifest: list[dict[str, object]] = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        raw = path.read_bytes()
        entry: dict[str, object] = {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        }
        try:
            entry["content"] = raw.decode("utf-8")
        except UnicodeDecodeError:
            entry["content_encoding"] = "binary"
        manifest.append(entry)
    return manifest


def build_prompt(scenario_id: str) -> str:
    """Build the byte-identical prompt for both benchmark modes."""

    scenario = load_public_scenario(scenario_id)
    output_contract = scenario.output_contract.model_dump(mode="json")
    fixture_manifest = _public_fixture_manifest(scenario_id)
    return (
        f"Scenario: {scenario.title}\n"
        f"Model: {REQUIRED_MODEL}\n"
        "Rules:\n"
        "- Use only the provided public fixture copy.\n"
        "- Do not assume private evaluator data.\n"
        "- Produce only the documented outputs inside the fixture workspace.\n"
        f"- Tool union: {', '.join(scenario.tool_union)}\n\n"
        f"{scenario.brief_markdown.strip()}\n\n"
        "Output contract:\n"
        f"{json.dumps(output_contract, indent=2, sort_keys=True)}\n\n"
        "Public fixture manifest:\n"
        f"{json.dumps(fixture_manifest, indent=2, sort_keys=True)}\n"
    )


def build_default_budgets(timeout_seconds: float = DEFAULT_DEVELOPMENT_TIMEOUT_SECONDS) -> BudgetCaps:
    """Return the frozen development caps shared by both modes."""

    return BudgetCaps(
        qwen_input_tokens=600_000,
        qwen_output_tokens=600_000,
        qwen_total_tokens=600_000,
        qwen_model_calls=24,
        image_credits=6,
        vision_calls=12,
        browser_renders=20,
        subprocess_seconds=20.0,
        wall_time_seconds=timeout_seconds,
    )


def build_default_retry_policy() -> RetryPolicy:
    """Return the frozen retry policy shared by both modes."""

    return RetryPolicy()


def resolve_adapter(benchmark_mode: BenchmarkMode, *, deterministic: bool) -> ModeAdapter:
    """Select the injected adapter implementation for one mode."""

    if deterministic:
        return DeterministicSingleAgentAdapter() if benchmark_mode == "single_agent" else DeterministicSocietyAdapter()
    return ProviderBackedSingleAgentAdapter() if benchmark_mode == "single_agent" else ProviderBackedSocietyAdapter()


def evaluate_official_gates(scenario_id: str, *, deterministic: bool) -> PromotionGateReport:
    """Refuse official mode until all accepted promotion gates exist."""

    seal_manifest = load_seal_manifest(scenario_id)
    reasons: list[str] = []
    if deterministic:
        reasons.append("official mode refuses deterministic adapters; provider-backed adapters are required")
    if seal_manifest.status != "sealed":
        reasons.append("fixture seals and hashes are not frozen")
    if any(not variant.sealed or not variant.fixture_hash or not variant.evaluator_hash for variant in seal_manifest.variants):
        reasons.append("future official variant slots are placeholders only")
    reasons.extend([
        "evaluator version freeze is not promoted beyond development",
        "budget and model/tool freeze markers for official collection are absent",
        "required development smokes for both modes and scenarios are not recorded",
        "promotion-review gates are not satisfied in this deterministic pass",
    ])
    return PromotionGateReport(ready=False, reasons=reasons)


async def run_attempt(
    *,
    scenario_id: str,
    benchmark_mode: BenchmarkMode,
    run_mode: RunMode,
    output_dir: Path,
    adapter: AdapterKind = "deterministic",
    deterministic: bool | None = None,
    timeout_seconds: float = DEFAULT_DEVELOPMENT_TIMEOUT_SECONDS,
) -> AttemptRecord:
    """Run one deterministic development attempt or refuse official mode."""

    if deterministic is not None:
        adapter = "deterministic" if deterministic else "provider"
    selected_deterministic = adapter == "deterministic"
    scenario = load_public_scenario(scenario_id)
    attempt_root = output_dir / f"{scenario_id}-{benchmark_mode}-{run_mode}"
    budgets = build_default_budgets(timeout_seconds)
    retry_policy = build_default_retry_policy()
    prompt_text = build_prompt(scenario_id)
    source_hash = hash_tree(Path(scenario.fixture_source_dir))
    if not selected_deterministic and output_dir.exists() and any(output_dir.iterdir()):
        writer = AttemptBundleWriter(attempt_root)
        record = AttemptRecord(
            attempt_id=attempt_root.name,
            run_mode=run_mode,
            benchmark_mode=benchmark_mode,
            scenario_id=scenario.scenario_id,
            tool_union=list(scenario.tool_union),
            public_scenario_hash=scenario.public_hash,
            fixture_source_hash=source_hash,
            prompt_text=prompt_text,
            prompt_hash=stable_hash(prompt_text),
            timeout_seconds=timeout_seconds,
            retry_policy=retry_policy,
            budgets=budgets,
            provider_adapter=resolve_adapter(benchmark_mode, deterministic=selected_deterministic).name,
            status="refused",
            refusal_reasons=[
                "provider development attempts require an empty output directory to avoid mixed smoke evidence",
            ],
            finished_at=utc_now_iso(),
        )
        record.hashes = {
            "budgets_hash": stable_hash(budgets),
            "retry_policy_hash": stable_hash(retry_policy),
            "budget_config_hash": stable_hash(
                {
                    "budgets": budgets.model_dump(mode="json"),
                    "retry_policy": retry_policy.model_dump(mode="json"),
                    "timeout_seconds": timeout_seconds,
                }
            ),
        }
        writer.write_prompt(prompt_text)
        writer.write_json("refusal.json", {"ready": False, "reasons": list(record.refusal_reasons)})
        writer.write_record(record)
        return record
    writer = AttemptBundleWriter(attempt_root)
    record = AttemptRecord(
        attempt_id=attempt_root.name,
        run_mode=run_mode,
        benchmark_mode=benchmark_mode,
        scenario_id=scenario.scenario_id,
        tool_union=list(scenario.tool_union),
        public_scenario_hash=scenario.public_hash,
        fixture_source_hash=source_hash,
        prompt_text=prompt_text,
        prompt_hash=stable_hash(prompt_text),
        timeout_seconds=timeout_seconds,
        retry_policy=retry_policy,
        budgets=budgets,
        provider_adapter=resolve_adapter(benchmark_mode, deterministic=selected_deterministic).name,
    )
    record.hashes = {
        "budgets_hash": stable_hash(budgets),
        "retry_policy_hash": stable_hash(retry_policy),
        "budget_config_hash": stable_hash(
            {
                "budgets": budgets.model_dump(mode="json"),
                "retry_policy": retry_policy.model_dump(mode="json"),
                "timeout_seconds": timeout_seconds,
            }
        ),
    }
    writer.write_prompt(prompt_text)
    if run_mode == "official":
        gates = evaluate_official_gates(scenario_id, deterministic=selected_deterministic)
        record.status = "refused"
        record.refusal_reasons = list(gates.reasons)
        record.finished_at = utc_now_iso()
        writer.write_json("refusal.json", gates)
        writer.write_record(record)
        return record

    workspace_dir, _source_hash, copy_hash = prepare_fresh_fixture_copy(scenario, attempt_root)
    record.fixture_copy_dir = str(workspace_dir)
    record.fixture_copy_hash = copy_hash
    writer.write_json("scenario.json", scenario)

    ledger = AtomicBudgetLedger(budgets, attempt_root / "ledger.jsonl")
    adapter_impl = resolve_adapter(benchmark_mode, deterministic=selected_deterministic)
    started_at = time.perf_counter()

    def _load_optional_json(path: Path) -> dict[str, object] | None:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _collect_cleanup_artifacts() -> tuple[list[str], dict[str, object]]:
        cleanup_paths: list[str] = []
        diagnostics: dict[str, object] = {}
        cleanup_manifest = workspace_dir / "cleanup" / "cleanup.json"
        cleanup_failure = workspace_dir / "cleanup" / "cleanup_failure.json"
        if cleanup_manifest.exists():
            cleanup_paths.append("cleanup/cleanup.json")
        if cleanup_failure.exists():
            cleanup_paths.append("cleanup/cleanup_failure.json")
            payload = _load_optional_json(cleanup_failure)
            if payload is not None:
                diagnostics["cleanup_failure"] = payload
        return cleanup_paths, diagnostics

    try:
        result = await asyncio.wait_for(
            adapter_impl.run(
                scenario=scenario,
                workspace_dir=workspace_dir,
                prompt_text=prompt_text,
                ledger=ledger,
                retry_policy=retry_policy,
            ),
            timeout=timeout_seconds,
        )
        evaluation = evaluate_attempt(scenario.scenario_id, workspace_dir)
        finalization = await ledger.finalize_wall_time(time.perf_counter() - started_at)
        record.finished_at = utc_now_iso()
        record.status = "success" if evaluation.passed else "failed"
        record.raw_outputs = result.raw_outputs
        cleanup_evidence, diagnostics = _collect_cleanup_artifacts()
        record.cleanup_evidence = cleanup_evidence or list(result.cleanup_evidence)
        record.raw_outputs.update(diagnostics)
        record.raw_outputs["budget_finalization"] = finalization
        record.usage = ledger.snapshot()
        record.graph_result = result.graph_result
        record.evaluator_result = evaluation
        record.hashes.update(
            {
                "workspace_tree": hash_tree(workspace_dir),
                "tool_trace_hash": stable_hash([trace.model_dump(mode="json") for trace in result.tool_traces]),
            }
        )
        fairness = result.raw_outputs.get("fairness")
        if isinstance(fairness, dict):
            for hash_name in (
                "canonical_union_hash",
                "single_agent_union_hash",
                "society_union_hash",
            ):
                hash_value = fairness.get(hash_name)
                if isinstance(hash_value, str) and hash_value:
                    record.hashes[hash_name] = hash_value
        writer.write_json("tool_traces.json", [trace.model_dump(mode="json") for trace in result.tool_traces])
        writer.write_json("evaluator.json", evaluation)
        writer.write_json("raw_outputs.json", result.raw_outputs)
    except ProviderAdapterRefusal as exc:
        finalization = await ledger.finalize_wall_time(time.perf_counter() - started_at)
        record.finished_at = utc_now_iso()
        record.status = "refused"
        record.refusal_reasons = list(exc.reasons)
        record.raw_outputs = {
            "typed_blockers": list(exc.typed_blockers),
            "provider_adapter": adapter_impl.name,
            "budget_finalization": finalization,
        }
        record.cleanup_evidence, diagnostics = _collect_cleanup_artifacts()
        record.raw_outputs.update(diagnostics)
        record.usage = ledger.snapshot()
        writer.write_json("refusal.json", record.raw_outputs)
    except WorkspaceExportViolation as exc:
        finalization = await ledger.finalize_wall_time(time.perf_counter() - started_at)
        record.finished_at = utc_now_iso()
        record.status = "failed"
        record.cleanup_evidence, diagnostics = _collect_cleanup_artifacts()
        record.raw_outputs.update(diagnostics)
        record.raw_outputs["typed_failure"] = exc.to_payload()
        record.raw_outputs["budget_finalization"] = finalization
        record.usage = ledger.snapshot()
        record.failure = {"error_type": type(exc).__name__, "message": str(exc), "code": exc.code}
        writer.write_json("failure.json", record.failure)
    except Exception as exc:
        finalization = await ledger.finalize_wall_time(time.perf_counter() - started_at)
        record.finished_at = utc_now_iso()
        record.status = "failed"
        record.cleanup_evidence, diagnostics = _collect_cleanup_artifacts()
        record.raw_outputs.update(diagnostics)
        record.raw_outputs["budget_finalization"] = finalization
        record.usage = ledger.snapshot()
        record.failure = {"error_type": type(exc).__name__, "message": str(exc)}
        writer.write_json("failure.json", record.failure)
    writer.write_record(record)
    return record
