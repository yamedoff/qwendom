# Qwendom Society Runtime

This document describes the current V3 Qwendom Agent Society implementation:
what exists, how a task runs, which agents and tools participate, and which
collaboration patterns the system demonstrates.

## System Shape

Qwendom is a FastAPI + React application backed by Agno agents, teams, tools,
workflow checkpoints, local JSONL events, and an Agno SQLite database.

Main runtime modules:

| Area | File | Responsibility |
|---|---|---|
| API | `backend/main.py` | FastAPI routes, SSE task stream, metrics endpoint |
| Orchestration | `backend/society/orchestrator.py` | End-to-end society lifecycle and event emission |
| Agent factory | `backend/society/agents.py` | Builds Agno `Agent` instances with model, memory, knowledge, and tools |
| Team factory | `backend/society/team.py` | Builds the Agno `Team` using `TeamMode.coordinate` |
| Workflow | `backend/society/workflow.py` | Defines the six governance checkpoints and task metrics computation |
| Session state | `backend/society/session.py` | Defines the shared JSON-serializable governance ledger |
| Events and memory replay | `backend/society/memory.py` | Append-only JSONL event store and memory reconstruction |
| Reputation | `backend/society/reputation.py` | Multi-dimensional reputation scores and replayable snapshots |
| Knowledge | `backend/society/knowledge/loaders.py` | Role-scoped filesystem knowledge loader |
| Agno DB | `backend/society/db.py` | Agno SQLite or configured DB factory |
| Schemas | `backend/society/schemas/*.py` | Pydantic contracts for tool outputs and metrics |
| Tools | `backend/society/tools/*.py` | Agno `@tool` functions used by agents or available to V3 workflows |

## Runtime Configuration

Configuration is loaded from `backend/.env` through `backend/config.py`.

Important settings:

| Setting | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER` | `qwen` | `qwen`, `cerebras`, or `openrouter` |
| `CEREBRAS_API_KEY` | empty | Enables Cerebras-backed agents |
| `CEREBRAS_MODEL` | `gemma-4-31b` | Cerebras model id |
| `QWEN_API_KEY` | empty | Enables Qwen/DashScope-backed agents |
| `QWEN_BASE_URL` | DashScope compatible endpoint | OpenAI-compatible Qwen endpoint |
| `QWEN_MODEL` | `qwen3.7-plus` | Qwen submission model id |
| `OPENROUTER_API_KEY` | empty | Enables OpenRouter-backed agents |
| `LLM_TIMEOUT_SECONDS` | `60` | Timeout for individual model-backed tool calls |
| `ALLOW_DETERMINISTIC_NO_KEY` | `false` | Explicitly enables local fallback tasks without a model credential |
| `FRONTEND_ORIGIN` | `http://localhost:5173` | CORS origin for the React app |
| `KNOWLEDGE_DIR` | `backend/society/knowledge/data` | Role knowledge root |
| `AGNO_SQLITE_FILE` | `backend/society/data/agno.sqlite` | Local Agno DB file |
| `AGNO_DB_URL` | empty | Optional DB URL override |
| `AGENT_PROFILES_ENABLED` | `true` | Injects stable work-behavior profiles into agent prompts |
| `SOCIAL_TOOLS_ENABLED` | `true` | Records typed stance, objection, endorsement, and trust artifacts |
| `SOCIAL_TRACE_ENABLED` | `true` | Emits social artifacts as user-visible timeline events |
| `CONTEXTUAL_TRUST_ENABLED` | `true` | Blends task-class trust into leadership scoring |
| `ROLE_SPECIFIC_TOOLS_ENABLED` | `true` | Gives each role a distinct primary tool and social support-tool bundle |

Without an API key, task submission fails honestly by default. Deterministic
no-key mode remains available only when `ALLOW_DETERMINISTIC_NO_KEY=true` is
explicitly set for local development or tests. Never present that mode as Qwen
submission evidence.

## Agents

