"""Public development fixtures and official seal placeholders for Layer B."""

from __future__ import annotations

import json
from pathlib import Path

from .artifacts import copy_tree, hash_tree
from .models import OutputContract, PublicScenarioSurface, SealManifest

PACKAGE_ROOT = Path(__file__).resolve().parent
FIXTURES_ROOT = PACKAGE_ROOT / "fixtures_public"
SEALS_ROOT = PACKAGE_ROOT / "seals"

_TOOL_UNION = [
    "browser_render",
    "copy_asset",
    "filesystem_edit",
    "read_fixture",
    "subprocess_check",
    "vision_inspect",
    "write_report",
]


def _load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _scenario_surface(scenario_id: str) -> PublicScenarioSurface:
    if scenario_id == "incident_repair":
        source_dir = FIXTURES_ROOT / "incident_repair" / "dev"
        return PublicScenarioSurface(
            scenario_id="incident_repair",
            title="Multi-service incident repair",
            summary="Repair a small fictional repository with auth, idempotency, database, and dependency issues.",
            brief_markdown=_load_text(source_dir / "brief.md"),
            fixture_source_dir=str(source_dir),
            tool_union=list(_TOOL_UNION),
            output_contract=OutputContract(
                required_paths=[
                    "repo/services/api/auth.py",
                    "repo/services/worker/idempotency.py",
                    "repo/services/config/database.py",
                    "repo/requirements.txt",
                    "reports/test_report.json",
                    "reports/security_report.json",
                    "reports/evidence_report.json",
                    "incident/rollback_plan.json",
                    "cleanup/cleanup.json",
                ],
                required_reports=[
                    "reports/test_report.json",
                    "reports/security_report.json",
                    "reports/evidence_report.json",
                ],
                required_cleanup_markers=["cleanup/cleanup.json"],
            ),
        )
    if scenario_id == "screenshot_to_product":
        source_dir = FIXTURES_ROOT / "screenshot_to_product" / "dev"
        return PublicScenarioSurface(
            scenario_id="screenshot_to_product",
            title="Screenshot to working product",
            summary="Complete a small unfinished frontend using public brief, component contract, and local PNG references.",
            brief_markdown=_load_text(source_dir / "brief.md"),
            fixture_source_dir=str(source_dir),
            tool_union=list(_TOOL_UNION),
            output_contract=OutputContract(
                required_paths=[
                    "app/src/app.js",
                    "app/src/styles.css",
                    "app/dist/index.html",
                    "app/dist/assets/hero-card.png",
                    "screenshots/desktop.png",
                    "screenshots/mobile.png",
                    "reports/build.json",
                    "reports/interaction.json",
                    "reports/layout.json",
                    "reports/a11y.json",
                    "reports/copy_manifest.json",
                    "reports/provenance.json",
                    "cleanup/cleanup.json",
                ],
                required_reports=[
                    "reports/build.json",
                    "reports/interaction.json",
                    "reports/layout.json",
                    "reports/a11y.json",
                    "reports/copy_manifest.json",
                    "reports/provenance.json",
                ],
                required_cleanup_markers=["cleanup/cleanup.json"],
            ),
        )
    raise KeyError(scenario_id)


def load_public_scenario(scenario_id: str) -> PublicScenarioSurface:
    """Return one validated public scenario surface."""

    return PublicScenarioSurface.model_validate(_scenario_surface(scenario_id).model_dump(mode="json"))


def list_public_scenarios() -> list[PublicScenarioSurface]:
    """Return both public development scenarios in deterministic order."""

    return [load_public_scenario("incident_repair"), load_public_scenario("screenshot_to_product")]


def prepare_fresh_fixture_copy(scenario: PublicScenarioSurface, attempt_root: Path) -> tuple[Path, str, str]:
    """Copy one public fixture tree into the attempt directory."""

    source_dir = Path(scenario.fixture_source_dir)
    destination = attempt_root / "workspace"
    copy_tree(source_dir, destination)
    return destination, hash_tree(source_dir), hash_tree(destination)


def load_seal_manifest(scenario_id: str) -> SealManifest:
    """Return the unsealed future official variant manifest."""

    path = SEALS_ROOT / f"{scenario_id}.json"
    return SealManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
