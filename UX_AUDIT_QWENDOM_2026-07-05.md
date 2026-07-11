# Qwendom UX Pass Audit

Date: 2026-07-05
Repo: `C:\Users\boudi\Documents\gemma kingdom`
Frontend audited: `http://127.0.0.1:5173` -> `http://127.0.0.1:8002`
Offline/error check frontend: `http://127.0.0.1:5175` -> dead API `http://127.0.0.1:8999`

## Method

This pass used the repo contract in:

- `README.md`
- `docs/AGENT_UPGRADE_IMPLEMENTATION_PLAN.md`
- `backend/society/projections.py`
- `backend/society/orchestrator.py`
- `backend/society/workflow.py`

Audit journeys completed:

1. Fresh run
   - Task id: `b8843a48-d0d2-4652-90ef-c2fe380dec43`
   - Browser verified across Intake, Live, Review, Recap
2. Replayed completed run
   - Task id: `380c5537-2791-48a7-b69c-cf08d919d5ff`
   - Browser verified across Live, Review, Recap, Dossier
3. Offline/error state
   - Separate frontend instance bound to an unreachable backend
   - Verified no-mock intake, no-run review, empty dossier, and failed submit copy

Key backend evidence used:

- `GET /tasks/{task_id}/cockpit`
- `GET /tasks/{task_id}/review`
- `GET /tasks/{task_id}/recap`
- `GET /agents/{agent_id}/dossier`
- `GET /tasks/{task_id}/events`

## Executive Read

Qwendom already succeeds at one important promise from the repo contract: it does not fabricate missing society truth. Partial and missing evidence are surfaced honestly in the running UI, and the offline flow refuses to invent a society.

The core UX problem is not fake data. The core UX problem is that the frontend still wraps real backend truth in several heuristic or theatrical surfaces that overstate clarity:

- Live implies measured alignment, artifact competition, and office semantics that are not backed by projections.
- Review cannot explain the winning decision end-to-end because proposal truth is not emitted or projected consistently.
- Recap is structurally honest but shallow on social learning and dissent when the runtime does not emit stronger evidence.
- Dossier is backed by real memory and trust data, but it overloads the viewer with undifferentiated historical memory and does not clearly separate stable profile from run-specific behavior.

The product makes sense to a first-time viewer only at a high level: "agents are working on a task." It does not yet make the society legible in the human-collaboration terms the implementation plan promises:

- who believed what first
- why the leader was chosen
- what each agent proposed
- who objected
- what changed their mind
- why the final answer won
- what dissent remained

## Artifact 1: UX Audit Report

Scoring scale: `1` poor, `5` strong.

### Intake

| Dimension | Score |
| --- | --- |
| comprehension | 4 |
| task usability | 3 |
| state clarity | 3 |
| trust/honesty | 4 |
| demo legibility | 4 |

Findings:

1. `P1` The left rail can contradict the current screen state on first load.
   - Evidence:
     - Browser: initial screen content was Intake while the left rail highlighted `Live run`.
     - Frontend route default comes from `useHashPage()` and defaults to `"live"` when the hash is absent in `frontend/src/App.tsx`.
   - Root cause: `frontend`

2. `P1` Intake overclaims constraint behavior before the society has actually validated anything.
   - Evidence:
     - Copy says constraints are enforceable in the intake form.
     - In code the form only submits prompt text through `createTask()`; no pre-submit validation exists in `frontend/src/App.tsx`.
   - Root cause: `frontend`

3. `P2` Intake does not help the user structure a good brief around constraints, success criteria, and blockers even though the backend workflow explicitly depends on those concepts.
   - Evidence:
     - Browser: one freeform prompt box only.
     - Contract: `docs/AGENT_UPGRADE_IMPLEMENTATION_PLAN.md` expects visible working-group behavior around blockers, dissent, and decision rationale.
   - Root cause: `mixed`

4. `P2` The offline intake state is honest but still visually dense because Live-only scaffolding remains visible below the error banner.
   - Evidence:
     - Offline browser instance showed `Backend is offline or unavailable. Static navigation remains available; no society data is fabricated.`
     - The Room, Working Brief, Delegation, Artifacts, Replay, and Memory shells still render on the same page.
   - Root cause: `frontend`

