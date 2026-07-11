# Qwendom Full QA Findings

Date: 2026-07-05

Scope: This document consolidates the full QA evidence collected across the entire thread for Qwendom on the local dev stack:

- Frontend: `http://127.0.0.1:5173`
- Backend: `http://127.0.0.1:8002`
- Browser surface: Codex in-app browser only

The goal of the pass was to exercise the product from intake through live run, review, recap, and dossier surfaces using actual backend behavior only, then correlate visible UI state with backend truth and runtime failures.

## Environment

- Frontend local override: `frontend/.env.local` points `VITE_API_BASE=http://127.0.0.1:8002`
- Backend `/health` returned:
  - `status=ok`
  - `provider=cerebras`
  - `llm_enabled=true`
  - `model=gemma-4-31b`
- Backend was started on `127.0.0.1:8002`
- Frontend was started on `127.0.0.1:5173`

## QA Methods Used

- Live route inspection in the in-app browser
- Cross-route navigation across `#intake`, `#live`, `#review`, `#recap`, `#dossier`, and `#dossiers`
- Backend truth checks against:
  - `GET /tasks`
  - `GET /tasks/{task_id}`
  - `GET /tasks/{task_id}/events`
  - `GET /tasks/{task_id}/review`
  - `GET /tasks/{task_id}/recap`
  - `GET /agents`
- Screenshot capture from the in-app browser, including widened viewport recaptures

## Runs Examined

## Fresh QA run created during this thread

Task id: `c255116c-3e19-4dd7-b781-edb7f02c51aa`

Prompt:

`QA FLOW: Design a hackathon-ready demo plan for Qwendom that visibly shows agent disagreement, one delegated subtask, a leader decision, and a final answer with caveats if evidence is incomplete. Constraints: preserve honest uncertainty, no fabricated evidence, keep it demoable in one run, and make the output understandable to judges watching the UI live.`

Observed lifecycle:

- Began in setup/team formation
- Advanced through goal discussion
- Reached readiness voting
- Reached `working_brief`
- Never emitted proposals, ballots, leader synthesis, or delegation outcomes
- Eventually failed with a backend validation error

Final backend failure:

`1 validation error for TeamCoordinationBrief`

`confidence`

`Input should be a valid number [type=float_type, input_value=None, input_type=NoneType]`

Phase:

- `learn_and_measure`

Result:

- task status became `failed`
- no final answer was produced

## Earlier runs used as comparison points

- A completed historical run demonstrated that the app can render a populated Live, Review, and Recap state when enough truth exists
- Several failed historical runs showed honest missing-evidence states, but also exposed weaknesses in how the UI presents those states

## Confirmed Findings

## 1. Backend crash blocks end-to-end completion

Severity: Critical

The main fresh QA run failed in backend orchestration, not due to the browser.

Confirmed backend evidence:

- `task_failed`
- `run_failed`
- `failure_recovery_attempted`

Failure reason:

- `TeamCoordinationBrief.confidence` received `None`
- Pydantic rejected it because a float was required

Impact:

- The run never reached proposal generation
- Review remained empty
- Recap remained partial
- No final synthesis or final answer was produced

## 2. `#dossier` and `#dossiers` are split, and only one is real

Severity: High

Confirmed behavior:

- The visible nav links point to `#dossier`
- `#dossier` is the real dossier route
- `#dossiers` is a different surface that falls back into run/live-like content

Observed on the browser:

- `#dossier` shows per-agent dossier content
- `#dossiers` renders the run view pattern instead of dossier content

Impact:

- The product has a route mismatch in a primary navigation surface
- Users can end up on a broken route depending on the path used

## 3. Review truth is honest, but often empty because proposal truth is absent

Severity: High

Confirmed behavior on the fresh run:

- `0 proposal(s) in play`
- no ballots
- no critique events
- no selected winner
- missing sources:
  - `proposals`
  - `ballots`
  - `selected_winner`
  - `critiques`

Backend `/review` for the fresh failed QA run confirmed:

- `proposals=[]`
- `ballots=[]`
- `selected_winner=null`
- `leader_synthesis=null`
- `winner_rationale=null`

Interpretation:

- This was not a frontend fabrication problem
- Review was honestly representing missing proposal truth

Product problem:

- Honest failure is present
- But the UX is weak because the page has little diagnostic help when proposal truth never materializes

## 4. Recap renders while the run is still in progress and remains partial after failure

Severity: High

Observed behavior:

- During the run, Recap rendered `Run recap` with a partial after-action state even though the task was still active
- After failure, it still remained partial because `task_complete` and `metrics` were missing

Backend `/recap` confirmed:

- `status=failed`
- `completion_outcome=failed`
- `metrics=null`
- `final_answer=null`
- missing sources included `task_complete` and `metrics`

Impact:

- Recap does expose real backend truth
- But it mixes in-progress and after-action concepts in a confusing way
- The result feels incomplete rather than deliberately diagnostic

## 5. Live route is overloaded by replay, memory, and tool-usage text

Severity: High

Observed behavior:

- Large duplicated text blocks from replay and memory surfaces flood the page
- The current-turn evidence competes with side data
- The page becomes hard to scan even when the core run state is valid

Specific consequences:

- The actual meeting-room evidence is harder to follow
- Replay/history content visually dominates the run
- Old memory payloads produce extreme information density

## 6. Dossier controls exist, but normal interaction is fragile

Severity: High

Confirmed behavior:

- Dossier buttons for `Ada`, `Researcher`, `Lin`, and `Noor` are present in the visible DOM
- Standard Playwright clicks repeatedly timed out on these controls
- Low-level DOM click (`dom_cua.click`) succeeded

Observed consequence:

- The page is not non-existent
- The interaction layer is unreliable or fragile

Additional detail:

- After switching to `Researcher`, the dossier correctly showed:
  - run summary
  - phase
  - contributions
  - run timeline
  - published notes
  - tool-usage details

This proves the route is meaningful when interacted with through the lower-level path.

## 7. Replay/history selectors are visible but effectively broken

Severity: High

Observed on `#live`:

- Replay/history buttons for past runs are rendered
- Their text is visible
- Their layout boxes can be measured

However:

- Normal Playwright clicks timed out
- DOM eval click approaches did not work reliably
- Low-level coordinate clicks also failed because hit areas did not line up cleanly
- The task binding never switched to the selected historical run during this QA attempt

Impact:

- Cross-run browsing appears broken or at least highly unreliable
- Historical comparison inside the UI is not dependable

## 8. Dossier full-page capture duplicates the page vertically

Severity: Medium

Observed artifact:

- `researcher-dossier-full.png` showed the dossier page repeated vertically multiple times

Interpretation:

- This may reflect route-specific layout or full-page rendering behavior
- Even if partly capture-related, it is evidence that this surface behaves differently from the others

Impact:

- The route likely has unusual layout or scroll behavior
- It is another sign that the dossier surface needs closer frontend attention

## 9. `TOUR` control is inconsistent or non-actionable on some surfaces

Severity: Medium

Observed behavior:

- `TOUR` is visible on multiple routes
- On the dossier route, both normal Playwright click and later DOM-based attempts failed to produce a usable tour state

Impact:

- A visible onboarding affordance appears unreliable
- The issue is especially notable on a route already showing interaction fragility

## 10. Route naming and nav labels are inconsistent

Severity: Medium

Examples:

- top nav and link text say `Dossiers` or `Dossier`
- the valid route is singular: `#dossier`
- a broken plural route also exists: `#dossiers`

Impact:

- This increases ambiguity
- It likely contributed to the route bug surviving in the first place

## 11. Fresh run state transitioned in ways that can mislead the user

Severity: Medium

Observed sequence:

- The task remained `running` long after only `working_brief`-level truth existed
- Review stayed empty
- Recap rendered partial
- Then the task finally failed at the backend

Impact:

- A user can interpret this as UI stalling rather than orchestration failure
- The UI does not cleanly differentiate:
  - still progressing
  - missing proposal truth
  - hard backend failure

## 12. Older completed runs can still show partial or missing evidence banners

Severity: Medium

Observed earlier in the thread:

- Historical completed surfaces sometimes still showed evidence banners such as missing proposal sources

Interpretation:

- These older runs likely predate the newer truth rollout
- This is not necessarily a current frontend bug

But:

- It still affects product trust because the UI does not help distinguish “old event schema” from “current run is broken”

## What Was Verified As Working

The QA pass did confirm that several major concepts do exist and can surface meaningful truth when the right route and interaction path are used:

- Live route renders actual backend event history
- Failed runs do surface their real failure phase and error text
- Review honestly reports missing proposal truth instead of inventing data
- Recap pulls real failure metadata from the backend
- Real dossier content exists on `#dossier`
- Dossier per-agent state can show run-scoped notes and timeline data

## Backend Truth Correlations

## Fresh failed QA run

Task id:

- `c255116c-3e19-4dd7-b781-edb7f02c51aa`

Confirmed events emitted before failure:

- `goal_discussion_started`
- `agent_goal_opinion`
- `conversation_turn`
- `agent_position_stated`
- `readiness_vote_cast`
- `readiness_vote_tallied`
- `working_brief_finalized`
- `private_note_published`
- `meeting_recap`

Confirmed events never emitted for this run:

- proposals
- ballots
- selected winner
- delegation outcomes
- task completion
- final answer

Failure termination events:

- `failure_recovery_attempted`
- `run_failed`
- `task_failed`

## Screenshots and Artifacts

## Initial route screenshot set

Folder:

- `C:\Users\boudi\Documents\gemma kingdom\.qa-screenshots\qa-2026-07-05T04-02-22-130Z`

Important files:

- `manifest.json`
- `live-full.png`
- `review-full.png`
- `recap-full.png`
- `dossiers-full.png`

## Failed-state screenshots

Folder:

- `C:\Users\boudi\Documents\gemma kingdom\.qa-screenshots\qa-2026-07-05T04-02-22-130Z\final-failed-state`

Files:

- `live-failed-full.png`
- `review-failed-full.png`
- `recap-failed-full.png`

## Wide recapture set

Folder:

- `C:\Users\boudi\Documents\gemma kingdom\.qa-screenshots\recapture-2026-07-05T04-05-25-849Z`

Files:

- `live-full-wide.png`
- `review-full-wide.png`
- `recap-full-wide.png`
- `dossiers-full-wide.png`

## Real dossier artifact

File:

- `C:\Users\boudi\Documents\gemma kingdom\.qa-screenshots\researcher-dossier-full.png`

## Ordered Fix Priority

## 1. Fix backend orchestration crash

Target:

- `TeamCoordinationBrief.confidence` validation path

Reason:

- This is the blocker preventing end-to-end run completion

## 2. Resolve `#dossier` vs `#dossiers`

Reason:

- The product currently exposes both, but only one is real

## 3. Repair replay/history interaction

Reason:

- Users cannot reliably inspect prior runs from the UI

## 4. Reduce Live information flooding

Reason:

- The core run becomes hard to follow because replay, memory, and tool-usage text dominate the page

## 5. Improve failure-mode Review and Recap UX

Reason:

- The app is honest about missing truth, but the presentation is still confusing and low-signal

## 6. Stabilize dossier interaction behavior

Reason:

- The route has value, but standard clicks repeatedly fail

## Final Conclusion

This thread established that Qwendom does have real backend-driven truth and meaningful run surfaces, but the current product still has several structural failures:

- one confirmed backend crash that stops end-to-end completion
- one route split where only the singular dossier route is real
- one broken replay/history interaction surface
- overloaded live-state presentation
- weak failure UX in Review and Recap
- fragile dossier interaction behavior

The QA pass was not blocked by lack of evidence. It produced concrete, reproducible findings tied to both browser-rendered state and backend task/event truth.