The persistent society starts with four agents. Each agent has a stable
work-behavior profile in addition to its role and skills. Profiles are exposed
through `GET /agents` and injected into Agno instructions so agents make
different collaboration moves instead of merely using different names.

| ID | Name | Role | Work behavior | Primary capability tool |
|---|---|---|---|---|
| `architect` | Ada | Systems Architect | Structured, scope-aware, values boundaries and maintainability, blocks vague ownership or incoherent architecture, defers to Lin on implementation cost | `decompose_task` |
| `researcher` | Ibn | Research Analyst | Careful, evidence-driven, labels uncertainty, blocks unsupported claims, defers to Ada on system shape | `memory_lookup` |
| `builder` | Lin | Implementation Engineer | Direct, delivery-focused, values small shippable slices and validation commands, defers to Noor on quality gates | `implementation_plan` |
| `critic` | Noor | Adversarial Reviewer | Concise, skeptical, values correctness and bounded risk, blocks missing validation, defers to Ibn on evidence quality | `risk_assessment` |

Profile fields include values, communication style, risk tolerance, decision
bias, default blockers, deferral preferences, and the agent's known failure
mode. When `AGENT_PROFILES_ENABLED=true`, the instruction layer tells agents not
to agree for politeness and to support, challenge, defer, or block based on
their profile and task evidence. Deterministic fallback contributions also use
profile style, bias, and blockers so local demos still show distinct behavior.

The leader can spawn a temporary child specialist. The child role comes from
the spawn decision tool when LLM mode is enabled, or from the deterministic long
prompt fallback. The child receives derived skills such as focused delegation,
status reporting, evidence gathering, risk analysis, implementation planning,
or scope reduction depending on the requested specialist role. Temporary child
specialists also receive role-derived work-behavior profiles so complex tasks do
not degrade into generic agent behavior.

Child agents join the task team and can propose, vote, learn, and then dissolve
with the temporary team.

## Task Lifecycle

Submitting `POST /tasks` creates a `TaskRun`, emits `task_received`, and starts
`SocietyOrchestrator.run_task()`.

Current lifecycle:

1. **Form team**
   - Creates a task-scoped `Team` domain record.
   - Seeds `session_state` with prompt, roster, proposals, ballots, metrics,
     reputation snapshot, and child-agent slots.
   - Builds an Agno `Workflow`.
   - Builds an Agno `Team` in LLM mode.

2. **Run Workflow checkpoints**
   - Executes a six-step Agno `Workflow`.
   - Each step writes a checkpoint into shared session state.
   - The orchestrator emits `workflow_checkpoint` events plus
     `workflow_completed`.

3. **Run Agno Team coordination**
   - In LLM mode, the Agno Team runs one bounded coordination pass.
   - The app emits `agno_team_ran` with a concise coordination brief.
   - In deterministic mode this step is skipped.

4. **Elect leader**
   - LLM mode calls `elect_leader`.
   - Deterministic mode uses reputation and skill count.
   - Session state records `leader_id`.
   - Event: `leader_elected`.

5. **Decide child-agent spawn**
   - LLM mode calls `decide_spawn`.
   - Deterministic mode spawns only for longer prompts.
   - If spawning, `_do_spawn()` creates a child with role-derived skills.
   - Events: `no_spawn` or `child_agent_spawned`.

6. **Negotiate proposals**
   - Each member uses their role capability tool.
   - In LLM mode, the structured tool result becomes that member's proposal.
   - In deterministic mode, local fallback text becomes the proposal.
   - Event per member: `agent_negotiated`.

7. **Challenge and revise**
   - The critic challenges one target proposal.
   - The target proposal is revised before voting.
   - Session state records `challenges` and `revisions`.
   - Events: `proposal_challenged`, `proposal_revised`,
     `debate_round_completed`, `negotiation_closed`.