What should stay in Intake:

- single concise mission entry
- explicit no-mock honesty
- path into Live as the primary product surface

What should move later into Live:

- office semantics
- room-state summary
- replay list
- memory teaser

Alignment with workflow phases:

- partially aligned
- the brief entry belongs to pre-execution conversation and working brief formation, but the current page does not explain that progression

### Live

| Dimension | Score |
| --- | --- |
| comprehension | 3 |
| task usability | 3 |
| state clarity | 2 |
| trust/honesty | 3 |
| demo legibility | 3 |

Findings:

1. `P0` Live still implies stronger society clarity than the backend can currently prove.
   - Evidence:
     - Browser fresh run showed `Offices aligned`, artifact standings like `LEADING` / `DRAFTING`, and animated office cards before the projections had emitted enough structured decision truth.
     - Frontend computes `Offices aligned` heuristically from roster count in `frontend/src/App.tsx`.
     - `artifactStanding()` assigns status from array position, not backend artifact state.
   - Root cause: `frontend`

2. `P0` The Room still reads partly as theatrical chrome instead of a trustworthy projection of social state.
   - Evidence:
     - Browser fresh run: cards labeled `Strategist`, `Archivist`, `Critic`, `Risk Officer` while backend roster ids are `architect`, `researcher`, `builder`, `critic`.
     - Frontend role mapping is inferred from `roleKeyOf()` string matching in `frontend/src/App.tsx`.
   - Root cause: `frontend`

3. `P1` Phase clarity depends on frontend interpretation when backend projection is absent or stale.
   - Evidence:
     - `currentPhase` can fall back to frontend event mapping in `phaseOf()` in `frontend/src/App.tsx`.
     - Browser fresh run moved between `setup / team formation`, `leader election`, and `work / review` while projections lagged the stream.
   - Root cause: `mixed`

4. `P1` Live shows truthful partial-evidence states, but they are not always matched to user questions.
   - Evidence:
     - Fresh run Live showed `No delegation events have been emitted for this run` and `No artifact events have been emitted for this run`.
     - That is honest, but it does not answer whether delegation has not happened yet, was not emitted, or is blocked by runtime design.
   - Root cause: `projection layer`

5. `P1` Replay is useful but hard to parse as a first-time demo surface.
   - Evidence:
     - Browser replay list is long, text-heavy, and mixed across running, failed, and complete states.
     - No filtering by status, recency, or evidence completeness.
   - Root cause: `frontend`

6. `P2` Event visibility is filtered by frontend event-type allowlists, so the viewer cannot know whether all emitted events are shown.
   - Evidence:
     - `typedEventTypes` and `systemEventTypes` in `frontend/src/App.tsx` define what appears.
   - Root cause: `frontend`

7. `P2` Stream failure honesty is incomplete.
   - Evidence:
     - Code sets `The live event stream stopped. The task status will keep polling.`
     - Existing projections remain visible with no explicit stale marker.
   - Root cause: `mixed`

Required Live assessment:

- Timeline ordering is understandable at the coarse phase level.
- It is not yet intelligible enough for the contract-level questions about objections, endorsements, delegation provenance, or winner rationale.
- The Live view does reveal Agno workflow checkpoints, but more as implementation metadata than as a human story.
- Replay is functional, not demo-legible.

### Review

| Dimension | Score |
| --- | --- |
| comprehension | 3 |
| task usability | 3 |
| state clarity | 3 |
| trust/honesty | 4 |
| demo legibility | 2 |

Findings:

1. `P0` `DecisionReview` is not enough to explain why the final answer won.
   - Evidence:
     - Completed run browser showed `DECISION RECORD · PARTIAL`, `Selected: builder`, `04 Ballots emitted`, `00 Proposals with evidence`.
     - Backend `GET /review` for completed run returned `selected_winner`, ballots, and critique, but `proposals: []` with `missing_sources: ["proposals"]`.
     - Backend recap metrics simultaneously reported `proposals_count: 4`.
   - Root cause: `projection layer`

