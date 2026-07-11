# Agent Upgrade Implementation Plan

## Purpose

This plan upgrades Qwendom from a structured task-governance pipeline into a
more believable expert society. The product goal is not to make agents chat
more. The goal is to make users see a group with memory, work styles, dissent,
trust, changing leadership, and visible learning.

The explicit product goal is:

> Make Qwendom mimic human work behavior: agents should behave like a small
> group of specialists who remember prior work, form opinions, disagree,
> defer, persuade each other, change their minds, build or lose trust, and
> carry unresolved dissent forward when they proceed.

This should be implemented as observable product behavior, not as theatrical
human impersonation. Qwendom should remain transparent that the participants are
AI agents, while making the society dynamics feel closer to a real working team.

The implementation should preserve Qwendom's current strengths:

- traceable event stream,
- bounded workflow execution,
- typed governance artifacts,
- replayable task history,
- Agno-native agents, teams, tools, workflow, memory, and knowledge.

## Product Target

Users should be able to understand the society as a working group:

- who joined the task and why,
- what each agent believed at the start,
- who objected and how severe the objection was,
- who deferred to or endorsed another agent,
- who changed position,
- what unresolved dissent remained,
- why the leader was chosen,
- what the agents learned,
- how trust changed after the run.

The user-facing standard is: after watching a task run, a user should be able
to describe the society in human collaboration terms:

- "Ada led because the task needed architecture judgment."
- "Noor objected, and the team carried that risk into the final answer."
- "Lin changed position after the validation concern."
- "Ibn was trusted more on evidence-heavy tasks."
- "The group proceeded, but not with full consensus."

The first product milestone is:

> Agent identity plus visible dissent and trust signals.

This milestone should make the society feel more human-like without turning the
runtime into an unbounded social simulation.

## Current Baseline

The current runtime already has these useful primitives:

- `backend/society/orchestrator.py`
  - task lifecycle,
  - team formation,
  - pre-execution conversation,
  - readiness voting,
  - working brief,
  - leader election,
  - child agent spawning,
  - proposal/debate/revision/voting,
  - delegation,
  - monitoring,
  - validation,
  - learning and metrics.
- `backend/society/session.py`
  - shared session state for public governance artifacts.
- `backend/society/agents.py`
  - Agno agent construction,
  - memory,
  - knowledge loading,
  - session state injection.
- `backend/society/team.py`
  - Agno team construction.
- `backend/society/workflow.py`
  - Agno workflow checkpoints and routing metadata.
- `backend/society/reputation.py`
  - multi-dimensional reputation.
- `backend/society/tools/*`
  - typed RunContext-aware governance tools.
- `backend/society/knowledge/data/*`
  - role-specific doctrine and rubrics.

The main gap is product behavior. Agents have roles, but not rich work styles.
They can discuss, but the conversation mostly serves readiness. They can vote,
but dissent is not a durable first-class product object. Reputation exists, but
it mostly rewards participation and winners rather than modeling contextual
trust.

## Agno Primitives To Activate

Use these Agno primitives as implementation building blocks:

- `Agent`
  - stronger `role`, `instructions`, memory, session IDs, and role-specific
    knowledge.
- `Team`
  - use different team modes as product meeting formats.
- `Workflow`
  - make social rituals explicit steps, not ad hoc prompt text.
- `RunContext.session_state`
  - mutate society state through typed tools.
- Agent memory and session summaries
  - preserve what agents learn across runs.
- Knowledge and `KnowledgeTools`
  - give each role an office library.
- Structured outputs and strict tool schemas
  - make stance, objection, endorsement, and trust changes typed artifacts.

## Design Decisions

### Decision 1: Product Mode

Choose this mode for the first implementation:

> Task-solving with social signals.

Do not build a fully autonomous social simulation yet. The orchestrator remains
the governor, and Agno primitives provide agent identity, memory, knowledge,
typed social artifacts, and meeting formats.

### Decision 2: State Visibility

Split society state into three conceptual layers:

- public room state,
- per-agent private state,
- durable attributed memory.

The first implementation can store all three inside `session_state`, but the
shape must distinguish them clearly. Do not keep treating every state write as
shared public context.

Because the current agent factory can inject the full `session_state` into
model context, private state must not rely on naming alone. The implementation
must add an explicit context projection layer before private state is enabled.
An agent should only receive:

- public room state,
- its own private notes,
- durable memories addressed to that agent,
- redacted summaries of other private state when explicitly published.

### Decision 3: Dissent Policy

