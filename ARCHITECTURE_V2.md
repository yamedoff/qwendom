# Qwendom v2 Architecture Plan

**Date**: 2026-06-26  
**Status**: Planning (no code changes in this pass)  
**Objective**: Upgrade from JSON-parsing governance to native Agno tool-calling agents with visible capabilities.

---

## 1. Target Backend File Structure

```
backend/
├── main.py                          # FastAPI app (unchanged)
├── config.py                        # Settings (add tool config fields)
├── requirements.txt                 # Add: agno>=2.6.19
├── society/
│   ├── __init__.py                  # Export SocietyOrchestrator
│   ├── models.py                    # Add: ToolCallEvent, AgentCapability models
│   ├── memory.py                    # EventStore (unchanged)
│   ├── orchestrator.py              # Refactor: use native tools, remove JSON parsing
│   ├── agents.py                    # Refactor: register tools, use arun()
│   ├── tools/                       # NEW: Native Agno tools
│   │   ├── __init__.py
│   │   ├── governance.py            # LeaderElection, Voting, PeerReview tools
│   │   ├── research.py              # WebSearch, ContextGather tools
│   │   ├── analysis.py              # Decompose, RiskAssess tools
│   │   └── coordination.py          # SpawnChild, DelegateWork tools
│   └── schemas/                     # NEW: Pydantic output schemas
│       ├── __init__.py
│       ├── governance.py            # LeaderDecision, VoteResult, CritiqueReport
│       ├── task.py                  # TaskDecomposition, RiskAssessment
│       └── negotiation.py           # Proposal, CounterProposal
```

**New files to create**:
- `backend/society/tools/__init__.py`
- `backend/society/tools/governance.py`
- `backend/society/tools/research.py`
- `backend/society/tools/analysis.py`
- `backend/society/tools/coordination.py`
- `backend/society/schemas/__init__.py`
- `backend/society/schemas/governance.py`
- `backend/society/schemas/task.py`
- `backend/society/schemas/negotiation.py`

**Files to refactor**:
- `backend/society/agents.py` — register tools, switch to `arun()`
- `backend/society/orchestrator.py` — remove `_parse_json_response`, use structured outputs
- `backend/society/models.py` — add tool-call tracking models

---

## 2. Native Agno Tool API Usage (Verified from Context7 Docs)

### 2.1 Tool Definition Patterns

**Pattern A: Callable function (simplest)**
```python
def get_weather(city: str) -> str:
    """Get weather for a city."""
    return f"Sunny in {city}"

agent = Agent(
    model=OpenAIChat(id="qwen-plus"),
    tools=[get_weather],  # Pass callable directly
)
```

**Pattern B: Function with strict schema (for governance)**
```python
from agno.tools import Function

vote_tool = Function(
    name="cast_vote",
    description="Vote for the best proposal",
    parameters={
        "type": "object",
        "properties": {
            "choice": {"type": "string", "description": "Agent ID of chosen proposal"},
            "reason": {"type": "string", "description": "Why this proposal wins"}
        },
        "required": ["choice", "reason"],
        "additionalProperties": False
    },
    strict=True,
    entrypoint=cast_vote_impl
)
```

**Pattern C: Toolkit class (for grouped tools)**
```python
from agno.tools import Toolkit

class GovernanceTools(Toolkit):
    def __init__(self):
        super().__init__(name="governance")
        self.register(self.elect_leader)
        self.register(self.cast_vote)
    
    def elect_leader(self, task: str, candidates: list[str]) -> str:
        """Elect the best leader for this task."""
        # Implementation
        return json.dumps({"leader_id": "...", "reason": "..."})
```

### 2.2 Structured Output (replaces JSON parsing)

```python
from pydantic import BaseModel
from agno.agent import Agent

class LeaderDecision(BaseModel):
    leader_id: str
    reason: str

agent = Agent(
    model=OpenAIChat(id="qwen-plus"),
    tools=[...],
    output_schema=LeaderDecision,  # Agent returns typed object
)

response = agent.run("Elect a leader for this task...")
decision: LeaderDecision = response.content  # No JSON parsing needed
```

### 2.3 Async Execution

```python
# Current (broken): asyncio.to_thread(agno_agent.run, prompt)
# Correct: use native arun()
response = await agent.arun(prompt)
```

### 2.4 Tool Choice Control