2. `P1` Missing-evidence honesty is strong, but the resulting explanation is still incomplete for a demo viewer.
   - Evidence:
     - Browser clearly said `Evidence is partial. Missing sources: proposals.`
     - Yet the screen cannot show the actual winning proposal even though the winner is known.
   - Root cause: `projection layer`

3. `P1` Unresolved dissent is underpowered.
   - Evidence:
     - Completed run Review showed critique text and risks, but `00 Dissents preserved`.
     - Critique exists, but there is no stronger distinction between critique, dissent, blocking objection, and carried-forward caveat.
   - Root cause: `Agno/runtime`

Required Review assessment:

- `DecisionReview` is not sufficient today for a real decision explanation.
- Additional proposal truth and explicit winner rationale projection are needed.

### Recap

| Dimension | Score |
| --- | --- |
| comprehension | 4 |
| task usability | 3 |
| state clarity | 4 |
| trust/honesty | 4 |
| demo legibility | 3 |

Findings:

1. `P1` Recap is structurally solid but still shallow on social learning.
   - Evidence:
     - Completed run Recap showed `01 Lessons saved`, but the lesson content was only `Agents updated memory and reputation from the collaboration.`
     - Contract expects legible learning and trust change, not just that memory was updated.
   - Root cause: `Agno/runtime`

2. `P1` Recap under-answers "why" compared with "what".
   - Evidence:
     - It shows counts, duration, plan changes, and final answer.
     - It does not summarize why the winner won, why no dissent remained, or what changed in the social dynamics.
   - Root cause: `projection layer`

3. `P2` Validation caveats are present in the final answer text but not elevated as a first-class recap outcome.
   - Evidence:
     - Completed run final answer includes `Validation note: ... 3 subtask(s) have blockers.`
     - Recap does not elevate that as a top-level risk badge or run outcome summary.
   - Root cause: `projection layer`

Required Recap assessment:

- Useful for demos: yes, at the outcome-summary level.
- Useful for debugging: partly.
- It needs stronger why/dissent/trust summaries to satisfy the product contract.

### Dossier

| Dimension | Score |
| --- | --- |
| comprehension | 2 |
| task usability | 2 |
| state clarity | 2 |
| trust/honesty | 4 |
| demo legibility | 2 |

Findings:

1. `P0` Dossier overloads the viewer with undifferentiated historical memory.
   - Evidence:
     - Browser dossier for Ada showed a long mixed memory stream spanning old tasks, child-agent notes, smoke tests, and collaboration strings.
     - Run-specific memory is not visually separated from durable cross-run memory.
   - Root cause: `frontend`

2. `P1` Dossier does not clearly separate stable profile from observed behavior in this run.
   - Evidence:
     - Browser page mixes `behavioural tendencies`, `failure mode`, and a long memory feed in one pass.
     - `behavioral_tendencies` can fall back to profile defaults in `frontend/src/App.tsx`.
   - Root cause: `mixed`

3. `P1` Dossier does not answer the product question "what did this agent believe or learn in this run versus across runs?"
   - Evidence:
     - Endpoint is not task-scoped.
     - Browser page shows one `recent_stances` summary and a large durable memory archive, but not a clear per-run split.
   - Root cause: `projection layer`

4. `P2` Dossier is honest when no backend agents exist.
   - Evidence:
     - Offline browser showed `No backend agents are available. The dossier will not substitute demo offices.`
   - Root cause: none; this is correct behavior

Required Dossier assessment:

- Informative today: profile, trust, some tendencies, real memory existence.
- Generic metadata today: much of the profile framing and the memory wall.
- Missing evidence needed: run-scoped beliefs, run-scoped lessons, stable behavioral summaries separated from raw memory.

## Artifact 2: Agent Observability Gap Matrix