Do not equate majority readiness with consensus.

The society may proceed with dissent, but unresolved objections must remain
visible in the working brief, final answer, and task timeline.

### Decision 4: Reputation Scope

Reputation should become contextual trust, not one global social score.

Leadership should use task-class-specific trust when possible. Global
reputation can remain as a compatibility summary.

### Decision 5: Source Of Truth

Social artifacts should not replace existing governance artifacts.

Use this precedence when artifacts disagree:

1. critical `ObjectionRecord` blocks or caveats execution,
2. `WorkingBrief` summarizes the agreed operating contract,
3. `AgentPosition` records individual stance,
4. readiness ballots decide whether the run may start,
5. debate proposals and revisions decide solution candidates,
6. voting decides the selected candidate.

This means a task can be ready to proceed while still carrying non-critical
dissent, and a winning proposal can still be caveated by unresolved objections.
The final answer should reflect that instead of pretending all signals agree.

### Decision 6: Replay Compatibility

Old JSONL event logs are valid history.

Do not backfill invented social artifacts for old runs. Render old runs as
partial social traces with a clear "legacy run" marker when they lack typed
position, objection, endorsement, or trust events.

## Phase 1: Agent Profiles

### Goal

Give agents stable work styles that users can recognize across runs.

### Files

- `backend/main.py`
- `backend/society/models.py`
- `backend/society/orchestrator.py`
- `backend/society/agents.py`
- `backend/society/session.py`
- `backend/society/knowledge/data/*`
- `docs/SOCIETY_RUNTIME.md`

### Data Model

Extend `SocietyAgent` with additive fields:

```python
class AgentProfile(BaseModel):
    values: list[str] = Field(default_factory=list)
    communication_style: str = ""
    risk_tolerance: Literal["low", "medium", "high"] = "medium"
    decision_bias: str = ""
    default_blockers: list[str] = Field(default_factory=list)
    defers_to: dict[str, list[str]] = Field(default_factory=dict)
    failure_mode: str = ""
```

Then add:

```python
profile: AgentProfile = Field(default_factory=AgentProfile)
```

### Seed Profiles

Ada, Systems Architect:

- values: boundaries, decomposability, maintainability,
- style: structured and scope-aware,
- risk tolerance: medium,
- blocks on incoherent architecture or vague ownership,
- defers to Lin on implementation cost,
- failure mode: can over-design.

Ibn, Research Analyst:

- values: evidence, uncertainty labeling, context,
- style: careful and question-driven,
- risk tolerance: low,
- blocks on unsupported claims,
- defers to Ada on system shape,
- failure mode: can slow execution by asking for more evidence.

Lin, Implementation Engineer:

- values: shipping, small slices, concrete acceptance checks,
- style: pragmatic and delivery-focused,
- risk tolerance: medium-high,
- blocks on non-executable plans,
- defers to Noor on validation risk,
- failure mode: can under-investigate edge cases.

Noor, Adversarial Reviewer:

- values: correctness, safety, adversarial testing,
- style: concise and skeptical,
- risk tolerance: low,
- blocks on unbounded risk or missing validation,
- defers to Ibn on evidence quality,
- failure mode: can over-block.

### Implementation Notes

In `build_agno_agent()`, convert the profile into instructions:

- what this agent optimizes for,
- what this agent tends to miss,
- when to defer,
- when to block,
- how to update its stance.

Keep the language product-useful. Avoid theatrical personality text.

### Acceptance Criteria

- `/agents` includes profile fields.
- Every seeded agent has a documented profile.
- Agent prompts include the profile values.
- Existing task execution still works.
- Agent behavior remains role-distinct even when all agents use the same model.

## Phase 2: Social Artifact Schemas

### Goal

Make human-like behavior visible and typed.

### Files

- `backend/society/schemas/social.py` (new)
- `backend/society/tools/social.py` (new)
- `backend/society/tools/__init__.py`
- `backend/society/session.py`
- `backend/society/orchestrator.py`

### New Schemas

Create typed artifacts:

```python
class AgentPosition(BaseModel):
    agent_id: str
    phase: str
    stance: Literal["support", "oppose", "uncertain", "defer", "block"]
    target: str | None = None
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)
    conditions: list[str] = Field(default_factory=list)
```

```python
class ObjectionRecord(BaseModel):
    agent_id: str
    target: str | None = None
    severity: Literal["low", "medium", "high", "critical"]
    objection: str
    resolution_condition: str
    blocks_execution: bool = False
```

