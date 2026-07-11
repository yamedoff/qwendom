# Product QA Log

## Internal Map

- Frontend: `frontend/src/App.tsx` is the React control room with intake, live run, review, recap, and dossier screens. API calls are centralized in `frontend/src/api.ts`.
- Backend: `backend/main.py` exposes FastAPI endpoints for agents, tasks, event streams, cockpit, review, recap, and dossiers.
- Run state: task summaries live in the in-process `SocietyOrchestrator`; event and memory history are persisted under `backend/society/data`.
- LLM configuration: `backend/config.py` reads `LLM_PROVIDER` plus provider keys from `backend/.env`. The app can run honestly without keys through fallback behavior, and `/health` reports provider/model/LLM status.
- Demo path: intake or live composer -> `POST /tasks` -> live event stream and polling -> review/recap/dossier projections from recorded events.

## QA Plan

1. Start the local stack from a clean terminal and walk the judge path as a first-time user.
2. Delegate a bounded OpenCode Go worker review of the same judge path and reconcile findings against live behavior.
3. Fix only issues on the demo path or likely judge stumble paths: setup, first impression, progress visibility, duplicate/hostile actions, resize, refresh/restart, and clear failure messages.
4. Re-run the app after each fix and record issue -> discovery action -> severity -> fix -> verification.
5. Finish with three clean end-to-end judge-path runs and two independent review passes.

## Issues

| ID | Issue | How found | Severity | Fix | Re-verified |
| --- | --- | --- | --- | --- | --- |
| QA-001 | Fresh setup can start backend on `8000` while the frontend defaults to `8001`, making the app look offline after following `npm run dev`. | Read `package.json`, `README.md`, and `frontend/src/api.ts` during Step 0 orientation. | blocker | Changed the frontend default API base to `http://localhost:8000`. | Pending |
| QA-002 | `npm run dev` fails to start the backend on Windows because the script calls `uvicorn` directly when it is not on PATH. | Ran the documented `npm run dev` command from a fresh shell. | blocker | Changed the backend dev script to `python -m uvicorn main:app --reload --port 8000`. | Pending |
| QA-003 | Empty mission submit prints raw FastAPI validation JSON in the product UI. | Clicked `Convene society` with an empty textarea on the live screen. | judge-visible | Added frontend prompt validation and normalized API error messages. | Pending |
| QA-004 | Fast duplicate submits can queue multiple runs or leave the judge unsure whether the first click worked. | Inspected submit path while testing hostile click behavior. | judge-visible | Added a submitting/running guard and disabled the live composer button while a run is starting or active. | Pending |
| QA-005 | A real LLM run can pause for 30-40 seconds between visible event updates and the full path took about 304 seconds, which can feel frozen to a judge. | Submitted a real task through `POST /tasks` and monitored event count/status until completion. | judge-visible | Added a live heartbeat/status strip with last-event age and clear waiting copy. | Pending |
| QA-006 | A run can fail during the planning meeting if the model does not return a parseable `submit_goal_discussion` tool result after retry. | Second clean-run attempt failed at `learn_and_measure` with `No tool result found for 'submit_goal_discussion'`. | blocker | Added a typed conservative fallback for non-critical goal-discussion tool failures while preserving a failed tool-call event. | Pending |
| QA-007 | A run can fail during debate when `memory_lookup` receives structured memory objects instead of strings. | Third clean-run attempt failed at `debating` with `relevant_memories.list[str]` validation errors. | blocker | Made `memory_lookup` accept list entries of any shape and stringify them into readable memory text. | Pending |
| QA-008 | Runs can fail when provider traffic makes `cast_readiness_vote` or other non-critical social/governance tools time out. | Three-run acceptance attempt failed with `Timeout calling cast_readiness_vote`. | blocker | Added typed fallback records for readiness, goal discussion, working-brief positions, and private notes. | Pending |
| QA-009 | Runs can fail when `assign_subtask` times out, even though the planned subtask data already exists locally. | Three-run acceptance attempt failed with `Timeout calling assign_subtask`. | blocker | Wrapped assignment with a local planned-subtask fallback and emitted the failed tool-call event. | Pending |
| QA-010 | Runs can remain `running` after the meeting recap if Agno Team coordination stalls before emitting a coordination event. | Three-run acceptance attempt stalled at 50 events with last event `meeting_recap`. | blocker | Added a partial `TeamCoordinationBrief` fallback for Agno Team coordination failures/timeouts. | Pending |
| QA-011 | Runs can fail with a blank error if an agent work-product call times out during subtask reporting. | Final acceptance attempt failed with empty `task_failed` message after a subtask assignment/report sequence. | blocker | Added fallback work products and report records with explicit recovered tool-call events. | Pending |
| QA-012 | Runs can remain `running` after selecting a role capability bundle if the capability tool call wedges or times out. | Final acceptance attempt stalled after `agent_tool_bundle_selected` for the builder bundle. | blocker | Added role-specific capability fallbacks for decomposition, memory lookup, implementation planning, and risk assessment. | Pending |

## Decisions Needed

- None yet.
