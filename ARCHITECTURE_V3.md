# Qwendom V3 Architecture Proposal

**Date**: 2026-06-26
**Status**: Draft plan
**Focus**: Agent and society improvements using Agno-native primitives (not UI polish)

---

## 0. Executive Summary

V2 proved that governance decisions can flow through native Agno `@tool` calls with structured Pydantic schemas. The orchestrator, however, hand-rolls every coordination primitive: team formation, leader election, proposal negotiation, voting, monitoring, learning, and dissolution all live as imperative Python in `orchestrator.py`.

V3 replaces the hand-rolled coordination with Agno's native **Team**, **Workflow**, **session_state**, **RunContext**, **Knowledge**, and **persistent memory** primitives. Governance domain logic (debate, revision, voting, reputation) stays as Qwendom-specific tooling on top of those primitives.

### What changes

| Concern | V2 (current) | V3 (target) |
|---|---|---|
| Team formation | Manual `Team` model + `member_ids` list | `agno.team.Team` with `TeamMode` |
| Lifecycle orchestration | Imperative `run_task()` method chain | `agno.workflow.Workflow` with ordered steps |
| Shared governance state | Python dicts passed between methods | `session_state` dict via `RunContext` |
| Memory | In-memory `list[str]` + JSONL replay | Agno `db` plus JSONL audit trail |
| Knowledge | None | Agno `Knowledge` and `KnowledgeTools` per role office |
| Agent tools | Flat `@tool` functions | `RunContext`-aware tools reading/writing session_state |
| Reputation | Float incremented in `_learn()` | Structured reputation store in session_state + durable db |
| Child agents | Hard-coded "Kai" with UUID | Dynamic spawning via Team member delegation |
| Evaluation | None | Metrics emitted as structured events |

---

## 1. Current State Audit

### 1.1 What works

- **Native governance tools** (`tools/governance.py`): `elect_leader`, `decide_spawn`, `cast_vote`, `peer_review` — all use `@tool(stop_after_tool_call=True)` and return validated Pydantic JSON.
- **Capability tools** (`tools/capabilities.py`): `decompose_task`, `memory_lookup`, `implementation_plan`, `risk_assessment`, `memory_write` — one per persistent agent role.
- **Tool result extraction** (`orchestrator.py:60-92`): `_extract_tool_result` correctly walks `response.tools` for native results and rejects prose.
- **Deterministic fallback**: No-key mode preserves all lifecycle steps without LLM calls.
- **Event store**: JSONL append-only log with replay, task summaries, and agent memory reconstruction.
- **Preflight smoke test** (`preflight.py`): Validates native tool calling before full runs.

### 1.2 What blocks V3-quality agent behavior

1. **No Agno Team primitive.** The orchestrator manually iterates `team.member_ids`, calls agents sequentially, and collects proposals in a dict. There is no delegation graph, no parallel member execution, no Agno-native coordination.

2. **No Workflow.** `run_task()` is a 10-step imperative chain. If step 5 fails, steps 1–4 have no replay mechanism beyond re-running the entire task.

3. **No session_state.** Governance state (current proposals, vote tallies, critique results) lives as local variables inside `run_task()`. Tools cannot read or write shared state; they only return JSON strings.

4. **No RunContext in tools.** All `@tool` functions are stateless — they validate arguments and return JSON. They cannot access the team roster, current vote tally, or prior proposals.

5. **No persistent memory beyond JSONL.** Agent memory is `list[str]` on the `SocietyAgent` model, lost on process restart. The JSONL store reconstructs memory-write events but does not support semantic retrieval.

6. **No Knowledge.** Agents have no access to domain knowledge bases. The "researcher" role has no research tools beyond `memory_lookup` which searches the agent's own `list[str]`.

7. **No debate/revision cycle.** Negotiation is one-pass: each agent proposes once in sequence. There is no mechanism for agents to read, challenge, and revise each other's proposals before voting.

8. **No evaluation metrics.** There is no structured measurement of answer quality, governance overhead, tool-call success rates, or memory utilization.

9. **Child agent is hard-coded.** `_do_spawn()` always creates "Kai" with fixed skills. The spawn decision tool returns a `specialist_role` string that is never used to configure the child.

10. **Reputation is ephemeral.** `agent.reputation += 0.15` is in-memory only. It resets on restart and has no decay, no context, and no influence on future governance beyond the leader election heuristic.

---

## 2. Target Architecture