```python
class EndorsementRecord(BaseModel):
    agent_id: str
    endorsed_agent_id: str
    domain: str
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)
```

```python
class MindChangeRecord(BaseModel):
    agent_id: str
    previous_stance: str
    new_stance: str
    trigger_agent_id: str | None = None
    reason: str
```

```python
class PrivateNote(BaseModel):
    agent_id: str
    phase: str
    note: str
    may_publish: bool = False
```

```python
class TrustUpdate(BaseModel):
    evaluator_id: str
    target_agent_id: str
    domain: str
    delta: float = Field(ge=-1.0, le=1.0)
    reason: str
```

### Session State Additions

Add:

```python
"public_room": {
    "positions": [],
    "objections": [],
    "endorsements": [],
    "mind_changes": [],
},
"private_agent_state": {},
"trust_updates": [],
```

Keep existing fields for compatibility. Do not remove `goal_discussions`,
`readiness_ballots`, `proposals`, `challenges`, or `revisions`.

### New Tools

Implement:

- `state_position`
- `register_objection`
- `endorse_agent`
- `change_mind`
- `record_private_note`
- `publish_private_note`
- `evaluate_peer`

Each tool must:

- accept `RunContext`,
- validate through Pydantic,
- write to the correct session state list,
- return JSON,
- increment tool metrics,
- be easy to render in the frontend event timeline.

### Acceptance Criteria

- Agents can record a stance without writing prose.
- Agents can object without blocking by default.
- Critical objections are explicit.
- Endorsements and deferrals are visible.
- Mind changes are persisted as their own event.
- Private notes are not automatically inserted into public context.
- When social artifacts and existing governance artifacts disagree, the source
  of truth rules in this document determine rendering and final-answer caveats.

## Phase 2.5: Context Projection

### Goal

Make private state real by preventing accidental leakage into every agent
prompt.

### Files

- `backend/society/agents.py`
- `backend/society/orchestrator.py`
- `backend/society/session.py`
- `backend/society/schemas/social.py`

### Projection Contract

Add a helper such as:

```python
def session_context_for_agent(
    session_state: dict[str, Any],
    agent_id: str,
    phase: str,
) -> dict[str, Any]:
    ...
```

The projection should include:

- `task_prompt`,
- `phase`,
- roster with public profile fields,
- public room state,
- working brief,
- public objections,
- public endorsements,
- the requesting agent's private notes,
- published private notes from other agents,
- high-level metrics required for the phase.

The projection must exclude:

- other agents' unpublished private notes,
- raw private-agent state for other agents,
- hidden trust calculations not meant for the current phase,
- any state marked private unless `may_publish` is true.

### Implementation Notes

Do not enable `PRIVATE_AGENT_STATE_ENABLED` until this projection exists.

When building an Agno agent for a task phase, pass the projected state rather
than the full session state. Tools can still mutate canonical `session_state`
through `RunContext`, but model context must be filtered.

### Acceptance Criteria

- Unit-level checks prove one agent cannot see another agent's unpublished
  private note.
- Public notes and published private notes remain visible.
- Existing public governance context still reaches agents.
- Disabling the feature falls back to current shared state behavior.

## Phase 3: Instruction And Tool Access Matrix

### Goal

Make agents feel different because they can do different things.

### Files

- `backend/society/agents.py`
- `backend/society/orchestrator.py`
- `backend/society/tools/__init__.py`
- `backend/society/tools/social.py`
- `docs/SOCIETY_RUNTIME.md`

### Tool Access

Ada:

- `decompose_task`
- `state_position`
- `endorse_agent`
- `register_objection`
- future: `record_architecture_decision`

Ibn:

- `memory_lookup`
- `state_position`
- `request_clarification`
- `claim_confidence`
- `record_private_note`
- future: `evidence_check`

Lin:

- `implementation_plan`
- `state_position`
- `change_mind`
- `record_private_note`
- future: `estimate_effort`

Noor:

- `risk_assessment`
- `register_objection`
- `change_mind`
- `evaluate_peer`
- future: `validation_gate`

Shared:

- `memory_write`
- limited `publish_private_note`

### Implementation Notes

Replace one-size-fits-all tool lists with a role-aware tool registry:

```python
def tools_for_agent(agent: SocietyAgent, phase: str) -> list[Any]:
    ...
```

Start with phase-insensitive mapping. Add phase-sensitive access after the
artifact flow is stable.

### Acceptance Criteria

- Different roles receive different tool lists.
- Tool access is documented.
- Agents cannot all perform the exact same social actions by default.
- Existing governance tools still work.

