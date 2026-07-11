# Qwendom v4.2 All-Pages UI Port Plan

## Implemented pages

- `#intake`
  - Intake-only entry page for composing a mission prompt before starting a run.
- `#live`
  - Main society run view with composer, phase strip, room/feed, replay list, memory panel, and agent pool.
- `#review`
  - Review screen for inspecting event-derived governance and execution output.
- `#recap`
  - Recap screen for summarizing the session from the same event/task state.
- `#dossier`
  - Agent dossier screen backed by live agents when available and fallback office profiles otherwise.

## Routing

- Routing is hash-based in `frontend/src/App.tsx` through `useHashPage()`.
- Supported page keys are `intake`, `live`, `review`, `recap`, and `dossier`.
- Unknown or empty hashes currently resolve to `live`.
- Both the desktop rail and bottom navigation point into the same hash route state.

## Mocked backend fallback behavior

- Initial bootstrap now keeps the UI showable when backend reads fail:
  - `listAgents()` falls back to the existing static `fallbackAgents`.
  - `listTasks()` falls back to an empty local task list.
  - `getHealth()` falls back to `null`, which keeps the rail in `LOCAL SESSION`.
  - `listAgentMemory()` falls back to an empty list for the selected agent.
- These initial-load fallbacks no longer surface raw fetch or `TypeError` banners.
- `submit` and `intakeSubmit` now fall back to a local mock `TaskRun` when `createTask()` fails.
  - The mock run uses the submitted prompt.
  - The mock run is inserted into local `tasks`.
  - The mock run becomes the current `task`.
  - The app still navigates to `#live`.
- Mock runs are local-only:
  - No fake SSE stream is created.
  - No polling loop is started for the mock task.
  - Replaying a mock task restores it from local state instead of calling the backend.

## Remaining backend-integration gaps

- Mock runs do not generate society events, so live/review/recap remain visually usable but data-light when the backend is down.
- Clarification resume still depends on backend `submitClarification()` and has no local fallback.
- Replaying real tasks still depends on backend `getTask()` and `listTaskEvents()`.
- The live event stream error banner still appears for real backend stream failures; only local mock runs bypass stream setup.
- Health, task history, and agent memory still need live backend data for non-fallback fidelity.