```python
agent = Agent(
    model=OpenAIChat(id="qwen-plus"),
    tools=[...],
    tool_choice="auto",  # or "required", "none", or {"type": "function", "function": {"name": "..."}}
)
```

---

## 3. Minimal Incremental Implementation Plan

### Phase 1: Foundation (this slice)
1. Create `backend/society/tools/` and `backend/society/schemas/` directories
2. Implement `governance.py` tools: `elect_leader_tool`, `cast_vote_tool`, `peer_review_tool`
3. Define Pydantic schemas: `LeaderDecision`, `VoteResult`, `CritiqueReport`
4. Refactor `agents.py` to register tools and use `arun()`
5. Refactor `orchestrator.py` to use structured outputs (remove `_parse_json_response`)
6. Add `ToolCallEvent` model to track tool invocations in event stream

### Phase 2: Research & Analysis Tools
1. Implement `research.py` tools: `web_search_tool`, `gather_context_tool`
2. Implement `analysis.py` tools: `decompose_task_tool`, `risk_assess_tool`
3. Wire tools into researcher and critic agents

### Phase 3: Coordination Tools
1. Implement `coordination.py` tools: `spawn_child_tool`, `delegate_work_tool`
2. Enable leader to spawn children via tool calls (not JSON parsing)
3. Add tool-call visualization to frontend event stream

### Phase 4: Observability
1. Log all tool calls to event store with input/output
2. Add `/tasks/{id}/tool-calls` endpoint
3. Frontend timeline shows tool invocations with expand/collapse

---

## 4. Risks & Fallback Strategy

### Risk 1: OpenRouter models may not support tool-calling
**Mitigation**: 
- Test with `openai/gpt-4o-mini` first (known tool support)
- Add `tool_support` flag to `config.py` per model
- Fallback: if tool call fails, retry with JSON prompt + parsing (current behavior)

**Detection code**:
```python
try:
    response = await agent.arun(prompt)
    if response.content is None and response.tool_calls:
        # Model attempted tools but failed
        raise ToolCallFailure("Model returned tool calls but no content")
except Exception as e:
    if "tool" in str(e).lower() or "function" in str(e).lower():
        # Fallback to JSON parsing
        return await self._ask_agent_json_fallback(agent, prompt)
    raise
```

### Risk 2: Qwen Cloud models may have limited tool support
**Mitigation**:
- Qwen-Plus and Qwen-Max support function calling via OpenAI-compatible API
- Test with `qwen-plus` first
- If tools fail, use `tool_choice="none"` and prompt for JSON output

### Risk 3: Async `arun()` may not work with all providers
**Mitigation**:
- Agno's `arun()` is documented and tested (see Context7 examples)
- Fallback: wrap `run()` in `asyncio.to_thread()` (current approach, but fix the bug)

### Risk 4: Structured output may fail on complex schemas
**Mitigation**:
- Start with simple schemas (2-3 fields)
- Use `output_schema` only for governance decisions
- Fallback: if `response.content` is not a Pydantic instance, parse as JSON

### Risk 5: Tool execution timeout
**Mitigation**:
- Wrap tool calls in `asyncio.wait_for(tools, timeout=settings.llm_timeout_seconds)`
- Log timeout events to event store
- Fallback: return deterministic tool output (e.g., "Leader elected by reputation")

---

## 5. First Implementation Slice (Safe One-Pass Completion)

### Scope
Implement **governance tools only** (Phase 1) with full fallback to current JSON parsing.

### Files to Create

**`backend/society/schemas/governance.py`**:
```python
from pydantic import BaseModel, Field

class LeaderDecision(BaseModel):
    leader_id: str = Field(description="ID of the elected leader")
    reason: str = Field(description="Why this agent was chosen")

class VoteResult(BaseModel):
    choice: str = Field(description="ID of the chosen proposal")
    reason: str = Field(description="Why this proposal wins")

class CritiqueReport(BaseModel):
    critique: str = Field(description="Risks, unsupported assumptions, improvements")
    confidence: float = Field(default=0.5, description="0.0 to 1.0")
```