| User question | Current backend evidence source | Current UI surface | Gap type | Required fix |
| --- | --- | --- | --- | --- |
| who joined the run | `team_formed`, `RunCockpit.team` | Live The Room, live roster | frontend-only | Replace themed office framing with explicit joined roster and role reason chips |
| why the leader was chosen | `leader_elected` event payload includes `reason`, `task_class`, `leadership_score` | not clearly surfaced; only implicit in live/recap | frontend-only | Render explicit leader rationale block from event payload in Live and Review |
| what each agent believed first | `agent_goal_opinion`, `conversation_turn`, `agent_position_stated` | partially visible in Live event stream | projection-only | Add first-position projection keyed by agent and phase |
| what each agent proposed | tool-call proposal results exist inconsistently; review projection lacks completed proposal list | Review shows zero proposals on completed run | mixed | Emit stable proposal events or project proposal artifacts from tool results into `DecisionReview` |
| who objected | `agent_objection_registered`, readiness blockers, `working_brief.unresolved_dissent` | only visible if emitted; absent in audited runs | Agno/runtime-only | Emit typed objection records with severity and target in more runs |
| what blocked execution | readiness blockers, validation gate failures, subtask blockers | Live blocker copy, final answer caveat | projection-only | Add top-level blocker summary projection for Live and Recap |
| who changed position | `agent_changed_mind` -> `RunRecap.mind_changes` | Recap count only if emitted | Agno/runtime-only | Emit explicit mind-change records and link them to prior stance and trigger |
| what work was delegated | `subtasks_assigned_from_brief`, collaboration action events | Live delegation panel | mixed | Project assignment provenance, owner, status, blocker, and outcome explicitly |
| what tools were used | `tool_call` events, metrics `tool_calls_total` | only implicit in event stream/metrics | projection-only | Add per-agent tool usage summary for Live and Dossier |
| why the final answer won | ballots, critique, selected winner, final answer | Review partial; Recap final answer only | projection-only | Add winner rationale projection built from ballots, critique, and selected artifact |
| what unresolved dissent remained | `working_brief.unresolved_dissent`, `meeting_recap`, objection events | Review/Recap only if present | Agno/runtime-only | Emit richer dissent artifacts and carry them into recap even when proceeding |
| what each agent learned after the run | `learning_recorded`, memory writes, trust updates | Dossier memory wall, Recap one lesson line | mixed | Project run-scoped lessons per agent and summarize durable updates separately |

## Artifact 3: Frontend UX Backlog

### Navigation And Information Architecture

1. `P0` Make page state and rail state impossible to contradict.
   - Intended user outcome: the selected page is always visually unambiguous.
   - Data dependency: none
   - Exact UI change: route default to `#intake` or derive initial page from visible screen intent instead of defaulting to `live`; ensure rail state matches page body.
   - Acceptance criteria: first load with no hash never highlights a different page than the content being shown.

2. `P1` Split Intake from Live scaffolding.
   - Intended user outcome: first-time viewers understand they are briefing a run, not already inside one.
   - Data dependency: none
   - Exact UI change: keep Intake focused on prompt entry, product promise, and brief guidance; move Room, Replay, Memory, and artifact shells below a clear "after run starts" boundary or hide them before a run exists.
   - Acceptance criteria: with no selected task, the page has a clear pre-run state and does not imply active society state.

### Live Run Readability

3. `P0` Remove heuristic alignment and artifact-competition labels unless backed by projections.
   - Intended user outcome: viewers trust that emphasis equals backend truth.
   - Data dependency: either existing projection fields or new ones
   - Exact UI change: remove or relabel `Offices aligned`, `LEADING`, `CHALLENGED`, `DRAFTING`, and heuristic artifact counts unless those are projection-backed.
   - Acceptance criteria: every metric or badge on Live can be traced to a backend field or emitted event.

4. `P0` Replace office theater with explicit agent-state framing.
   - Intended user outcome: viewers can tell who is participating and what state each agent is in.
   - Data dependency: existing roster plus stance/blocker/delegation projections
   - Exact UI change: show agent cards with role, current stance, latest contribution type, and status source instead of themed office labels.
   - Acceptance criteria: a first-time viewer can map every displayed card to a real backend agent id and current evidence source.