```text
backend/society/
  agents.py                    # Agent factory: builds Agno Agents with role-scoped tools + knowledge
  team.py                      # NEW: Builds agno.team.Team with mode, members, instructions
  workflow.py                  # NEW: Builds agno.workflow.Workflow with governance steps
  session.py                   # NEW: session_state schema and initial state factory
  reputation.py                # NEW: Reputation store backed by Agno db
  metrics.py                   # NEW: Evaluation metric collectors
  orchestrator.py              # Slim coordinator: creates Workflow, runs it, maps events
  memory.py                    # EventStore (kept for JSONL replay) + Agno db adapter
  models.py                    # Domain models (SocietyAgent, SocietyEvent, TaskRun, etc.)
  preflight.py                 # Kept as-is
  schemas/
    governance.py              # Extended: DebateEntry, RevisionRecord, BallotTally
    capabilities.py            # Kept as-is
    evaluation.py              # NEW: TaskMetrics, GovernanceMetrics, MemoryMetrics
  tools/
    governance.py              # Refactored: RunContext-aware tools reading session_state
    debate.py                  # NEW: propose_tool, challenge_tool, revise_tool
    voting.py                  # NEW: cast_ballot_tool, tally_ballots_tool (RunContext)
    delegation.py              # NEW: assign_subtask_tool, report_subtask_tool
    knowledge.py               # NEW: search_knowledge_tool, ingest_knowledge_tool
    evaluation.py              # NEW: record_metric_tool
  knowledge/                   # NEW: Knowledge loaders
    __init__.py
    loaders.py                 # Domain knowledge ingestion (CSV, markdown, URLs)
  config.py                    # Extended: db_url, knowledge_dir, metrics_enabled
```

---

## 3. Staged Implementation Phases

### Phase 1: Agno Team + session_state Foundation

**Goal**: Replace the hand-rolled team and imperative lifecycle with `agno.team.Team` using `TeamMode.coordinate` or `TeamMode.tasks`, and introduce `session_state` as the shared governance ledger.

#### 3.1.1 New file: `backend/society/team.py`

Build an Agno `Team` from the 4 persistent agents:

```python
from agno.team import Team
from agno.team.mode import TeamMode

def build_society_team(members: list[Agent], model, instructions: list[str]) -> Team:
    return Team(
        name="Qwendom Society",
        mode=TeamMode.coordinate,
        model=model,
        members=members,
        instructions=instructions,
        markdown=True,
        show_members_responses=True,
        max_iterations=12,
    )
```

**Team mode choice**:
- `TeamMode.coordinate` for the default path: the team leader delegates to members, members report back, leader synthesizes. This maps naturally to the current elect → negotiate → vote → monitor flow.
- `TeamMode.tasks` as an alternative for complex tasks where the leader creates named subtasks with dependencies (Phase 5).

#### 3.1.2 New file: `backend/society/session.py`

Define the session_state schema that all governance tools read/write through `RunContext`:

```python
def initial_session_state(task_prompt: str, roster: list[dict]) -> dict:
    return {
        "task_prompt": task_prompt,
        "roster": roster,
        "phase": "forming",
        "leader_id": None,
        "proposals": {},
        "challenges": [],
        "revisions": {},
        "ballots": [],
        "tally": {},
        "winner_id": None,
        "critique": None,
        "spawn_decision": None,
        "child_agents": [],
        "metrics": {
            "tool_calls": 0,
            "governance_rounds": 0,
            "debate_rounds": 0,
        },
    }
```

#### 3.1.3 Refactored: `backend/society/tools/governance.py`

All governance tools become `RunContext`-aware. Example for leader election:

```python
from agno.run import RunContext
from agno.tools import tool

@tool(name="elect_leader", stop_after_tool_call=True)
def elect_leader_tool(run_context: RunContext, leader_id: str, reason: str, confidence: float = 0.8) -> str:
    roster_ids = [r["id"] for r in run_context.session_state["roster"]]
    if leader_id not in roster_ids:
        return json.dumps({"error": f"Invalid leader_id '{leader_id}'. Valid: {roster_ids}"})
    run_context.session_state["leader_id"] = leader_id
    run_context.session_state["phase"] = "leader_elected"
    run_context.session_state["metrics"]["tool_calls"] += 1
    return LeaderDecision(leader_id=leader_id, reason=reason, confidence=confidence).model_dump_json()
```

Key change: tools now **write to session_state**, making governance state observable and durable.

#### 3.1.4 Refactored: `backend/society/agents.py`

Agent factory gains `knowledge` and `db` parameters:

```python
def build_agno_agent(
    identity: SocietyAgent,
    settings: Settings,
    tools: list | None = None,
    tool_choice: Any | None = None,
    extra_instructions: list[str] | None = None,
    tool_call_limit: int | None = None,
    knowledge: KnowledgeBase | None = None,
    db: InMemoryDb | None = None,
) -> Agent:
    kwargs = dict(
        name=identity.name,
        role=identity.role,
        model=build_model(settings),
        instructions=instructions,
        markdown=True,
    )
    if tools: kwargs["tools"] = tools
    if tool_choice: kwargs["tool_choice"] = tool_choice
    if tool_call_limit: kwargs["tool_call_limit"] = tool_call_limit
    if knowledge: kwargs["knowledge"] = knowledge
    if db:
        kwargs["db"] = db
        kwargs["update_memory_on_run"] = True
    return Agent(**kwargs)
```

#### 3.1.5 Refactored: `backend/society/orchestrator.py`

The orchestrator becomes a thin wrapper that:
1. Builds the Agno Team.
2. Initializes session_state.
3. Runs the team with `session_state` and `session_id`.
4. Maps Agno Team response events back to `SocietyEvent` for the JSONL store and SSE stream.

The imperative `_elect_leader`, `_negotiate`, `_vote`, `_monitor`, `_learn` methods are removed. Their logic moves into Team instructions and RunContext-aware tools.

#### 3.1.6 Files changed

| File | Action |
|---|---|
| `backend/society/team.py` | **Create** |
| `backend/society/session.py` | **Create** |
| `backend/society/tools/governance.py` | **Refactor** — add RunContext to all tools |
| `backend/society/tools/capabilities.py` | **Refactor** — add RunContext where needed |
| `backend/society/agents.py` | **Refactor** — add knowledge/db params |
| `backend/society/orchestrator.py` | **Major refactor** — replace imperative chain with Team run |
| `backend/society/models.py` | **Extend** — add session_state snapshot to events |
| `backend/config.py` | **Extend** — add `db_backend` field |

#### 3.1.7 Validation criteria

1. `python -m compileall backend` passes.
2. Submit a task. The Agno Team produces a response with member delegation visible in logs.
3. `session_state` after run contains: `leader_id`, `proposals`, `ballots`, `winner_id`, `critique`.
4. JSONL event store still receives `team_formed`, `leader_elected`, `vote_cast`, `peer_monitor_report`, `task_complete`.
5. Deterministic no-key mode still works (Team falls back to deterministic instructions).
6. Preflight (`preflight.py`) still passes with RunContext-aware tools.

#### 3.1.8 Risks

- **Team mode mismatch**: `TeamMode.coordinate` may not enforce the strict governance ordering V2 had. Mitigation: strong Team instructions that encode the lifecycle order.
- **session_state serialization**: Agno persists session_state through its db. If using InMemoryDb, state is lost on restart. Mitigation: start with InMemoryDb, migrate to PostgresDb in Phase 3.
- **Event mapping**: Agno Team emits its own event/response structure. Mapping back to `SocietyEvent` requires parsing member responses. Mitigation: use `show_members_responses=True` and parse structured tool results.

---

### Phase 2: Workflow-Orchestrated Governance Lifecycle

**Goal**: Replace the single Team run with an Agno `Workflow` that encodes the governance lifecycle as discrete, replayable steps.

#### 3.2.1 New file: `backend/society/workflow.py`

```python
from agno.workflow import Workflow

def build_governance_workflow(
    steps: list,
    db: InMemoryDb,
    session_state: dict,
) -> Workflow:
    return Workflow(
        name="Qwendom Governance",
        db=db,
        steps=steps,
        session_state=session_state,
    )
```

Workflow steps map to governance phases:

1. **Form and Elect** — Team runs with election instructions. session_state gains `leader_id`.
2. **Spawn Decision** — Leader agent decides whether to spawn a child. session_state gains `spawn_decision`.
3. **Debate** — Multi-round proposal/challenge/revise cycle (Phase 3 tools). session_state gains `proposals`, `challenges`, `revisions`.
4. **Vote** — Ballot casting and tallying via RunContext tools. session_state gains `ballots`, `tally`, `winner_id`.
5. **Monitor** — Critic reviews winner. session_state gains `critique`.
6. **Learn** — All agents write durable memories. Reputation updated.

Each step is a function that receives `RunContext`, reads/writes `session_state`, and returns a result. If a step fails, the workflow can be re-run from that step.

#### 3.2.2 New file: `backend/society/tools/debate.py`