8. **Vote**
   - LLM mode calls `cast_vote` for each team member.
   - Deterministic mode uses a stable hash fallback.
   - Session state records ballots, tally, and winner.
   - Events: `vote_cast`, `ballots_tallied`, `solution_selected`.

9. **Monitor**
   - The critic reviews the winning candidate.
   - LLM mode calls `peer_review`.
   - Deterministic mode emits a simple fallback critique.
   - Event: `peer_monitor_report`.

10. **Learn and update reputation**
    - Each member records a memory event.
    - Multi-dimensional reputation scores update.
    - A reputation snapshot is emitted for replay after restart.
    - Events: `tool_call` for `memory_write`, `reputation_updated`,
      `learning_recorded`.

11. **Dissolve and complete**
    - Child agents are removed from the live pool.
    - Metrics are computed from session state.
    - Final answer is composed from leader and winning proposal.
    - Events: `team_dissolved`, `task_metrics`, `task_complete`.

## Workflow Checkpoints

`backend/society/workflow.py` defines these checkpoints:

1. `form_and_elect`
2. `spawn_or_delegate`
3. `debate`
4. `vote`
5. `monitor`
6. `learn_and_measure`

The checkpoints are Agno `Step` objects. They are intentionally small: the
workflow records phase progression, while the orchestrator maps each phase into
domain events, tool calls, and UI-visible timeline entries.

## Shared Session State

`initial_session_state()` creates the task ledger:

```json
{
  "task_prompt": "...",
  "roster": [],
  "phase": "forming",
  "leader_id": null,
  "proposals": {},
  "challenges": [],
  "revisions": {},
  "ballots": [],
  "tally": {},
  "winner_id": null,
  "critique": null,
  "spawn_decision": null,
  "child_agents": [],
  "subtasks": [],
  "evaluation_metrics": [],
  "reputations": {},
  "metrics": {
    "tool_calls": 0,
    "tool_calls_failed": 0,
    "governance_rounds": 0,
    "debate_rounds": 0,
    "memory_writes": 0
  }
}
```

The orchestrator passes this state into Agno Agent and Team calls. Tools can
write into `run_context.session_state`, and the orchestrator also writes the
authoritative event-mapped fields so deterministic and LLM runs behave
consistently.

## Tools

Tools are Agno `@tool` functions. They return validated JSON and, when Agno
provides a `RunContext`, also update shared session state.

### Governance Tools

File: `backend/society/tools/governance.py`

| Tool | Used by | Purpose | Output schema |
|---|---|---|---|
| `elect_leader` | election | Chooses the task leader from team member ids | `LeaderDecision` |
| `decide_spawn` | spawn decision | Decides whether a child specialist is needed and names the specialist role | `SpawnDecision` |
| `cast_vote` | voting | Casts a vote for a candidate proposal | `VoteDecision` |
| `peer_review` | monitoring | Reviews the winning candidate for assumptions, risks, and improvements | `CritiqueReport` |

The list fields in `peer_review` tolerate either real JSON arrays or newline
bullet strings because some providers send list-shaped arguments as text.

### Capability Tools

File: `backend/society/tools/capabilities.py`

| Tool | Agent | Purpose | Output schema |
|---|---|---|---|
| `decompose_task` | Ada / architect | Clarifies the objective, ordered steps, and delegation plan | `TaskDecomposition` |
| `memory_lookup` | Ibn / researcher | Retrieves relevant memories and a lesson to apply | `MemoryLookup` |
| `implementation_plan` | Lin / builder and fallback child specialists | Plans artifact, milestones, and acceptance checks | `ImplementationPlan` |
| `risk_assessment` | Noor / critic | Captures risks, mitigations, and a quality gate | `RiskAssessment` |
| `memory_write` | all agents | Records a durable lesson or collaboration fact | `MemoryWrite` |

Like governance tools, list fields tolerate both JSON arrays and bullet strings.

### Debate Tools

File: `backend/society/tools/debate.py`

