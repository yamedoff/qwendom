# Qwendom V3 Implementation Plan

## Objective

Implement the V3 architecture from `ARCHITECTURE_V3.md` as an incremental,
deployable vertical slice that keeps the existing HTTP API and deterministic
no-key development mode working.

## Scope

1. Add V3 session-state primitives for governance state.
2. Add an Agno Team builder that can be used by LLM-enabled orchestration.
3. Add debate, voting, delegation, knowledge, and evaluation tool modules with
   documented, JSON-serializable behavior.
4. Refactor the orchestrator to maintain shared session state, emit debate and
   metrics events, and preserve current task endpoints.
5. Add queryable aggregate metrics through `GET /metrics`.
6. Validate with `python -m compileall backend` and the frontend build.

## Constraints

- Preserve existing endpoints and event names unless an added V3 event is
  explicitly additive.
- Preserve deterministic no-key mode and label deterministic tool calls as
  `deterministic_no_key`.
- Do not add a silent fallback from Agno Team/Workflow to the old manual path in
  LLM-enabled mode.
- Keep session state JSON-serializable.
- Keep edits minimal and localized to `backend/society`, `backend/main.py`, and
  validation-supporting files when needed.

## Review Criteria

- No syntax or import errors in the backend.
- The frontend production build still passes.
- Metrics are persisted through the JSONL event stream.
- New code is documented where behavior is not obvious.
- The implementation does not claim full V3 completion unless all phases are
  actually implemented.