```python
@tool(name="propose", stop_after_tool_call=True)
def propose_tool(run_context: RunContext, proposal: str, rationale: str) -> str:
    agent_name = run_context.agent_name  # or passed via context
    run_context.session_state["proposals"][agent_name] = {
        "proposal": proposal,
        "rationale": rationale,
        "round": run_context.session_state["metrics"]["debate_rounds"],
    }
    return json.dumps({"status": "recorded", "agent": agent_name})

@tool(name="challenge", stop_after_tool_call=True)
def challenge_tool(run_context: RunContext, target_agent: str, objection: str, suggested_revision: str) -> str:
    run_context.session_state["challenges"].append({
        "challenger": run_context.agent_name,
        "target": target_agent,
        "objection": objection,
        "suggested_revision": suggested_revision,
    })
    return json.dumps({"status": "challenge_recorded"})

@tool(name="revise", stop_after_tool_call=True)
def revise_tool(run_context: RunContext, revised_proposal: str, changes: list[str]) -> str:
    agent_name = run_context.agent_name
    run_context.session_state["revisions"][agent_name] = {
        "revised_proposal": revised_proposal,
        "changes": changes,
        "round": run_context.session_state["metrics"]["debate_rounds"],
    }
    return json.dumps({"status": "revision_recorded"})
```

#### 3.2.3 New file: `backend/society/tools/voting.py`

```python
@tool(name="cast_ballot", stop_after_tool_call=True)
def cast_ballot_tool(run_context: RunContext, choice: str, reason: str, confidence: float = 0.8) -> str:
    ballots = run_context.session_state["ballots"]
    ballots.append({"voter": run_context.agent_name, "choice": choice, "reason": reason, "confidence": confidence})
    return VoteDecision(choice=choice, reason=reason, confidence=confidence).model_dump_json()

@tool(name="tally_ballots", stop_after_tool_call=True)
def tally_ballots_tool(run_context: RunContext) -> str:
    from collections import Counter
    ballots = run_context.session_state["ballots"]
    votes = Counter(b["choice"] for b in ballots)
    tally = dict(votes.most_common())
    winner = votes.most_common(1)[0][0] if votes else None
    run_context.session_state["tally"] = tally
    run_context.session_state["winner_id"] = winner
    run_context.session_state["phase"] = "voted"
    return json.dumps({"tally": tally, "winner": winner})
```

#### 3.2.4 Files changed

| File | Action |
|---|---|
| `backend/society/workflow.py` | **Create** |
| `backend/society/tools/debate.py` | **Create** |
| `backend/society/tools/voting.py` | **Create** |
| `backend/society/schemas/governance.py` | **Extend** — add `DebateEntry`, `RevisionRecord`, `BallotTally` |
| `backend/society/orchestrator.py` | **Refactor** — use Workflow instead of direct Team run |
| `backend/society/session.py` | **Extend** — add debate/revision fields |

#### 3.2.5 Validation criteria

1. Workflow executes all 6 steps in order.
2. After a debate step, `session_state["proposals"]` contains entries from multiple agents.
3. Challenges reference specific proposals and include objections.
4. Revisions reference challenges and modify proposals.
5. `tally_ballots` correctly counts votes from `session_state["ballots"]`.
6. Workflow can be re-run from a failed step (verify by injecting a failure in step 4 and re-running).
7. JSONL events include `debate_round`, `challenge_issued`, `proposal_revised`, `ballot_cast`, `ballot_tallied`.

#### 3.2.6 Risks

- **Debate loop bounds**: Multi-round debate could loop indefinitely. Mitigation: `max_debate_rounds` in session_state, enforced by workflow step logic.
- **Workflow step granularity**: Agno Workflow steps may not map 1:1 to governance phases. Mitigation: each step wraps a Team run or a direct agent call as needed.
- **RunContext agent identity**: `run_context` may not expose the calling agent's name directly. Mitigation: pass agent identity as a tool argument or use `run_context.session_state` to track current actor.

---

### Phase 3: Knowledge and Persistent Memory

**Goal**: Give agents access to domain knowledge bases and replace ephemeral `list[str]` memory with Agno's persistent memory via `db`.

#### 3.3.1 New directory: `backend/society/knowledge/`

```python
# knowledge/loaders.py
from agno.knowledge import KnowledgeBase, TextKnowledgeBase
from agno.vectordb.pgvector import PgVector  # or in-memory alternative

def load_role_knowledge(role: str, knowledge_dir: str) -> KnowledgeBase | None:
    role_files = {
        "architect": "architecture_patterns.md",
        "researcher": "research_methods.md",
        "builder": "implementation_checklists.md",
        "critic": "risk_catalog.md",
    }
    filename = role_files.get(role)
    if not filename:
        return None
    path = os.path.join(knowledge_dir, filename)
    if not os.path.exists(path):
        return None
    return TextKnowledgeBase(path=path)
```

Each persistent agent gets a role-specific knowledge base at construction time. The knowledge base provides:
- **Architect (Ada)**: System design patterns, decomposition heuristics.
- **Researcher (Ibn)**: Research methodologies, evidence standards.
- **Builder (Lin)**: Implementation checklists, integration patterns.
- **Critic (Noor)**: Risk catalogs, quality gate definitions.