| Tool | Purpose |
|---|---|
| `propose` | Writes an agent proposal into `session_state["proposals"]` |
| `challenge` | Writes a challenge into `session_state["challenges"]` |
| `revise` | Writes a revision into `session_state["revisions"]` |

These are available as V3 RunContext-native primitives. The current
orchestrator implements the active challenge/revision cycle directly so it can
emit stable timeline events and preserve deterministic parity.

### Voting Tools

File: `backend/society/tools/voting.py`

| Tool | Purpose |
|---|---|
| `cast_ballot` | Records a ballot in shared session state |
| `tally_ballots` | Tallies ballots and writes the winner |

These are reusable V3 primitives. The active runtime currently uses
`governance.cast_vote` because it matches the existing `VoteDecision`
extraction path and UI event payloads.

### Delegation Tools

File: `backend/society/tools/delegation.py`

| Tool | Purpose |
|---|---|
| `assign_subtask` | Adds an assigned subtask to session state |
| `report_subtask` | Marks a subtask completed and stores blockers |

These support future child-agent delegation workflows. The current runtime
spawns child agents and includes them in negotiation/voting, but does not yet
drive a full subtask assignment/report loop from the UI.

### Evaluation Tools

File: `backend/society/tools/evaluation.py`

| Tool | Purpose |
|---|---|
| `record_metric` | Appends an evaluation metric to `session_state["evaluation_metrics"]` |

The current runtime also computes system metrics automatically at task end with
`compute_task_metrics()`.

## Collaboration Patterns

### Temporary Task Society

Every prompt creates a task-scoped team. The team exists only for the task and
is dissolved afterward. This keeps collaboration state local to the task while
persistent identities, memory, and reputation carry forward.

### Social Trace

The runtime records typed social artifacts alongside existing governance events:

| Event | Meaning |
|---|---|
| `agent_position_stated` | An agent publicly supports, opposes, defers, blocks, or remains uncertain during a phase |
| `agent_objection_registered` | An agent records a severity-tagged objection and its resolution condition |
| `agent_endorsed_peer` | An agent defers to or endorses another agent for a domain |
| `agent_changed_mind` | An agent revises a position after another agent's challenge or new evidence |
| `private_note_published` | An agent chooses to share a private working note with the team |
| `agent_tool_bundle_selected` | The runtime records the role-specific tool bundle selected for an agent |
| `agent_help_requested` | An assigned agent asks another agent, usually the leader, to keep work unblocked |
| `agent_deferred_ownership` | An agent explicitly lets another agent coordinate or own a decision |
| `agent_joined_coalition` | An agent publicly backs another agent's proposal during selection |
| `trust_updated` | The society records a contextual trust movement after collaboration |

These events are a mix of tool-mediated stances, derived governance signals,
and bounded outcome signals. They let the product show human-like work behavior
without adding unbounded extra model calls. The frontend renders them in the
workflow timeline as the first visible social trace layer.

### Role-Specific Tools

Agents now receive product-visible tool bundles instead of a single shared
capability surface:

| Role | Primary Tool | Support Tools |
|---|---|---|
| Architect | `decompose_task` | `state_position`, `endorse_agent`, `record_private_note` |
| Researcher | `memory_lookup` | `record_private_note`, `publish_private_note`, `state_position` |
| Builder | `implementation_plan` | `state_position`, `change_mind`, `record_private_note` |
| Critic | `risk_assessment` | `register_objection`, `evaluate_peer`, `publish_private_note` |

Spawned specialists are mapped by role and skills, so a temporary evidence
specialist behaves like a researcher while a review specialist behaves like a
critic. The timeline emits `agent_tool_bundle_selected` when negotiation uses a
bundle, making capability differences inspectable in the product.

### Group Dynamics

The society also derives lightweight collaboration actions from existing
workflow decisions:

- leader election creates ownership deferrals from non-leaders to the elected
  coordinator,
- subtask assignment creates help requests from assignees back to the leader,
- voting creates coalition signals around the proposals agents support.