**`backend/society/tools/governance.py`**:
```python
from agno.tools import Function
from ..schemas.governance import LeaderDecision, VoteResult, CritiqueReport

def elect_leader_impl(task: str, candidates: list[dict]) -> str:
    """Elect a leader from candidates. Returns JSON for tool result."""
    # This is called by the LLM via tool-calling
    # Implementation: return structured decision
    import json
    return json.dumps({"candidates": candidates, "task": task})

elect_leader_tool = Function(
    name="elect_leader",
    description="Elect the best leader for a task from available candidates",
    parameters={
        "type": "object",
        "properties": {
            "task": {"type": "string"},
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "skills": {"type": "array", "items": {"type": "string"}}
                    }
                }
            }
        },
        "required": ["task", "candidates"]
    },
    entrypoint=elect_leader_impl
)

# Similar for cast_vote_tool, peer_review_tool
```

**`backend/society/tools/__init__.py`**:
```python
from .governance import elect_leader_tool, cast_vote_tool, peer_review_tool

__all__ = ["elect_leader_tool", "cast_vote_tool", "peer_review_tool"]
```

### Files to Refactor

**`backend/society/agents.py`**:
```python
from agno.agent import Agent
from agno.models.openai import OpenAIChat
from .tools import elect_leader_tool, cast_vote_tool, peer_review_tool

def build_agno_agent(identity, settings, tools=None):
    return Agent(
        name=identity.name,
        role=identity.role,
        model=OpenAIChat(
            id=settings.active_model,
            api_key=settings.active_api_key,
            base_url=settings.active_base_url,
        ),
        tools=tools or [],  # NEW: accept tools parameter
        instructions=[...],
        markdown=True,
    )
```

**`backend/society/orchestrator.py`** (key changes):
```python
# Remove: _parse_json_response (keep as fallback only)

async def _elect_leader(self, task, team):
    # Try native tool-calling first
    try:
        agent = build_agno_agent(
            coordinator, 
            self.settings,
            tools=[elect_leader_tool]
        )
        agent.output_schema = LeaderDecision
        response = await agent.arun(prompt)
        decision: LeaderDecision = response.content
        leader_id = decision.leader_id
        reason = decision.reason
    except Exception:
        # Fallback to JSON parsing
        raw = await self._ask_agent(coordinator, prompt)
        parsed = _parse_json_response(raw)
        leader_id = parsed.get("leader_id")
        reason = parsed.get("reason", "")
```

### Verification Steps
1. Run backend with `LLM_PROVIDER=openrouter` and `OPENROUTER_MODEL=openai/gpt-4o-mini`
2. Submit a task via `/tasks` endpoint
3. Check `/tasks/{id}/events` for `leader_elected` event with `reason` field
4. Verify no JSON parsing errors in logs
5. Test fallback: set `OPENROUTER_MODEL=meta-llama/llama-3.3-70b-instruct` (may not support tools)
6. Confirm fallback uses JSON parsing and still produces valid events

### Success Criteria
- [ ] Leader election uses native tool-calling when model supports it
- [ ] Fallback to JSON parsing when tool-calling fails
- [ ] No regression in existing functionality
- [ ] Event stream shows tool invocations (future: add `tool_call` event type)
- [ ] Code passes `ruff check` and `mypy` (if configured)

---

## 6. Next Steps After This Slice

1. **Phase 2**: Add research/analysis tools (web search, decomposition)
2. **Phase 3**: Add coordination tools (spawn child, delegate work)
3. **Phase 4**: Frontend visualization of tool calls
4. **Phase 5**: Persistent tool-call history in event store
5. **Phase 6**: Multi-model routing (use GPT-4o for tool-heavy tasks, Qwen for text)

---

## 7. Dependencies

**Current `requirements.txt`** (assumed):
```
fastapi
uvicorn
pydantic
pydantic-settings
agno>=2.6.0
```

**Updated `requirements.txt`**:
```
fastapi
uvicorn
pydantic>=2.0
pydantic-settings>=2.0
agno>=2.6.19  # Native tool support requires recent version
```

**Verify Agno version**:
```bash
pip show agno
# Version: 2.6.19 or higher
```

---

## 8. Open Questions

1. **Tool execution context**: Should tools have access to `EventStore` to log their own invocations?
2. **Tool result format**: Return JSON strings or Pydantic models from tool entrypoints?
3. **Multi-tool agents**: Should each agent have all tools, or role-specific tool subsets?
4. **Tool-call streaming**: How to stream tool invocations to frontend in real-time?
5. **Error handling**: Should tool failures abort the task or trigger fallback?

---

**Recommendation**: Start with Phase 1 (governance tools) as the first slice. It's isolated, testable, and demonstrates the core value of native tool-calling without risking the full orchestrator refactor.