#### 3.3.2 Persistent memory via Agno db

Replace `SocietyAgent.memory: list[str]` with Agno's `db` + `update_memory_on_run`:

```python
# In agents.py
agent = Agent(
    ...
    db=InMemoryDb(),  # Phase 3a; PostgresDb in Phase 3b
    update_memory_on_run=True,
    enable_session_summaries=True,
)
```

The `memory_write_tool` still exists but now writes to both the Agno memory store (via session_state) and the JSONL event log (for backward compatibility).

#### 3.3.3 New file: `backend/society/tools/knowledge.py`

```python
@tool(name="search_knowledge")
def search_knowledge_tool(run_context: RunContext, query: str) -> str:
    knowledge = run_context.session_state.get("knowledge_results", {})
    # Delegate to the agent's knowledge base search
    # This is a wrapper; the actual search happens via agent's knowledge param
    return json.dumps({"query": query, "note": "Use agent knowledge for retrieval"})

@tool(name="ingest_knowledge", stop_after_tool_call=True)
def ingest_knowledge_tool(run_context: RunContext, content: str, tags: list[str]) -> str:
    run_context.session_state.setdefault("ingested_knowledge", []).append({
        "content": content,
        "tags": tags,
    })
    return json.dumps({"status": "ingested", "tag_count": len(tags)})
```

#### 3.3.4 Files changed

| File | Action |
|---|---|
| `backend/society/knowledge/__init__.py` | **Create** |
| `backend/society/knowledge/loaders.py` | **Create** |
| `backend/society/tools/knowledge.py` | **Create** |
| `backend/society/agents.py` | **Refactor** — wire knowledge + db to each agent |
| `backend/society/memory.py` | **Extend** — add Agno db adapter alongside JSONL |
| `backend/config.py` | **Extend** — add `knowledge_dir`, `db_backend` fields |

#### 3.3.5 Validation criteria

1. Each agent's knowledge base loads without error at startup.
2. During negotiation, the researcher agent retrieves relevant context from its knowledge base.
3. After a task completes, `agent.get_user_memories(user_id=agent_id)` returns memories from the run.
4. Session summaries are generated and retrievable.
5. JSONL event store still works for backward-compatible replay.
6. `python -m compileall backend` passes.

#### 3.3.6 Risks

- **Knowledge base loading latency**: Large knowledge files slow startup. Mitigation: lazy loading, or pre-built vector indices.
- **InMemoryDb memory loss**: Memories are lost on restart. Mitigation: migrate to PostgresDb in Phase 3b; keep JSONL as durable fallback.
- **Knowledge relevance**: Naive text knowledge bases may return irrelevant results. Mitigation: role-scoped knowledge bases limit the search space.

---

### Phase 4: Role Offices and Reputation System

**Goal**: Formalize agent roles as "offices" with distinct capabilities, term limits, and a structured reputation system that influences governance.

#### 3.4.1 New file: `backend/society/reputation.py`

```python
class ReputationStore:
    def __init__(self, db):
        self.db = db

    def get_reputation(self, agent_id: str) -> dict:
        # Returns: {score, history, wins, losses, specializations}
        ...

    def update_reputation(self, agent_id: str, delta: float, reason: str, task_id: str) -> None:
        ...

    def rank_for_task(self, agent_ids: list[str], task_prompt: str) -> list[str]:
        # Returns agents ranked by fitness for the given task
        ...
```

Reputation dimensions:
- **Overall score**: Float, starts at 1.0, adjusted after each task.
- **Win rate**: Ratio of proposals that won votes.
- **Critique accuracy**: How often the critic's risks materialized (tracked via evaluation metrics).
- **Specialization affinity**: Which task types this agent historically excelled at.

#### 3.4.2 Role offices

Each persistent agent holds an "office" that defines:
- **Mandate**: What the office is responsible for.
- **Tools**: Which RunContext tools the office holder can use.
- **Knowledge**: Which knowledge base is attached.
- **Term**: How many tasks before the office is re-evaluated.

```python
OFFICES = {
    "architect": Office(
        mandate="Decompose problems and design solution architecture",
        tools=[decompose_task_tool, propose_tool, assign_subtask_tool],
        knowledge_role="architect",
    ),
    "researcher": Office(
        mandate="Gather evidence and test assumptions",
        tools=[memory_lookup_tool, search_knowledge_tool, challenge_tool],
        knowledge_role="researcher",
    ),
    "builder": Office(
        mandate="Plan and specify implementation artifacts",
        tools=[implementation_plan_tool, propose_tool, revise_tool],
        knowledge_role="builder",
    ),
    "critic": Office(
        mandate="Identify risks and enforce quality gates",
        tools=[risk_assessment_tool, peer_review_tool, challenge_tool],
        knowledge_role="critic",
    ),
}
```