## Phase 4: Meeting Formats

### Goal

Use Agno `Team` and `Workflow` to make social formats product-visible.

### Files

- `backend/society/team.py`
- `backend/society/workflow.py`
- `backend/society/orchestrator.py`
- `backend/config.py`
- `docs/SOCIETY_RUNTIME.md`

### Meeting Formats

Add a `meeting_format` field to routing metadata:

- `broadcast_review`
  - all agents independently assess the same prompt,
  - useful for early framing and broad review.
- `coordination`
  - leader delegates and synthesizes,
  - useful for implementation planning.
- `debate`
  - agents respond to each other's positions,
  - useful when there are conflicts or high uncertainty.
- `review_board`
  - agents approve, object, or block,
  - useful before final answer.

### Routing Policy

Use task class and uncertainty:

- research tasks: `broadcast_review` then `coordination`,
- implementation tasks: `coordination` then `review_board`,
- review tasks: `broadcast_review` then `debate`,
- planning tasks: `broadcast_review` then `coordination`.

### Implementation Notes

Do not fully replace the orchestrator. The orchestrator should call explicit
meeting format helpers:

- `_run_broadcast_review()`
- `_run_coordination_meeting()`
- `_run_debate_meeting()`
- `_run_review_board()`

Each helper should emit events and register social artifacts.

### Acceptance Criteria

- Every task records which meeting format was used.
- Broadcast review produces independent positions.
- Debate produces objections and possible mind changes.
- Review board produces final objections and confidence.
- Meeting format is visible in events and frontend.

## Phase 5: Working Brief With Dissent

### Goal

Stop collapsing discussion into fake consensus.

### Files

- `backend/society/schemas/conversation.py`
- `backend/society/orchestrator.py`
- `frontend/src/App.tsx`
- `frontend/src/styles.css`
- `docs/PRE_EXECUTION_CONVERSATION_WORKFLOW.md`

### Schema Upgrade

Extend `WorkingBrief`:

```python
agreed_points: list[str] = Field(default_factory=list)
contested_points: list[str] = Field(default_factory=list)
unresolved_assumptions: list[str] = Field(default_factory=list)
objectors: list[str] = Field(default_factory=list)
proceeding_with_dissent: bool = False
```

### Behavior

When readiness passes:

- preserve unresolved objections,
- distinguish low-risk dissent from blockers,
- record who objected,
- include dissent in leader election context,
- carry dissent into final validation.

### Acceptance Criteria

- The working brief can show disagreement.
- Majority readiness does not erase objections.
- Critical objections still block execution.
- Low/medium objections can remain visible while the task proceeds.
- Final answer includes caveats when proceeding with dissent.

## Phase 6: Contextual Trust

### Goal

Replace flat reputation behavior with contextual trust.

### Files

- `backend/society/reputation.py`
- `backend/society/orchestrator.py`
- `backend/society/schemas/social.py`
- `backend/society/tools/social.py`
- `backend/society/workflow.py`
- `docs/SOCIETY_RUNTIME.md`

### Trust Dimensions

Keep existing dimensions:

- leadership,
- delivery,
- critique,
- collaboration.

Add task-class trust:

```python
task_class_scores: dict[str, dict[str, float]]
```

Where task classes are:

- research,
- planning,
- implementation,
- review.

Add social signals:

- endorsements received,
- objections upheld,
- objections ignored and later validated,
- blocked subtasks,
- mind changes caused,
- failed confidence claims.

### Decay And Negative Signals

Add:

- small decay toward baseline after each run,
- positive updates for useful endorsements and accurate objections,
- negative updates for over-blocking, missed critical risks, and failed delivery,
- task-class-specific updates.

### Storage Contract

Use bounded scores.

```python
MIN_TRUST = 0.0
BASELINE_TRUST = 1.0
MAX_TRUST = 2.0
DECAY_RATE = 0.02
```

Each trust dimension and task-class score should be clamped to this range after
updates. Decay moves scores toward `BASELINE_TRUST`, not toward zero.

Snapshot shape:

```json
{
  "leadership": 1.0,
  "delivery": 1.0,
  "critique": 1.0,
  "collaboration": 1.0,
  "task_class_scores": {
    "research": {"delivery": 1.0, "critique": 1.0},
    "planning": {"leadership": 1.0},
    "implementation": {"delivery": 1.0},
    "review": {"critique": 1.0}
  },
  "social_signals": {
    "endorsements_received": 0,
    "accurate_objections": 0,
    "over_blocks": 0,
    "mind_changes_caused": 0
  },
  "history": []
}
```