These actions are stored in `public_room.collaboration_actions` and rendered as
timeline events. They make the society read less like isolated tool calls and
more like a working group that asks for help, hands off authority, and forms
temporary support around ideas.

The frontend also renders a compact Society Behavior summary above the raw
timeline. It aggregates objections, deferrals, help requests, mind changes,
coalition joins, shared private notes, and trust movement, then shows the latest
social moments as a scan-friendly digest. Raw events remain available below it
for replay and debugging.

After the working brief, each agent records a task-local private note. Notes stay
private in `private_agent_state` unless the agent marks them publishable, in
which case the runtime emits `private_note_published` and adds the note to the
public room.

Learning now stores compact social trace counts with each lesson, including
positions, objections, endorsements, mind changes, private notes, published
private notes, collaboration actions, and trust updates. This gives future runs a
durable signal for persuasion, useful dissent, selective disclosure, and group
dynamics instead of only remembering the final selected proposal.

Learning also extracts per-agent social lessons from the typed trace:

- agents that raise blockers remember the blocker and its resolution condition,
- agents that change their mind remember what challenge changed their stance,
- agents that persuaded a teammate remember to challenge with a concrete
  revision path,
- agents that ask for help, defer ownership, or join a coalition remember the
  reusable behavioral rule for next time,
- agents that receive trust or deferral remember the domain where teammates
  relied on them.

The memory panel parses these JSON lessons and shows the durable social rules
separately from the raw trace counts.

### Elected Leadership

Leadership is task-specific. In LLM mode, the `elect_leader` tool selects the
leader based on prompt, roster, skills, reputation scores, and contextual trust
for the current task class. In deterministic mode, the highest contextual trust
and skill-weighted score wins.

When `CONTEXTUAL_TRUST_ENABLED=true`, leadership scoring blends global
reputation with task-class-specific trust for `research`, `planning`,
`implementation`, and `review` tasks. Scores are clamped, decay slightly toward
baseline after each run, and are persisted inside the replayable reputation
snapshot. Endorsements, task outcomes, validation signals, blockers, and role
fit can move trust up or down.

### Role-Specific Contribution

Agents do not all answer the same way. Each persistent office has a primary
capability tool:

- architect decomposes
- researcher recalls memory
- builder plans implementation
- critic assesses risk

This creates structured disagreement and avoids a flat "four assistants say the
same thing" pattern.

### Challenge Before Vote

Before voting, the critic challenges one proposal and the target proposal is
revised. The system records both the challenge and revision. This gives the
vote a concrete debate history instead of selecting from untested first drafts.

### Voting and Monitoring

Each member votes independently. The winning proposal is then reviewed by the
critic. This separates selection from quality control.

### Dynamic Child Agents

The leader can spawn one child specialist when the task appears complex. In LLM
mode, the child role comes from the `decide_spawn` tool. In deterministic mode,
longer prompts spawn a scope-reduction specialist. The child participates in
proposal and voting phases, then dissolves with the team.

### Learning Loop

At the end of every task:

- each agent records a memory event
- reputation and task-class trust scores update
- reputation is persisted as a replayable event
- task metrics are emitted

This makes later tasks sensitive to past collaboration without requiring a
production database.

## Persistence

There are two persistence layers:

1. **JSONL event log**
   - Path: `backend/society/data/events.jsonl`
   - Stores timeline events, tool calls, memory-write events, task summaries,
     metrics, and reputation snapshots.
   - Powers replay in the UI and restart reconstruction.

2. **Agno DB**
   - Default path: `backend/society/data/agno.sqlite`
   - Created by `backend/society/db.py`.
   - Passed into Agno Agents and Workflows for native session/memory support.

The `.gitignore` excludes `backend/society/data/`, so local run history and DB
files stay out of source control.

## Metrics

Task metrics are computed at completion from session state:

| Metric | Meaning |
|---|---|
| `total_duration_seconds` | End-to-end task duration |
| `governance_rounds` | Leader-election/governance cycles |
| `debate_rounds` | Debate rounds run before voting |
| `tool_calls_total` | Count of emitted tool calls |
| `tool_calls_failed` | Failed tool calls |
| `proposals_count` | Number of proposals in session state |
| `challenges_count` | Number of recorded challenges |
| `revisions_count` | Number of recorded revisions |
| `vote_margin` | Winning vote share |
| `critique_risk_count` | Risks in critic review |
| `child_agents_spawned` | Child specialists created |
| `memory_writes` | Memory writes emitted |
| `answer_length` | Final answer length |

`GET /metrics` aggregates live metrics and replayed `task_metrics` events from
the JSONL log.

## API Surface

| Endpoint | Purpose |
|---|---|
| `GET /health` | Provider, model, and LLM-enabled status |
| `GET /agents` | Current live agent pool |
| `GET /agents/{agent_id}/memory` | Agent memory from live state plus JSONL replay |
| `GET /teams` | In-memory task teams |
| `GET /metrics` | Aggregate task metrics |
| `POST /tasks` | Submit a new task |
| `GET /tasks` | List task summaries from memory and replay |
| `GET /tasks/{task_id}` | Fetch one task summary |
| `GET /tasks/{task_id}/events` | Fetch persisted task events |
| `GET /tasks/{task_id}/stream` | SSE stream for live UI timeline |

## UI Behavior

The React control room submits tasks and listens to the SSE stream. It renders:

- current provider/model readiness
- task timeline events
- tool call payloads
- final solution
- agent pool cards
- replayed task summaries
- per-agent memory

The frontend intentionally consumes stable event names instead of inspecting raw
Agno internals.

## Behavior Preflight

Run this local gate after changing society behavior:

```bash
python -m society.behavior_preflight
```

It verifies the product-level human-behavior contract without spending model
tokens. The preflight exercises the typed trace recorders for positions,
objections, endorsements, mind changes, published private notes, help requests,
ownership deferrals, coalition joins, trust updates, and reusable social
learning. It fails if required event names disappear, public-room counts drift,
or the social lesson extractor stops producing behavior-specific lessons.

## Deterministic Mode vs LLM Mode

| Concern | Deterministic mode | LLM mode |
|---|---|---|
| Team coordination | Agno Team skipped | Agno Team bounded coordination pass |
| Leader election | Reputation/skills heuristic | `elect_leader` tool |
| Spawn decision | Prompt length heuristic | `decide_spawn` tool |
| Role contribution | Local fallback contribution | Role capability tool result |
| Challenge/revision | Local deterministic challenge | Structured state-level challenge/revision |
| Voting | Hash fallback | `cast_vote` tool |
| Review | Local fallback critique | `peer_review` tool |
| Learning | Local memory-write events | Structured memory-write events |

Both modes emit the same broad event lifecycle so the UI and replay paths stay
stable.

## Validation Commands

Run these from the repo root unless noted:

```powershell
python -m compileall backend
cd backend; python -m society.preflight
cd frontend; npm run build
```

Useful live checks:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/metrics
```

The latest validated live run completed through:

- Agno Team coordination
- six Workflow checkpoints
- native tool calls
- child-agent spawn
- negotiation
- challenge and revision
- voting
- peer review
- learning and reputation update
- task metrics
- task completion

## Current Boundaries

The implementation is V3-complete for the hackathon demo surface, but these are
the current boundaries:

- Role knowledge is filesystem-backed and lazy; empty knowledge folders are
  treated as no knowledge rather than errors.
- Child agents inherit knowledge by role fallback when no exact specialist
  folder exists.
- Debate, voting, delegation, and evaluation tool modules include reusable
  RunContext primitives; not all of them are the active orchestrator path yet.
- JSONL and local SQLite are appropriate for local/demo persistence, not a
  distributed production deployment.
- The frontend shows event payloads and final output, but does not yet provide a
  dedicated metrics dashboard.