#### 3.4.3 Reputation-influenced governance

Leader election now considers reputation:

```python
@tool(name="elect_leader", stop_after_tool_call=True)
def elect_leader_tool(run_context: RunContext, leader_id: str, reason: str, confidence: float = 0.8) -> str:
    reputations = run_context.session_state.get("reputations", {})
    # The agent sees reputations in the prompt; the tool validates the choice
    ...
```

The orchestrator injects reputation scores into the election prompt so the electing agent can make informed decisions.

#### 3.4.4 Files changed

| File | Action |
|---|---|
| `backend/society/reputation.py` | **Create** |
| `backend/society/offices.py` | **Create** — Office definitions and tool scoping |
| `backend/society/agents.py` | **Refactor** — wire office tools and knowledge per agent |
| `backend/society/tools/governance.py` | **Refactor** — inject reputation data into election prompts |
| `backend/society/session.py` | **Extend** — add `reputations` to session_state |
| `backend/society/models.py` | **Extend** — add `Office` model |

#### 3.4.5 Validation criteria

1. Each agent receives only the tools defined in its office.
2. Leader election prompt includes reputation scores for all candidates.
3. After a task, reputation scores are updated and persisted.
4. An agent with consistently losing proposals sees its reputation decrease.
5. Reputation survives process restart (via db).

#### 3.4.6 Risks

- **Reputation gaming**: Agents might optimize for reputation rather than task quality. Mitigation: reputation is multi-dimensional and includes critique accuracy.
- **Office rigidity**: Fixed offices may not fit all task types. Mitigation: offices define defaults; the leader can override tool access via session_state.

---

### Phase 5: Spawned Child Agents and Delegation

**Goal**: Replace the hard-coded "Kai" spawn with dynamic child agent creation driven by the leader's spawn decision and the task decomposition.

#### 3.5.1 Dynamic child spawning

When `decide_spawn_tool` returns `spawn=True` with a `specialist_role`, the workflow:

1. Creates a new `SocietyAgent` with skills derived from `specialist_role`.
2. Builds an Agno `Agent` with role-appropriate tools and knowledge.
3. Adds the child to the Team's member list (or runs it as a sub-agent via `TeamMode.tasks`).
4. Records the child in `session_state["child_agents"]`.

```python
def spawn_child_agent(spawn_decision: SpawnDecision, task_prompt: str, settings: Settings) -> Agent:
    child_identity = SocietyAgent(
        id=f"child-{uuid4().hex[:8]}",
        name=generate_child_name(spawn_decision.specialist_role),
        role=spawn_decision.specialist_role,
        skills=infer_skills(spawn_decision.specialist_role),
        parent_id=spawn_decision.spawned_by,
    )
    return build_agno_agent(
        identity=child_identity,
        settings=settings,
        tools=infer_tools(spawn_decision.specialist_role),
        extra_instructions=[f"You are a specialist spawned for: {spawn_decision.specialist_role}"],
    )
```

#### 3.5.2 New file: `backend/society/tools/delegation.py`

```python
@tool(name="assign_subtask", stop_after_tool_call=True)
def assign_subtask_tool(run_context: RunContext, agent_id: str, subtask: str, deadline_step: int) -> str:
    run_context.session_state.setdefault("subtasks", []).append({
        "agent_id": agent_id,
        "subtask": subtask,
        "deadline_step": deadline_step,
        "status": "assigned",
    })
    return json.dumps({"status": "assigned", "agent_id": agent_id})

@tool(name="report_subtask", stop_after_tool_call=True)
def report_subtask_tool(run_context: RunContext, result: str, blockers: list[str] | None = None) -> str:
    agent_name = run_context.agent_name
    subtasks = run_context.session_state.get("subtasks", [])
    for st in subtasks:
        if st["agent_id"] == agent_name and st["status"] == "assigned":
            st["status"] = "completed"
            st["result"] = result
            st["blockers"] = blockers or []
            break
    return json.dumps({"status": "reported"})
```

#### 3.5.3 Files changed

| File | Action |
|---|---|
| `backend/society/tools/delegation.py` | **Create** |
| `backend/society/agents.py` | **Refactor** — add `spawn_child_agent` factory |
| `backend/society/workflow.py` | **Refactor** — add spawn step that creates real child agents |
| `backend/society/session.py` | **Extend** — add `subtasks`, `child_agents` to session_state |