Conflicting signals compose additively within one task, then clamp once at the
end. For example, a useful objection can increase critique trust while a blocked
subtask decreases delivery trust in the same run.

### Leadership Selection

Leader election should include:

- task class,
- contextual trust,
- relevant endorsements,
- current objections,
- role fit.

### Acceptance Criteria

- Trust can go down.
- Trust is task-class-aware.
- Trust scores are clamped and decay toward baseline.
- Old reputation snapshots still load with default task-class scores.
- Leadership reasons mention contextual fit.
- Reputation updates are emitted as events.

## Phase 7: Knowledge As Office Libraries

### Goal

Make agents competent in differentiated ways.

### Files

- `backend/society/knowledge/data/*`
- `backend/society/knowledge/loaders.py`
- `backend/society/agents.py`
- `docs/SOCIETY_RUNTIME.md`

### Office Libraries

Ada:

- architecture doctrine,
- system boundary heuristics,
- decomposition patterns,
- tradeoff rubric.

Ibn:

- evidence rubric,
- uncertainty taxonomy,
- source quality checklist,
- assumption tracking guide.

Lin:

- implementation slice patterns,
- acceptance checklist patterns,
- delivery risk heuristics,
- testing strategy guide.

Noor:

- adversarial review rubric,
- validation gates,
- risk severity taxonomy,
- dissent escalation policy.

### Implementation Notes

The repo already has role knowledge folders. Expand them before introducing
external retrieval. Local knowledge is enough for this milestone.

### Acceptance Criteria

- Each role has at least one doctrine and one rubric document.
- Agent prompts include knowledge when available.
- Agents cite office doctrine in social artifacts where useful.
- Missing knowledge folders do not break local runs.

## Phase 8: Frontend Social Trace

### Goal

Make the society behavior legible to users.

### Files

- `frontend/src/App.tsx`
- `frontend/src/api.ts`
- `frontend/src/styles.css`
- `backend/main.py` only if API shape needs additive endpoints.

### UI Concepts

Add a compact social trace:

- leader and why,
- meeting format,
- agent stance chips,
- unresolved objections,
- endorsements/deferrals,
- mind changes,
- trust movement,
- proceeding with dissent marker.

Do not hide the raw event stream. Add the social trace as a summary layer over
events.

### Event Types

Add or render:

- `agent_position_stated`
- `agent_objection_registered`
- `agent_endorsed_peer`
- `agent_changed_mind`
- `private_note_published`
- `peer_evaluated`
- `trust_updated`
- `meeting_format_selected`
- `dissent_carried_forward`

### Acceptance Criteria

- Users can tell who supported or opposed the plan.
- Users can see when an agent changed position.
- Users can see whether execution proceeded with dissent.
- Users can see why leadership changed.
- Replay renders social trace for completed tasks.
- Legacy runs without social events render as partial traces, not broken or
  backfilled timelines.

## Phase 8.5: Replay And Migration Compatibility

### Goal

Keep old JSONL history useful while introducing new social artifacts.

### Files

- `backend/society/memory.py`
- `backend/society/orchestrator.py`
- `frontend/src/App.tsx`
- `frontend/src/api.ts`
- `frontend/src/styles.css`

### Behavior

Do not mutate old event logs. Instead:

- detect whether a task has social artifact events,
- expose a derived `social_trace_status` value:
  - `legacy_absent`,
  - `partial`,
  - `complete`,
- render old tasks with a compact legacy marker,
- keep existing task summaries and metrics reconstruction unchanged,
- allow new runs to include social trace events incrementally.

### Acceptance Criteria

- Old tasks still appear in `/tasks`.
- Old task event streams still render.
- UI does not invent positions or objections for old runs.
- New social events are additive.
- Reputation replay tolerates both old and new snapshot shapes.

## Phase 9: Validation And Evaluation

### Goal

Prove the upgrade improves product behavior without breaking the runtime.

### Existing Validation

Run:

```powershell
python -m compileall backend
cd backend; python -m society.preflight
cd frontend; npm run build
```

### Behavioral Fixtures

Create test prompts:

1. Clear implementation task
   - expected: Lin leads or strongly influences,
   - low dissent,
   - concrete acceptance checks.

2. Ambiguous product task
   - expected: Ibn requests clarification or records uncertainty,
   - readiness may proceed with assumptions.

3. Risky architecture task
   - expected: Ada and Noor object or constrain scope,
   - final brief carries risk caveats.

