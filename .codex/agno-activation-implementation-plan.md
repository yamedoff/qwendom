# AGNO activation implementation plan

Scope follows docs/AGNO_ACTIVATION_PLAN.md Immediate Next Sprint only:

1. Phase 0: add typed artifact schemas and additive session-state fields; register legacy governance outputs as artifacts without changing the visible event contract.
2. Phase 1 skeleton: tighten subtask shape/lifecycle and optionally wire deterministic delegation when the delegation feature flag is enabled; keep legacy child-spawn behavior as fallback.
3. Feature flags: add rollout flags in backend/config.py, default false, and branch new execution paths through Settings.
4. Validation: run compileall, backend preflight, frontend build if dependencies are available; also run focused deterministic task checks where feasible.
5. Review loop: use Opencode Go reviewers on the resulting diff, apply concrete findings, and repeat until clean or explicitly bounded by tool failure.

Architectural boundaries:
- Do not activate native debate/voting/routing/evaluation phases beyond feature flags in this sprint.
- Preserve deterministic no-key mode and all existing event names.
- Keep artifacts additive; final answer remains compatible with the existing frontend timeline.
- Avoid broad refactors of the orchestrator.