#### 3.5.4 Validation criteria

1. Spawn decision with `specialist_role="Data Analyst"` creates a child agent with data analysis skills.
2. Child agent participates in debate and voting.
3. Child agent is dissolved after the task (removed from agent registry).
4. `session_state["child_agents"]` lists all spawned children with their roles.
5. Subtask assignment and reporting are visible in session_state.

#### 3.5.5 Risks

- **Team member mutation**: Agno Team may not support adding members mid-run. Mitigation: spawn children before the Team run, or use a separate agent call for child work.
- **Child agent quality**: Dynamically created agents may produce lower-quality work. Mitigation: child agents inherit office knowledge bases and are monitored by the critic.

---

### Phase 6: Evaluation Metrics

**Goal**: Emit structured evaluation metrics for every task, enabling measurement of agent and society quality over time.

#### 3.6.1 New file: `backend/society/metrics.py`

```python
class MetricsCollector:
    def __init__(self):
        self.task_metrics: list[TaskMetrics] = []

    def record(self, metrics: TaskMetrics) -> None:
        self.task_metrics.append(metrics)

    def summary(self) -> dict:
        ...
```

#### 3.6.2 New file: `backend/society/schemas/evaluation.py`

```python
class TaskMetrics(BaseModel):
    task_id: str
    total_duration_seconds: float
    governance_rounds: int
    debate_rounds: int
    tool_calls_total: int
    tool_calls_failed: int
    proposals_count: int
    challenges_count: int
    revisions_count: int
    vote_margin: float  # winner votes / total votes
    critique_risk_count: int
    child_agents_spawned: int
    memory_writes: int
    answer_length: int

class GovernanceMetrics(BaseModel):
    leader_election_confidence: float
    average_vote_confidence: float
    debate_productivity: float  # revisions / challenges
    reputation_variance: float

class MemoryMetrics(BaseModel):
    memories_created: int
    memories_retrieved: int
    knowledge_searches: int
    session_summary_generated: bool
```

#### 3.6.3 New file: `backend/society/tools/evaluation.py`

```python
@tool(name="record_metric", stop_after_tool_call=True)
def record_metric_tool(run_context: RunContext, metric_name: str, value: float, context: str = "") -> str:
    run_context.session_state.setdefault("evaluation_metrics", []).append({
        "metric_name": metric_name,
        "value": value,
        "context": context,
    })
    return json.dumps({"status": "recorded", "metric": metric_name})
```

#### 3.6.4 Metrics collection points

The workflow's final step computes metrics from session_state:

```python
def compute_task_metrics(session_state: dict, start_time: float) -> TaskMetrics:
    ballots = session_state.get("ballots", [])
    tally = session_state.get("tally", {})
    winner_votes = max(tally.values()) if tally else 0
    total_votes = sum(tally.values()) if tally else 1
    return TaskMetrics(
        task_id=session_state.get("task_id", ""),
        total_duration_seconds=time.time() - start_time,
        governance_rounds=session_state["metrics"]["governance_rounds"],
        debate_rounds=session_state["metrics"]["debate_rounds"],
        tool_calls_total=session_state["metrics"]["tool_calls"],
        tool_calls_failed=session_state["metrics"].get("tool_calls_failed", 0),
        proposals_count=len(session_state.get("proposals", {})),
        challenges_count=len(session_state.get("challenges", [])),
        revisions_count=len(session_state.get("revisions", {})),
        vote_margin=winner_votes / total_votes if total_votes else 0,
        critique_risk_count=len(session_state.get("critique", {}).get("risks", [])),
        child_agents_spawned=len(session_state.get("child_agents", [])),
        memory_writes=session_state["metrics"].get("memory_writes", 0),
        answer_length=len(session_state.get("final_answer", "")),
    )
```

#### 3.6.5 Files changed

| File | Action |
|---|---|
| `backend/society/metrics.py` | **Create** |
| `backend/society/schemas/evaluation.py` | **Create** |
| `backend/society/tools/evaluation.py` | **Create** |
| `backend/society/workflow.py` | **Refactor** — add metrics computation as final step |
| `backend/society/orchestrator.py` | **Refactor** — emit metrics events |
| `backend/main.py` | **Extend** — add `GET /metrics` endpoint |

#### 3.6.6 Validation criteria

1. After each task, a `task_metrics` event is emitted with all fields populated.
2. `GET /metrics` returns aggregate statistics across all tasks.
3. Metrics are persisted in JSONL and survive restart.
4. `debate_productivity` correctly computes revisions/challenges.
5. `vote_margin` reflects actual ballot distribution.

#### 3.6.7 Risks