4. Evidence-heavy task
   - expected: Ibn gains trust,
   - unsupported claims are challenged.

5. Previously failed pattern
   - expected: memory affects positions or readiness.

### Product Metrics

Track:

- percentage of tasks with explicit positions,
- objections per task by severity,
- mind changes per task,
- tasks proceeding with dissent,
- leadership distribution by task class,
- trust movement by agent,
- validation failures after unresolved dissent,
- average added latency.

### Acceptance Criteria

- Social artifacts appear in every LLM-backed run.
- At least one fixture produces visible dissent.
- At least one fixture produces a mind change.
- At least one fixture changes leadership based on task class.
- Runtime remains bounded.
- UI remains readable.

## Feature Flags

Add flags in `backend/config.py`:

```python
agent_profiles_enabled: bool = Field(default=True, alias="AGENT_PROFILES_ENABLED")
social_tools_enabled: bool = Field(default=False, alias="SOCIAL_TOOLS_ENABLED")
private_agent_state_enabled: bool = Field(default=False, alias="PRIVATE_AGENT_STATE_ENABLED")
meeting_formats_enabled: bool = Field(default=False, alias="MEETING_FORMATS_ENABLED")
contextual_trust_enabled: bool = Field(default=False, alias="CONTEXTUAL_TRUST_ENABLED")
social_trace_enabled: bool = Field(default=False, alias="SOCIAL_TRACE_ENABLED")
```

Rollout order:

1. enable profiles,
2. enable social tools,
3. enable social trace,
4. enable meeting formats,
5. enable private state,
6. enable contextual trust.

## Rollback Strategy

Rollback must preserve old task execution.

If social tools fail:

- disable `SOCIAL_TOOLS_ENABLED`,
- fall back to existing `goal_discussions`, `proposals`, and `challenges`.

If private state causes bad context:

- disable `PRIVATE_AGENT_STATE_ENABLED`,
- keep private notes stored but do not inject them into prompts.

If contextual trust destabilizes leadership:

- disable `CONTEXTUAL_TRUST_ENABLED`,
- return to existing election scoring.

If social trace renders poorly:

- disable `SOCIAL_TRACE_ENABLED`,
- keep raw event stream.

## Implementation Order

### Sprint 1: Profiles And Instructions

1. Add `AgentProfile`.
2. Seed profiles for Ada, Ibn, Lin, Noor.
3. Inject profiles into Agno agent instructions.
4. Document profiles.
5. Validate backend compile and one LLM-backed run.

### Sprint 2: Social Tools

1. Add social schemas.
2. Add social tools.
3. Add session state fields.
4. Emit social events.
5. Wire role-specific tool access.
6. Validate one run with positions and objections.

### Sprint 3: Working Brief With Dissent

1. Extend `WorkingBrief`.
2. Populate agreed and contested points.
3. Carry objections into leader election.
4. Carry dissent into final answer.
5. Validate critical blockers still block.

### Sprint 4: Social Trace UI

1. Render stance chips.
2. Render objections.
3. Render mind changes.
4. Render trust movement.
5. Preserve raw event timeline.

### Sprint 5: Meeting Formats

1. Add meeting format routing.
2. Implement broadcast review.
3. Implement review board.
4. Keep debate and coordination bounded.
5. Validate latency.

### Sprint 6: Contextual Trust

1. Add task-class trust.
2. Add decay and negative signals.
3. Use trust in leader election.
4. Emit trust update events.
5. Validate leadership diversity.

### Sprint 7: Office Libraries

1. Expand role doctrine.
2. Expand role rubrics.
3. Add knowledge usage expectations.
4. Validate agents reference role-specific standards.

## Non-Goals

- Do not create an unbounded chatroom.
- Do not let agents talk indefinitely.
- Do not make all private thoughts visible by default.
- Do not hide unresolved dissent.
- Do not replace the orchestrator in the first pass.
- Do not introduce external web retrieval as part of this milestone.
- Do not make reputation punitive before trust updates are observable and
  reversible.

## Done Definition

This upgrade is complete when:

1. every agent has a stable profile,
2. agents use different tool sets,
3. every task records typed positions,
4. objections are severity-tagged,
5. agents can endorse or defer to each other,
6. agents can change their mind,
7. working briefs preserve disagreement,
8. leadership can vary by task class and trust,
9. users can inspect a social trace in the UI,
10. memories and knowledge influence future behavior,
11. the backend compile, preflight, and frontend build pass,
12. old task events remain replayable.