5. `P1` Add a "why this phase" explainer driven by projection truth.
   - Intended user outcome: viewers understand what just happened and why the run is in the current phase.
   - Data dependency: `RunCockpit.current_phase`, gates, latest relevant event
   - Exact UI change: summarize current phase, last meaningful checkpoint, and what evidence is still missing.
   - Acceptance criteria: Live always shows current phase, last checkpoint, and missing next-step evidence without relying on inferred frontend semantics alone.

6. `P1` Make replay selectable and filterable.
   - Intended user outcome: demo viewers can quickly open a useful completed run.
   - Data dependency: task list status and timestamps
   - Exact UI change: add status filters, sort by recent, and compact cards with status/evidence badges.
   - Acceptance criteria: a completed run can be opened from replay in under three clicks without scrolling through mixed statuses.

### Review Clarity

7. `P0` Surface missing proposal truth as the primary review defect.
   - Intended user outcome: viewers immediately understand whether the decision record is complete enough to trust.
   - Data dependency: `DecisionReview.evidence_status`, `missing_sources`
   - Exact UI change: elevate missing proposal evidence above the ballot list, and explain what that absence prevents the user from knowing.
   - Acceptance criteria: a completed run with ballots but no proposals states that winner rationale is incomplete because proposal evidence is missing.

8. `P1` Add winner rationale summary.
   - Intended user outcome: reviewers can understand the winning logic without reading every ballot.
   - Data dependency: selected winner, ballots, critique, proposal summary
   - Exact UI change: add a top-level "why this won" card.
   - Acceptance criteria: completed review shows a compact winner rationale sourced from backend fields only.

### Recap Usefulness

9. `P1` Promote validation caveats and blockers to recap summary.
   - Intended user outcome: viewers can tell whether a run completed cleanly or with caveats.
   - Data dependency: validation gate payload, blocked subtasks, final answer caveats
   - Exact UI change: add a recap outcome banner for `clean`, `partial`, or `completed with caveats`.
   - Acceptance criteria: audited completed run displays caveated completion without requiring the user to parse the final answer body.

10. `P1` Show social delta, not just counts.
   - Intended user outcome: recap answers what changed in the society.
   - Data dependency: mind changes, trust updates, dissent, lessons
   - Exact UI change: add compact per-agent deltas and run-scoped lessons.
   - Acceptance criteria: recap names which agents changed, gained trust, or learned something when the backend emits that truth.

### Dossier Usefulness

11. `P0` Separate profile, this-run evidence, and durable history.
   - Intended user outcome: viewers can distinguish stable character from current-run behavior.
   - Data dependency: dossier plus selected run context
   - Exact UI change: three explicit sections: profile, current run, durable memory.
   - Acceptance criteria: viewers can answer "how does Ada behave generally?" and "what did Ada do in this run?" without reading the full memory wall.

12. `P1` Compress and classify memory.
   - Intended user outcome: Dossier remains legible for demos.
   - Data dependency: memory record tags, dates, mode, task ids
   - Exact UI change: add task-grouping, recency filters, and compact summaries instead of raw long-form memory cards.
   - Acceptance criteria: dossier loads with a concise summary first and expands into raw memory only on demand.

### Empty, Loading, Offline, Error States

13. `P1` Distinguish empty, loading, offline, and partial explicitly on every surface.
   - Intended user outcome: users know whether data is absent, loading, stale, or unreachable.
   - Data dependency: fetch lifecycle state plus existing evidence status
   - Exact UI change: add explicit fetch states instead of jumping directly from null to empty copy.
   - Acceptance criteria: each screen can display `loading`, `empty`, `offline/error`, and `partial evidence` as separate states.

14. `P1` Mark stale projections after stream interruption.
   - Intended user outcome: users do not mistake frozen projections for live state.
   - Data dependency: EventSource error plus last successful projection refresh time
   - Exact UI change: add `stream stopped; snapshot may be stale` state to Live, Review, and Recap.
   - Acceptance criteria: after stream failure, visible projections carry an explicit stale badge until refreshed.

## Artifact 4: Agno And Projection Backlog