- **Metric noise**: Small sample sizes produce misleading averages. Mitigation: show confidence intervals or require N≥5 tasks before displaying aggregates.
- **Metric gaming**: Agents might optimize for metrics (e.g., always challenging to inflate `challenges_count`). Mitigation: metrics are observational, not used to reward agents directly.

---

## 4. Dependency Graph

```text
Phase 1 (Team + session_state)
  └── Phase 2 (Workflow + debate/voting)
        ├── Phase 3 (Knowledge + persistent memory)
        │     └── Phase 4 (Role offices + reputation)
        └── Phase 5 (Child agents + delegation)
              └── Phase 6 (Evaluation metrics)
```

Phases 3 and 5 can proceed in parallel after Phase 2. Phase 4 depends on Phase 3 (knowledge bases for offices). Phase 6 depends on Phase 5 (child agent metrics).

---

## 5. Migration Strategy

### 5.1 Backward compatibility

- The JSONL `EventStore` is kept throughout all phases. Agno db is additive.
- The `/tasks`, `/tasks/{id}/events`, `/agents/{id}/memory` API endpoints remain stable.
- Deterministic no-key mode is preserved in every phase.

### 5.2 Incremental adoption

Each phase is independently deployable:
- Phase 1 can ship without Phases 2–6. The orchestrator uses Team but still runs a single-pass lifecycle.
- Phase 2 can ship without Phases 3–6. The workflow runs but agents have no knowledge bases.
- Phase 3 can ship without Phases 4–6. Agents have knowledge but no formal offices.

### 5.3 Migration guardrails

V3 should be adopted through small vertical slices, but the target architecture
must not preserve V2's hand-rolled society as a permanent alternative path.
During a phase, temporary compatibility code is acceptable only when it keeps
the app runnable while the new Agno-native primitive is being validated.

Rules:

- Do not add a silent runtime fallback from Agno `Team`/`Workflow` to the old
  manual orchestration in LLM-enabled mode.
- If an Agno-native primitive fails, emit a clear task failure with evidence.
- Keep deterministic no-key mode for local development, but label it
  `deterministic_no_key`.
- Remove compatibility code at the end of each phase once validation passes.
- Preserve existing API contracts and event names unless the plan explicitly
  introduces a replacement event.

---

## 6. Explicit Non-Goals for V3

- **UI redesign**: No frontend changes. The React control room consumes the same SSE event stream.
- **Multi-model routing**: All agents use the same configured model. Per-agent model selection is deferred.
- **External integrations**: No web search, code execution, file system, or API tools beyond what V2 already excludes.
- **Authentication/authorization**: No user auth or agent identity verification.
- **Production database**: PostgresDb is optional. InMemoryDb is the default for local development.
- **Real-time streaming of session_state**: The SSE stream emits events, not live session_state diffs.

---

## 7. Risk Register

| Risk | Phase | Severity | Mitigation |
|---|---|---|---|
| Agno Team mode does not enforce governance ordering | 1 | High | Strong Team instructions; fallback to sequential agent calls |
| RunContext does not expose agent identity | 1 | Medium | Pass agent_id as explicit tool argument |
| session_state serialization fails for complex types | 1 | Medium | Keep session_state values as JSON-serializable primitives |
| Workflow step replay not supported by Agno | 2 | High | Implement manual step checkpointing in orchestrator |
| Knowledge base loading blocks startup | 3 | Medium | Lazy loading; pre-built indices |
| InMemoryDb loses memories on restart | 3 | Medium | JSONL remains as durable fallback; PostgresDb migration path |
| Reputation system creates perverse incentives | 4 | Medium | Multi-dimensional reputation; observational only |
| Dynamic child agents degrade Team performance | 5 | Medium | Cap child agents at 2 per task; monitor via metrics |
| Metrics collection adds latency | 6 | Low | Async metrics emission; compute at workflow end only |
| Compatibility code becomes permanent | All | Medium | Keep compatibility phase-local and delete it after validation |

---

## 8. Success Criteria (V3 Complete)

1. A submitted task runs through an Agno Team with Workflow-orchestrated governance.
2. Agents use RunContext-aware tools that read/write shared session_state.
3. Multi-round debate with challenges and revisions occurs before voting.
4. Each agent has a role-specific knowledge base and persistent memory.
5. The leader can spawn dynamic child agents with task-appropriate skills.
6. Reputation scores influence leader election and are updated after each task.
7. Structured evaluation metrics are emitted for every task and queryable via API.
8. All V2 API endpoints remain backward compatible.
9. Deterministic no-key mode still works for local development.
10. `python -m compileall backend` and `cd frontend && npm run build` both pass.
