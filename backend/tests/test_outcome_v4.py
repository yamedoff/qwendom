"""Focused deterministic tests for the Layer B outcome-v4 foundation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.outcome_v4.artifacts import AttemptBundleWriter, hash_tree
from benchmarks.outcome_v4.budget import AtomicBudgetLedger, BudgetExceededError
from benchmarks.outcome_v4.evaluators import evaluate_attempt
from benchmarks.outcome_v4.fixtures import FIXTURES_ROOT, load_public_scenario
from benchmarks.outcome_v4.models import AttemptRecord, BudgetCaps, RetryPolicy
from benchmarks.outcome_v4.runner import build_default_budgets, build_default_retry_policy, build_prompt, run_attempt


class _FailingAdapter:
    deterministic = True
    name = "failing-adapter"

    async def run(self, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("deterministic adapter failure")


class _NeverEndingAdapter:
    deterministic = False
    name = "never-ending-adapter"

    async def run(self, **kwargs):  # type: ignore[no-untyped-def]
        await asyncio.sleep(3600)


class OutcomeV4AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_ledger_enforces_exact_600k_combined_qwen_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = AtomicBudgetLedger(BudgetCaps(), Path(temp_dir) / "ledger.jsonl")
            await ledger.debit("qwen_input_tokens", 400_000.0)
            await ledger.debit("qwen_output_tokens", 200_000.0)
            snapshot = ledger.snapshot()
            self.assertEqual(snapshot.qwen_input_tokens, 400_000)
            self.assertEqual(snapshot.qwen_output_tokens, 200_000)
            self.assertEqual(snapshot.qwen_total_tokens, 600_000)
            with self.assertRaises(BudgetExceededError):
                await ledger.debit("qwen_output_tokens", 1.0, metadata={"case": "over-total"})
            self.assertEqual(ledger.snapshot().qwen_total_tokens, 600_000)
            journal = (Path(temp_dir) / "ledger.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event": "debit_rejected"', journal)
            self.assertIn('"category": "qwen_total_tokens"', journal)
            self.assertIn('"trigger_category": "qwen_output_tokens"', journal)

    async def test_budget_ledger_enforces_atomic_race_caps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = AtomicBudgetLedger(
                BudgetCaps(qwen_model_calls=4, peak_concurrency=2),
                Path(temp_dir) / "ledger.jsonl",
            )
            gate = asyncio.Event()

            async def worker() -> None:
                async with ledger.tool_call("filesystem_edit", category="qwen_model_calls", amount=1.0):
                    await gate.wait()

            first = asyncio.create_task(worker())
            second = asyncio.create_task(worker())
            await asyncio.sleep(0.02)
            with self.assertRaises(BudgetExceededError):
                async with ledger.tool_call("filesystem_edit", category="qwen_model_calls", amount=1.0):
                    pass
            gate.set()
            await asyncio.gather(first, second)
            self.assertEqual(ledger.snapshot().peak_concurrency, 2)

    async def test_budget_ledger_enforces_atomic_total_token_races(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = AtomicBudgetLedger(
                BudgetCaps(qwen_input_tokens=600_000, qwen_output_tokens=600_000, qwen_total_tokens=600_000),
                Path(temp_dir) / "ledger.jsonl",
            )
            await ledger.debit("qwen_input_tokens", 100_000.0)
            results = await asyncio.gather(
                ledger.debit("qwen_input_tokens", 300_000.0, metadata={"worker": "a"}),
                ledger.debit("qwen_output_tokens", 300_000.0, metadata={"worker": "b"}),
                return_exceptions=True,
            )
            failures = [result for result in results if isinstance(result, BudgetExceededError)]
            self.assertEqual(len(failures), 1)
            self.assertLessEqual(ledger.snapshot().qwen_total_tokens, 600_000)
            self.assertEqual(ledger.snapshot().qwen_total_tokens, 400_000)

    async def test_development_runs_use_fair_byte_identical_public_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            single = await run_attempt(
                scenario_id="incident_repair",
                benchmark_mode="single_agent",
                run_mode="development",
                output_dir=Path(temp_dir) / "single",
            )
            society = await run_attempt(
                scenario_id="incident_repair",
                benchmark_mode="society",
                run_mode="development",
                output_dir=Path(temp_dir) / "society",
            )
            self.assertEqual(single.prompt_hash, society.prompt_hash)
            self.assertEqual(single.prompt_text, society.prompt_text)
            self.assertEqual(single.public_scenario_hash, society.public_scenario_hash)
            self.assertEqual(single.fixture_source_hash, society.fixture_source_hash)
            self.assertEqual(single.tool_union, society.tool_union)
            self.assertEqual(single.budgets.model_dump(mode="json"), society.budgets.model_dump(mode="json"))
            self.assertEqual(single.retry_policy.model_dump(mode="json"), society.retry_policy.model_dump(mode="json"))
            self.assertEqual(single.hashes["budgets_hash"], society.hashes["budgets_hash"])
            self.assertEqual(single.hashes["budget_config_hash"], society.hashes["budget_config_hash"])
            self.assertEqual(single.timeout_seconds, society.timeout_seconds)
            self.assertEqual(single.model, "qwen3.7-plus")
            self.assertEqual(society.model, "qwen3.7-plus")

    async def test_prompt_never_contains_private_evaluator_expectations(self) -> None:
        prompt_a = build_prompt("incident_repair")
        prompt_b = build_prompt("screenshot_to_product")
        for prompt in (prompt_a, prompt_b):
            self.assertNotIn("future official variant slots are placeholders only", prompt)
            self.assertNotIn("mandatory_gates_passed", prompt)
            self.assertNotIn("reachable_vulnerability_closed", prompt)
            self.assertNotIn("hero_width", prompt)

    async def test_incident_public_fixture_exposes_dependency_remediation_metadata(self) -> None:
        prompt = build_prompt("incident_repair")

        self.assertIn('"path": "public/dependency_metadata.json"', prompt)
        metadata = json.loads(
            (FIXTURES_ROOT / "incident_repair" / "dev" / "public" / "dependency_metadata.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(metadata["dependencies"][0]["minimum_remediated_version"], "6.0.2")

    async def test_fresh_fixture_copies_are_isolated_and_source_tree_stays_unchanged(self) -> None:
        scenario = load_public_scenario("incident_repair")
        source_dir = Path(scenario.fixture_source_dir)
        source_hash_before = hash_tree(source_dir)
        original_auth = (source_dir / "repo" / "services" / "api" / "auth.py").read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            first = await run_attempt(
                scenario_id="incident_repair",
                benchmark_mode="single_agent",
                run_mode="development",
                output_dir=Path(temp_dir) / "first",
            )
            second = await run_attempt(
                scenario_id="incident_repair",
                benchmark_mode="single_agent",
                run_mode="development",
                output_dir=Path(temp_dir) / "second",
            )

            self.assertNotEqual(first.fixture_copy_dir, second.fixture_copy_dir)
            self.assertTrue((Path(first.fixture_copy_dir) / "repo" / "services" / "api" / "auth.py").exists())
            self.assertTrue((Path(second.fixture_copy_dir) / "repo" / "services" / "api" / "auth.py").exists())
            self.assertEqual(source_hash_before, hash_tree(source_dir))
            self.assertEqual(original_auth, (source_dir / "repo" / "services" / "api" / "auth.py").read_text(encoding="utf-8"))
            self.assertIn('scope == "admin"', original_auth)

    async def test_failed_attempt_is_retained_and_bundle_reconstructs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch("benchmarks.outcome_v4.runner.resolve_adapter", return_value=_FailingAdapter()):
                record = await run_attempt(
                    scenario_id="incident_repair",
                    benchmark_mode="single_agent",
                    run_mode="development",
                    output_dir=Path(temp_dir),
                )
            self.assertEqual(record.status, "failed")
            self.assertIsNotNone(record.failure)
            bundle = AttemptBundleWriter(Path(temp_dir) / record.attempt_id).reconstruct()
            self.assertIn("attempt.json", bundle)
            self.assertIn("failure.json", bundle)
            self.assertEqual(bundle["failure.json"]["error_type"], "RuntimeError")

    async def test_bundle_reconstruct_preserves_binary_artifacts_and_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bundle"
            writer = AttemptBundleWriter(root)
            writer.write_text("notes.txt", "hello\n")
            png_bytes = b"\x89PNG\r\n\x1a\nbinary-payload"
            artifact = root / "artifact.png"
            artifact.write_bytes(png_bytes)
            writer.write_record(
                AttemptRecord(
                    attempt_id="bundle",
                    run_mode="development",
                    benchmark_mode="single_agent",
                    scenario_id="incident_repair",
                    tool_union=[],
                    public_scenario_hash="public",
                    fixture_source_hash="fixture",
                    prompt_text="prompt",
                    prompt_hash="prompt-hash",
                    timeout_seconds=1.0,
                    retry_policy=RetryPolicy(),
                    budgets=BudgetCaps(),
                )
            )

            bundle = writer.reconstruct()
            self.assertEqual(bundle["notes.txt"], "hello\n")
            self.assertEqual(bundle["artifact.png"], png_bytes)
            self.assertEqual(hashlib.sha256(bundle["artifact.png"]).hexdigest(), hashlib.sha256(png_bytes).hexdigest())

            artifact.write_bytes(png_bytes + b"-tampered")
            with self.assertRaises(ValueError):
                writer.reconstruct()

    async def test_timeout_failure_persists_when_final_wall_time_exceeds_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch("benchmarks.outcome_v4.runner.resolve_adapter", return_value=_NeverEndingAdapter()),
                mock.patch("benchmarks.outcome_v4.runner.time.perf_counter", side_effect=[0.0, 123.197]),
            ):
                record = await run_attempt(
                    scenario_id="incident_repair",
                    benchmark_mode="single_agent",
                    run_mode="development",
                    output_dir=Path(temp_dir),
                    timeout_seconds=1.0,
                )
            self.assertEqual(record.status, "failed")
            self.assertEqual(record.failure, {"error_type": "TimeoutError", "message": ""})
            self.assertEqual(record.usage.wall_time_seconds, 123.197)
            self.assertTrue(record.raw_outputs["budget_finalization"]["over_cap"])
            bundle = AttemptBundleWriter(Path(temp_dir) / record.attempt_id).reconstruct()
            self.assertIn("\"event\": \"budget_overage\"", bundle["ledger.jsonl"])

    async def test_cleanup_evidence_is_recorded_separately_when_failure_path_writes_it(self) -> None:
        class _CleanupRecordingAdapter:
            deterministic = False
            name = "cleanup-recording-adapter"

            async def run(self, **kwargs):  # type: ignore[no-untyped-def]
                workspace_dir = kwargs["workspace_dir"]
                try:
                    await asyncio.sleep(3600)
                finally:
                    cleanup_dir = workspace_dir / "cleanup"
                    cleanup_dir.mkdir(parents=True, exist_ok=True)
                    (cleanup_dir / "cleanup.json").write_text(
                        json.dumps(
                            {
                                "workspace_removed": False,
                                "artifact_manifest_complete": False,
                                "cleanup_steps": ["cleanup_failure_recorded"],
                                "exported_paths": [],
                            },
                            indent=2,
                            sort_keys=True,
                        ),
                        encoding="utf-8",
                    )
                    (cleanup_dir / "cleanup_failure.json").write_text(
                        json.dumps({"error_type": "RuntimeError", "message": "cleanup boom"}, indent=2, sort_keys=True),
                        encoding="utf-8",
                    )

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch("benchmarks.outcome_v4.runner.resolve_adapter", return_value=_CleanupRecordingAdapter()):
                record = await run_attempt(
                    scenario_id="incident_repair",
                    benchmark_mode="single_agent",
                    run_mode="development",
                    output_dir=Path(temp_dir),
                    timeout_seconds=1.0,
                )
            self.assertEqual(record.failure, {"error_type": "TimeoutError", "message": ""})
            self.assertEqual(record.cleanup_evidence, ["cleanup/cleanup.json", "cleanup/cleanup_failure.json"])
            self.assertEqual(record.raw_outputs["cleanup_failure"]["error_type"], "RuntimeError")

    async def test_official_mode_refuses_with_nonempty_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            record = await run_attempt(
                scenario_id="screenshot_to_product",
                benchmark_mode="society",
                run_mode="official",
                output_dir=Path(temp_dir),
            )
            self.assertEqual(record.status, "refused")
            self.assertTrue(record.refusal_reasons)
            self.assertIn("fixture seals and hashes are not frozen", record.refusal_reasons)
            bundle = AttemptBundleWriter(Path(temp_dir) / record.attempt_id).reconstruct()
            self.assertIn("refusal.json", bundle)

    async def test_end_to_end_fake_runs_succeed_for_both_modes_and_scenarios(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            records = []
            for scenario_id in ("incident_repair", "screenshot_to_product"):
                for benchmark_mode in ("single_agent", "society"):
                    records.append(
                        await run_attempt(
                            scenario_id=scenario_id,
                            benchmark_mode=benchmark_mode,
                            run_mode="development",
                            output_dir=Path(temp_dir) / f"{scenario_id}-{benchmark_mode}",
                        )
                    )
            self.assertEqual([record.status for record in records], ["success", "success", "success", "success"])
            for record in records:
                self.assertIsNotNone(record.evaluator_result)
                self.assertTrue(record.evaluator_result.passed)
                self.assertTrue(record.cleanup_evidence)
                if record.benchmark_mode == "society":
                    self.assertIsNotNone(record.graph_result)


class OutcomeV4SyncTests(unittest.TestCase):
    def test_defaults_are_mode_neutral(self) -> None:
        budgets = build_default_budgets()
        retry_policy = build_default_retry_policy()
        self.assertEqual(budgets.peak_concurrency, 4)
        self.assertEqual(budgets.qwen_input_tokens, 600_000)
        self.assertEqual(budgets.qwen_output_tokens, 600_000)
        self.assertEqual(budgets.qwen_total_tokens, 600_000)
        self.assertEqual(budgets.image_credits, 6)
        self.assertEqual(budgets.vision_calls, 12)
        self.assertEqual(budgets.browser_renders, 20)
        self.assertEqual(budgets.subprocess_seconds, 20.0)
        self.assertEqual(budgets.wall_time_seconds, 300.0)
        self.assertEqual(retry_policy.model_dump(mode="json"), RetryPolicy().model_dump(mode="json"))

    def test_cli_help_surfaces_safe_commands(self) -> None:
        from benchmarks.outcome_v4.cli import build_parser

        parser = build_parser()
        help_text = parser.format_help()
        run_help = next(
            action.choices["run"].format_help()
            for action in parser._actions
            if getattr(action, "choices", None) and "run" in action.choices
        )
        self.assertIn("python -m benchmarks.outcome_v4.cli validate", help_text)
        self.assertIn("--run-mode development", help_text)
        self.assertIn("--timeout-seconds", run_help)

    def test_cli_timeout_arg_bounds(self) -> None:
        from benchmarks.outcome_v4.cli import build_parser

        parser = build_parser()
        args = parser.parse_args([
            "run",
            "--scenario",
            "incident_repair",
            "--benchmark-mode",
            "single_agent",
            "--timeout-seconds",
            "300",
        ])
        self.assertEqual(args.timeout_seconds, 300.0)
        with self.assertRaises(SystemExit):
            parser.parse_args([
                "run",
                "--scenario",
                "incident_repair",
                "--benchmark-mode",
                "single_agent",
                "--timeout-seconds",
                "0",
            ])

    def test_evaluators_are_stable_and_reject_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            incident_workspace = root / "incident"
            shutil.copytree(FIXTURES_ROOT / "incident_repair" / "dev", incident_workspace)
            (incident_workspace / "repo" / "services" / "api" / "auth.py").write_text(
                "def allow_support_scope(scope, token_enabled):\n    return token_enabled and scope in {\"admin\", \"support\"}\n",
                encoding="utf-8",
            )
            (incident_workspace / "repo" / "services" / "worker" / "idempotency.py").write_text(
                "processed_ids = set()\n\ndef process_job(job_id):\n    if job_id in processed_ids:\n        return \"duplicate_ignored\"\n    processed_ids.add(job_id)\n    return \"processed\"\n",
                encoding="utf-8",
            )
            (incident_workspace / "repo" / "services" / "config" / "database.py").write_text(
                "import os\n\ndef database_url():\n    return os.getenv(\"DB_URL\", \"postgresql://localhost/devdb\")\n",
                encoding="utf-8",
            )
            (incident_workspace / "repo" / "requirements.txt").write_text("pyyaml==6.0.2\n", encoding="utf-8")
            for relative, payload in {
                "reports/test_report.json": {"passed": True, "tests_run": 6, "truthful_failures_retained": True},
                "reports/security_report.json": {"passed": True, "reachable_vulnerability_closed": True, "truthful_failures_retained": True},
                "reports/evidence_report.json": {"evidence_ids": ["AUTH-LOG-401", "DB-CONFIG-URL", "JOB-TRACE-008"], "changed_files": ["auth.py"], "command_evidence": ["pytest -q"]},
                "incident/rollback_plan.json": {"max_rollback_minutes": 15, "validated": True},
                "cleanup/cleanup.json": {"workspace_removed": False, "artifact_manifest_complete": True},
            }.items():
                target = incident_workspace / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

            incident_results = [evaluate_attempt("incident_repair", incident_workspace) for _ in range(3)]
            self.assertTrue(all(result.model_dump(mode="json") == incident_results[0].model_dump(mode="json") for result in incident_results))
            self.assertTrue(incident_results[0].passed)

            semantic_incident = root / "incident-semantic-aliases"
            shutil.copytree(incident_workspace, semantic_incident)
            (semantic_incident / "repo" / "services" / "worker" / "idempotency.py").write_text(
                "processed_ids = set()\n\ndef process_job(job_id):\n    if job_id in processed_ids:\n        return 'duplicate'\n    processed_ids.add(job_id)\n    return 'processed'\n",
                encoding="utf-8",
            )
            for relative, payload in {
                "reports/test_report.json": {
                    "overall_status": "pass",
                    "executed_test_count": 2,
                    "tests": [{"result": "pass"}, {"result": "pass"}],
                    "failures": [],
                },
                "reports/security_report.json": {
                    "dependency_issues": [{"reachable": True, "status": "upgraded", "closed": True}],
                    "failures": [],
                },
                "reports/evidence_report.json": {
                    "evidence_ids_cited": ["AUTH-LOG-401", "DB-CONFIG-URL", "JOB-TRACE-008"],
                    "issues_resolved": [{"file": "repo/services/api/auth.py", "status": "resolved"}],
                    "command_evidence": [{"status": "executed"}],
                },
                "incident/rollback_plan.json": {
                    "rollback_plan": {
                        "overall_status": "validated",
                        "max_duration_minutes": 15,
                        "validated_max_duration_minutes": 10,
                        "steps": [{"action": "revert"}],
                        "within_limit": True,
                    }
                },
            }.items():
                (semantic_incident / relative).write_text(json.dumps(payload, indent=2), encoding="utf-8")
            self.assertTrue(evaluate_attempt("incident_repair", semantic_incident).passed)

            semantic_mutation = root / "incident-semantic-mutation"
            shutil.copytree(semantic_incident, semantic_mutation)
            (semantic_mutation / "reports" / "test_report.json").write_text(
                json.dumps({"test_status": "failed", "executed_test_count": 2, "failures_retained": [{"status_after_fix": "unresolved"}]}),
                encoding="utf-8",
            )
            self.assertFalse(evaluate_attempt("incident_repair", semantic_mutation).passed)

            rollback_mutation = root / "incident-rollback-over-limit"
            shutil.copytree(semantic_incident, rollback_mutation)
            (rollback_mutation / "incident" / "rollback_plan.json").write_text(
                json.dumps(
                    {
                        "rollback_limit_minutes": 15,
                        "validated_max_duration_minutes": 16,
                        "steps": [{"action": "revert"}],
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse(evaluate_attempt("incident_repair", rollback_mutation).passed)

            mutated_incident = root / "incident-mutated"
            shutil.copytree(incident_workspace, mutated_incident)
            (mutated_incident / "reports" / "security_report.json").write_text(
                json.dumps({"passed": False, "reachable_vulnerability_closed": False, "truthful_failures_retained": True}, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            self.assertFalse(evaluate_attempt("incident_repair", mutated_incident).passed)

            product_workspace = root / "product"
            shutil.copytree(FIXTURES_ROOT / "screenshot_to_product" / "dev", product_workspace)
            (product_workspace / "app" / "src" / "app.js").write_text(
                "export function renderHero(){return {title:'Ship calmer incident tooling',subtitle:'A compact control plane for repair, rollout, and rollback.',cta:'Review the launch checklist'};}\n",
                encoding="utf-8",
            )
            (product_workspace / "app" / "src" / "styles.css").write_text("body { margin: 0; }\n", encoding="utf-8")
            (product_workspace / "app" / "dist" / "assets").mkdir(parents=True, exist_ok=True)
            (product_workspace / "screenshots").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(product_workspace / "public" / "references" / "desktop.png", product_workspace / "screenshots" / "desktop.png")
            shutil.copyfile(product_workspace / "public" / "references" / "mobile.png", product_workspace / "screenshots" / "mobile.png")
            shutil.copyfile(product_workspace / "public" / "assets" / "hero-card.png", product_workspace / "app" / "dist" / "assets" / "hero-card.png")
            (product_workspace / "app" / "dist" / "index.html").parent.mkdir(parents=True, exist_ok=True)
            (product_workspace / "app" / "dist" / "index.html").write_text("<html></html>\n", encoding="utf-8")
            asset_manifest = json.loads((product_workspace / "public" / "asset_manifest.json").read_text(encoding="utf-8"))
            for relative, payload in {
                "reports/build.json": {"passed": True},
                "reports/interaction.json": {"core_flow_passed": True, "api_contract_passed": True},
                "reports/layout.json": {"desktop": {"hero_width": 960}, "mobile": {"hero_width": 320}},
                "reports/a11y.json": {"critical_violations": 0},
                "reports/copy_manifest.json": {"source_ids": ["COPY-HERO-001", "COPY-FEATURE-002", "COPY-CTA-003"]},
                "reports/provenance.json": {
                    "hero_asset": {"dimensions": [256, 144], "format": "png", "sha256": asset_manifest["hero_sha256"]},
                    "asset_manifest_complete": True,
                    "screenshots_recorded": True,
                },
                "cleanup/cleanup.json": {"workspace_removed": False, "artifact_manifest_complete": True},
            }.items():
                target = product_workspace / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

            product_results = [evaluate_attempt("screenshot_to_product", product_workspace) for _ in range(3)]
            self.assertTrue(all(result.model_dump(mode="json") == product_results[0].model_dump(mode="json") for result in product_results))
            self.assertTrue(product_results[0].passed)

            mutated_product = root / "product-mutated"
            shutil.copytree(product_workspace, mutated_product)
            (mutated_product / "reports" / "a11y.json").write_text(
                json.dumps({"critical_violations": 2}, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            self.assertFalse(evaluate_attempt("screenshot_to_product", mutated_product).passed)


if __name__ == "__main__":
    unittest.main()
