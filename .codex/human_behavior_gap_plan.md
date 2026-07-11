# Human Behavior Gap Implementation Plan

## Objective

Implement the highest-priority remaining human behavior gaps from
`docs/REMAINING_HUMAN_BEHAVIOR_GAPS.md` without broad architecture changes.

## Scope

1. Require transcript turns after the first speaker to reference a prior turn.
2. Add one targeted question-answer loop before readiness voting.
3. Preserve unanswered questions and unresolved dissent in the working brief.
4. Emit a meeting recap event that summarizes influence, plan changes, and
   remaining dissent for UI replay.
5. Surface the recap in the frontend timeline/coordination area.

## Validation

- Python syntax compile for `backend`.
- Frontend TypeScript/build validation.
- Deterministic smoke task through the FastAPI app if dependencies are present.
- Two external review passes through Opencode Go.