1. `P0` Project proposals into `DecisionReview`.
   - Missing truth belongs in: `projection model`
   - Rationale: completed runs currently have ballots and a winner without proposal evidence on the Review page.
   - Acceptance criteria: completed `GET /review` returns proposal summaries, producer ids, and source event ids whenever a proposal contributed to the final decision.
   - Safe independently: yes

2. `P0` Emit explicit winner-rationale artifact.
   - Missing truth belongs in: `emitted SocietyEvent`
   - Rationale: winner selection is inferable from ballots, but the product contract needs a human-meaningful rationale.
   - Acceptance criteria: completed runs emit a winner rationale record tying the selected answer to proposal, critique, and vote outcome.
   - Safe independently: yes

3. `P1` Emit richer objection records during debate and readiness.
   - Missing truth belongs in: `Agno tool output`
   - Rationale: current audited runs can complete with critique but without explicit objection/dissent artifacts.
   - Acceptance criteria: objections include source agent, target, severity, blocking status, and condition to clear.
   - Safe independently: yes

4. `P1` Add explicit proceeding-with-dissent signal.
   - Missing truth belongs in: `workflow/session state`
   - Rationale: unresolved dissent is currently a list when present, but the product contract also wants to know when the team proceeded despite disagreement.
   - Acceptance criteria: working brief or recap has a boolean or equivalent explicit marker when execution proceeds with dissent.
   - Safe independently: yes

5. `P1` Add delegation provenance and outcome summaries.
   - Missing truth belongs in: `projection model`
   - Rationale: subtasks exist, but the UI cannot answer delegated-work provenance cleanly.
   - Acceptance criteria: cockpit projection returns assignee, requester, success criteria, blocker state, and outcome for each delegated unit.
   - Safe independently: yes

6. `P1` Add per-agent tool usage summaries.
   - Missing truth belongs in: `projection model`
   - Rationale: raw `tool_call` events exist, but human-readable tool usage does not.
   - Acceptance criteria: cockpit and dossier projections expose grouped tool usage by agent and phase.
   - Safe independently: yes

7. `P1` Emit mind-change linkage.
   - Missing truth belongs in: `emitted SocietyEvent`
   - Rationale: current structure can say that a mind change occurred, but not what prior claim was overturned or why.
   - Acceptance criteria: `agent_changed_mind` links prior stance, trigger evidence, and new stance.
   - Safe independently: yes

8. `P1` Add run-scoped learning summaries per agent.
   - Missing truth belongs in: `projection model`
   - Rationale: memory writes are real but not legible as run-scoped lessons.
   - Acceptance criteria: dossier and recap expose per-agent lessons from the selected run separately from durable memory history.
   - Safe independently: yes

9. `P2` Separate public room truth from profile/default fallback truth in dossier.
   - Missing truth belongs in: `projection model`
   - Rationale: current dossier can fall back to default blockers in ways that blur static profile with observed behavior.
   - Acceptance criteria: every dossier tendency is labeled as either observed evidence or profile default.
   - Safe independently: yes

10. `P2` Expose projection freshness.
    - Missing truth belongs in: `projection model`
    - Rationale: stale-vs-live ambiguity matters after stream interruption.
    - Acceptance criteria: projections expose `generated_at` and source-event coverage so the frontend can mark stale state correctly.
    - Safe independently: yes

## Fresh Run And Replay Notes

Fresh run summary:

- Honest partial-evidence states are already strong.
- Live showed no fabricated delegation or artifact data.
- Review and Recap correctly admitted missing evidence during execution.

Completed replay summary:

- Cockpit and Recap are substantially more complete and useful than Review.
- Review becomes the weakest truth surface because it cannot show proposals on a completed run even when a winner exists.
- Dossier is real but not legible enough for demos.

## Highest-Leverage Next Moves

1. Fix Review first by projecting proposals and winner rationale.
2. Remove or relabel heuristic Live metrics that imply backend certainty.
3. Split Dossier into profile, this-run evidence, and durable history.
4. Add explicit stale/loading state handling so the UI distinguishes unavailable truth from still-loading truth.
