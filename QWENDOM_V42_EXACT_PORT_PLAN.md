# Qwendom v4.2 Exact Port Plan

## Goal

Make the React app visually match the provided prototype source in `qwendom-v42-source`, prioritizing the Live run screen.

## Why the previous pass failed

The previous implementation used the Notion component summary instead of the actual prototype source. It kept the old two-column dashboard shell and only added a few v4.2-inspired widgets. The prototype is structurally different: fixed left rail, `main/wrap/view` content frame, hero metrics, 48-dot style phase strip, dial cards, typed turn feed, living brief, delegation, and artifact passport cards.

## Implementation plan

1. Use the prototype CSS as source of truth.
   - Import `qwendom-v42-source/core.css` into the React app.
   - Replace local stylesheet overrides that conflict with the prototype shell.

2. Replace the React layout shell.
   - Remove the old `shell` grid/right sidebar as the primary view.
   - Add prototype `rail`, `mobilebar`, `main`, `wrap`, and `view` structure.
   - Preserve existing API behavior: task creation, health, events, replay, agent memory data loading.

3. Port the Live run screen first.
   - Add hero: `kicker`, `hero-title`, `hero-sub`, `bignums`.
   - Add `dotstrip` with prototype dot/gate visuals.
   - Add `workspace` two-column grid.
   - Left column: `dials`, `floor-note`, `feed`, typed `.turn` cards.
   - Right column: working brief, delegation, artifacts using prototype card classes.

4. Map real data to prototype shapes.
   - Use current agents for office/dial cards.
   - Use latest working brief/readiness/events for metrics and cards.
   - Map events into typed turns where possible, with safe fallback turn cards.

5. Keep secondary operational panels available but not dominant.
   - Move replay and memory into prototype-style cards below the main Live run content so the rail/live screen matches the prototype.

6. Validate and review.
   - Run `npm run build`.
   - Use OpenCode Qwen Plus for implementation review.
   - Fix findings until review is clean.

## Acceptance criteria

- The page at `http://localhost:5173/` uses the prototype shell and looks recognizably like `qwendom-v42-source/src/live-run.html`.
- Existing backend/API flows still work.
- `npm run build` passes.
- OpenCode Qwen Plus final review returns no blocking visual-contract or functional findings.
