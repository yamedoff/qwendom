from __future__ import annotations

import asyncio
import copy
import json
import time
from collections import Counter
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any, Type, TypeVar
from uuid import uuid4

from agno.agent import Agent
from pydantic import BaseModel, ValidationError

from config import Settings, get_settings
from .agents import build_agno_agent, build_model, fallback_contribution, role_tool_context, role_tool_instructions
from .composition_runtime import CompositionRuntime
from .provider_runtime import run_provider_call
from .capability_registry import (
    default_required_capabilities,
    find_capable_team_member,
    get_role_capabilities,
    resolve_role_key,
)
from .db import get_agno_db
from .error_taxonomy import ErrorCategory, build_attempt_record, classify_error, compute_backoff, is_retryable, make_idempotency_key
from .memory import EventStore
from .metrics import MetricsCollector, summarize_task_metrics
from .models import AgentProfile, SocietyAgent, SocietyEvent, TaskRun, Team, ToolCallPayload, now_iso
from .reputation import ReputationStore
from .schemas.capabilities import (
    ImplementationPlan,
    MemoryLookup,
    MemoryWrite,
    RiskAssessment,
    TaskDecomposition,
)
from .schemas.governance import (
    CritiqueReport,
    LeaderDecision,
    LeaderSynthesisRecord,
    SpawnDecision,
    VoteDecision,
)
from .schemas.evaluation import IndependentValidationReport, TaskMetrics, MetricRecord
from .schemas.artifacts import ArtifactRecord, ArtifactReference, FinalDeliverable
from .schemas.debate import ChallengeRecord, ProposalOpinionRecord, ProposalRecord, RevisionRecord
from .schemas.delegation import SubtaskAssignment, SubtaskReport
from .schemas.voting import TallyResult
from .schemas.conversation import (
    ConversationTurn,
    GoalDiscussionStatement,
    MeetingRecap,
    ReadinessBallot,
    ReadinessBlocker,
    ReadinessTally,
    TargetedQuestionExchange,
    WorkingBrief,
    BLOCKING_CATEGORIES,
)
from .schemas.coordination import CoordinationSubtaskHint, TeamCoordinationBrief
from .schemas.social import AgentPosition, CollaborationAction, EndorsementRecord, MindChangeRecord, ObjectionRecord, PrivateNote, TrustUpdate
from .session import initial_session_state
from .team import build_society_team
from .team_composer import (
    AgnoTeamPlanProvider,
    CompositionContext,
    TeamComposer,
    TeamCompositionBlocked,
    TeamCompositionProviderError,
    TeamCompositionResult,
)
from .specialist_selection import (
    FixedSpecialistCoordinator,
    InvokeSpecialistCall,
    ListSpecialistsCall,
    ListSpecialistsResult,
    SelectSpecialistsCall,
    SpecialistSelectionError,
)
from .tools.capabilities import (
    decompose_task_tool,
    implementation_plan_tool,
    memory_lookup_tool,
    memory_write_tool,
    risk_assessment_tool,
    load_notes_demo_evidence,
)
from .workflow import build_governance_workflow, compute_task_metrics, should_skip_phase, get_loop_budget
from .tools.governance import (
    cast_vote_tool,
    decide_spawn_tool,
    elect_leader_tool,
    leader_synthesize_tool,
    peer_review_tool,
)
from .tools.delegation import assign_subtask_tool, report_subtask_tool
from .tools.debate import challenge_tool, propose_tool, record_proposal_opinion_tool, revise_tool
from .tools.voting import cast_ballot_tool, tally_ballots_tool
from .tools.evaluation import record_metric_tool, report_independent_validation_tool
from .tools.conversation import submit_goal_discussion_tool, cast_readiness_vote_tool
from .tools.social import publish_private_note_tool, record_private_note_tool, state_position_tool
from .tools.specialists import list_specialists_tool, select_specialists_tool

T = TypeVar("T", bound=BaseModel)

NATIVE_TOOL_INSTRUCTIONS = [
    "You must call the provided tool exactly once.",
    "Do not answer in prose.",
    "Put your final decision or work product into the tool arguments.",
]


def _parse_json_response(text: str) -> dict | None:
    """Parse a tool-returned JSON object; never mine prose for governance decisions."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


_NO_MOCK_MARKERS: tuple[str, ...] = (
    "no mock",
    "no mocks",
    "no-mock",
    "no fabrication",
    "no-fabrication",
    "do not fabricate",
    "do not mock",
    "don't fabricate",
    "don't mock",
    "without mocking",
    "without fabrication",
    "no simulated",
    "no synthetic",
    "real evidence only",
    "actual evidence",
)


def _prompt_has_no_mock_constraint(prompt: str) -> bool:
    lowered = prompt.lower()
    return any(marker in lowered for marker in _NO_MOCK_MARKERS)


_MOCK_RECOMMENDATION_MARKERS: tuple[str, ...] = (
    "mock",
    "simulate",
    "fake",
    "synthetic",
    "offline",
    "stub",
    "placeholder",
)


def _strip_mock_recommendations(text: str) -> str:
    lines = text.split("\n")
    kept: list[str] = []
    for line in lines:
        lowered = line.lower().strip()
        if not lowered.startswith(("do not ", "don't ", "never ", "no ")) and any(marker in lowered for marker in _MOCK_RECOMMENDATION_MARKERS) and any(
            verb in lowered for verb in ("recommend", "suggest", "use", "try", "prefer", "could")
        ):
            continue
        kept.append(line)
    return "\n".join(kept).strip() or text


def _contains_mock_recommendation(text: str) -> bool:
    """Return whether the answer recommends simulated behavior as a deliverable."""

    for line in text.splitlines():
        lowered = line.lower()
        if any(marker in lowered for marker in ("mock", "simulate", "fake", "synthetic", "stub")) and any(
            verb in lowered for verb in ("recommend", "suggest", "use", "try", "prefer", "could", "can")
        ):
            return True
    return False


def _prompt_requires_implementation(prompt: str) -> bool:
    """Determine if a prompt requires actual implementation work.

    This deterministic classifier distinguishes between:
    - Analysis/documentation deliverables (memo, report, analysis, recommendation, checklist)
      which do NOT require implementation
    - Explicit implementation requests (implement, build, develop, ship, code, prototype,
      construct, runnable, executable) which DO require implementation
    - "Create" followed by concrete artifacts (dashboard, API, system, app, etc.) which
      DO require implementation

    Returns False for:
    - Explicit analysis-only or no-implementation briefs
    - Producing/writing memos, reports, analyses, recommendations, or checklists

    Returns True for:
    - Explicit implement/build/develop/ship/code/prototype/construct/runnable/executable
    - "Create a dashboard" or similar concrete artifact creation
    """
    lowered = prompt.lower()

    # Early exit: explicit no-implementation or analysis-only directives
    no_impl_markers = (
        "no implementation",
        "analysis only",
        "analyses only",
        "do not implement",
        "don't implement",
        "no need to implement",
        "not implement",
    )
    if any(marker in lowered for marker in no_impl_markers):
        return False

    # Explicit implementation language takes precedence over an accompanying
    # documentation deliverable (for example, "build the API and write a report").
    strong_impl_markers = (
        "implement",
        "build",
        "develop",
        "ship",
        "code",
        "prototype",
        "construct",
        "runnable",
        "executable",
    )
    if any(marker in lowered for marker in strong_impl_markers):
        return True

    # Document deliverables that don't require implementation
    doc_deliverables = (
        "memo",
        "report",
        "analysis",
        "recommendation",
        "checklist",
        "summary",
        "overview",
        "review",
        "assessment",
        "evaluation",
    )

    # Check if prompt is asking to produce/write/create a document
    doc_verbs = ("produce", "write", "create", "draft", "prepare", "generate")
    for verb in doc_verbs:
        if verb in lowered:
            # Check if it's followed by a document deliverable
            for deliverable in doc_deliverables:
                if deliverable in lowered:
                    return False

    # "Create" requires context - only counts if followed by concrete artifact
    if "create" in lowered:
        concrete_artifacts = (
            "dashboard",
            "api",
            "system",
            "app",
            "application",
            "service",
            "tool",
            "feature",
            "component",
            "module",
            "interface",
            "website",
            "platform",
            "solution",
            "program",
            "function",
            "class",
            "method",
            "endpoint",
            "integration",
            "automation",
            "pipeline",
            "workflow",
            "script",
        )
        if any(artifact in lowered for artifact in concrete_artifacts):
            return True

    return False


def _prompt_requires_collaborative_notes_demo(prompt: str) -> bool:
    """Return whether the request actually asks for the notes-demo benchmark.

    ``execute_notes_demo`` creates a fixed ``server.py``/``client.html``
    artifact pair. That evidence is only relevant to collaborative-notes
    delivery and must not satisfy an unrelated code-execution request.
    """

    lowered = prompt.lower()
    return "collaborative notes" in lowered or "collaborative-notes" in lowered


def _prompt_requires_validation(prompt: str) -> bool:
    lowered = prompt.lower()
    return any(
        marker in lowered
        for marker in (
            "validat",
            "test",
            "verify",
            "check",
            "acceptance",
            "quality",
            "review",
            "audit",
            "risk",
            "safe",
        )
    )


def _is_execution_output_blocker(reason: str) -> bool:
    """Identify circular readiness objections that demand work-phase outputs.

    .. deprecated::
        Retained for backward compatibility only. The runtime decision-maker
        is :func:`_is_structured_execution_blocker`, which uses typed blocker
        categories rather than keyword heuristics.
    """

    lowered = reason.lower()
    output_markers = (
        "implementation", "artifact", "citation", "url", "evidence", "validation",
        "test result", "builder", "critic", "researcher", "leader election",
    )
    absence_markers = ("missing", "not yet", "has not", "haven't", "incomplete", "without")
    return any(marker in lowered for marker in output_markers) and any(
        marker in lowered for marker in absence_markers
    )


def _is_structured_execution_blocker(ballot: ReadinessBallot) -> bool:
    """Decide whether a readiness ballot blocks execution using typed categories.

    Only ballots whose ``blocker_category`` is in ``BLOCKING_CATEGORIES``
    (``missing_user_input``, ``missing_system_capability``, ``safety_or_policy``)
    block execution. Ballots categorized as ``future_work`` or ``risk`` are
    recorded but do not block.

    Legacy critical ballots without a category block conservatively. Their
    prose is never inspected to make the readiness decision.
    """

    if not ballot.critical_blocker:
        return False
    if ballot.blocker_category is not None:
        return ballot.blocker_category in BLOCKING_CATEGORIES
    return True


def _json_only_retry_instruction(tool_name: str, schema_class: Type[BaseModel]) -> str:
    """Ask providers without a tool envelope for one schema-bound JSON result."""

    properties = ", ".join(schema_class.model_json_schema().get("properties", {}).keys())
    return (
        f"Your previous response did not call the required `{tool_name}` tool. "
        f"Retry now: call `{tool_name}` exactly once. If this provider cannot emit a tool call, "
        f"return ONLY one complete JSON object with these fields: {properties}. "
        "Do not include markdown, commentary, or any surrounding prose."
    )


def _strict_json_only_instruction(tool_name: str, schema_class: Type[BaseModel]) -> str:
    """Strict complete-JSON-only instruction with full schema for providers that cannot use output_schema."""

    schema = schema_class.model_json_schema()
    schema_json = json.dumps(schema, indent=2)
    return (
        f"Return ONLY a single complete JSON object matching this schema exactly. "
        f"No markdown, no commentary, no prose, no code fences. "
        f"The JSON object must validate against:\n{schema_json}\n"
        f"Your entire response must be parseable by json.loads() as one object."
    )


def _extract_tool_result(
    response: Any,
    tool_name: str,
    schema_class: Type[T],
) -> T:
    """Extract and validate a native tool result from an Agno response.

    Prefer Agno's explicit tool result. Some OpenAI-compatible Qwen responses
    return the forced tool arguments as a standalone JSON response instead of
    populating ``response.tools``; accept that exact, schema-validated object
    too. Never mine a decision from surrounding prose.
    """
    raw_json: str | None = None

    tools_list = getattr(response, "tools", None)
    if tools_list and isinstance(tools_list, list):
        for tool_entry in tools_list:
            entry_name = getattr(tool_entry, "tool_name", None) or getattr(tool_entry, "name", None)
            if entry_name and tool_name in str(entry_name):
                result_val = getattr(tool_entry, "result", None)
                if result_val is not None:
                    raw_json = str(result_val) if not isinstance(result_val, str) else result_val
                    break

    if raw_json is None:
        content = getattr(response, "content", None)
        if isinstance(content, BaseModel):
            raw_json = content.model_dump_json()
        elif isinstance(content, dict):
            raw_json = json.dumps(content)
        elif isinstance(content, str):
            # ``_parse_json_response`` only accepts a complete JSON object;
            # prose containing an embedded vote remains invalid.
            raw_json = content

    if raw_json is None:
        raise ValueError(f"No tool result found for '{tool_name}' in agent response")

    parsed = _parse_json_response(raw_json)
    if parsed is None:
        raise ValueError(f"Could not parse tool result JSON for '{tool_name}': {raw_json[:200]}")

    try:
        return schema_class.model_validate(parsed)
    except ValidationError as exc:
        raise ValueError(f"Tool result validation failed for '{tool_name}': {exc}") from exc


class GovernanceToolError(Exception):
    """Raised when a native governance tool call fails in LLM-enabled mode."""


class SocietyOrchestrator:
    """Coordinates the full Agent Society lifecycle for each submitted task."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        event_path = (
            Path(self.settings.event_store_file).expanduser()
            if self.settings.event_store_file
            else Path(__file__).resolve().parent / "data" / "events.jsonl"
        )
        self.events = EventStore(event_path)
        self.tasks: dict[str, TaskRun] = {}
        self.teams: dict[str, Team] = {}
        self.session_states: dict[str, dict[str, Any]] = {}
        self.agno_teams: dict[str, Any] = {}
        self.workflows: dict[str, Any] = {}
        self.metrics = MetricsCollector()
        self.reputation = ReputationStore()
        self.agno_db = get_agno_db(self.settings)
        self.agents: dict[str, SocietyAgent] = self._seed_agents()
        self._composition_cancellation_events: dict[str, asyncio.Event] = {}
        self._load_reputation_from_events()
        self._reconcile_stale_running_tasks()
        self._restore_from_events()

    def _seed_agents(self) -> dict[str, SocietyAgent]:
        agents = [
            SocietyAgent(
                id="architect",
                name="Ada",
                role="Systems Architect",
                skills=["decomposition", "architecture", "tradeoffs"],
                profile=AgentProfile(
                    values=["clear boundaries", "decomposable systems", "long-term maintainability"],
                    communication_style="structured, scope-aware, and explicit about tradeoffs",
                    risk_tolerance="medium",
                    decision_bias="prefer coherent system shape before optimizing local details",
                    default_blockers=["vague ownership", "incoherent architecture", "missing dependency boundaries"],
                    defers_to={"builder": ["implementation cost", "delivery sequencing"]},
                    failure_mode="can over-design when a smaller reversible slice would be enough",
                ),
            ),
            SocietyAgent(
                id="researcher",
                name="Researcher",
                role="Research Analyst",
                skills=["evidence", "assumption testing", "context gathering"],
                profile=AgentProfile(
                    values=["evidence quality", "uncertainty labeling", "context before confidence"],
                    communication_style="careful, question-driven, and explicit about unknowns",
                    risk_tolerance="low",
                    decision_bias="prefer supported claims over fast unsupported convergence",
                    default_blockers=["unsupported claims", "missing context", "unstated assumptions"],
                    defers_to={"architect": ["system shape", "architecture tradeoffs"]},
                    failure_mode="can slow execution by asking for more evidence than the task needs",
                ),
            ),
            SocietyAgent(
                id="builder",
                name="Lin",
                role="Implementation Engineer",
                skills=["prototyping", "integration", "delivery"],
                profile=AgentProfile(
                    values=["shipping useful slices", "clear acceptance checks", "practical sequencing"],
                    communication_style="direct, delivery-focused, and concrete about next actions",
                    risk_tolerance="high",
                    decision_bias="prefer the smallest runnable implementation that proves value",
                    default_blockers=["non-executable plans", "unclear deliverables", "missing validation command"],
                    defers_to={"critic": ["validation risk", "quality gates"]},
                    failure_mode="can under-investigate edge cases when momentum is high",
                ),
            ),
            SocietyAgent(
                id="critic",
                name="Noor",
                role="Adversarial Reviewer",
                skills=["risk analysis", "quality gates", "counterarguments"],
                profile=AgentProfile(
                    values=["correctness", "safety", "bounded risk", "clear failure modes"],
                    communication_style="concise, skeptical, and focused on what could break",
                    risk_tolerance="low",
                    decision_bias="prefer slowing down when an unresolved critical risk remains",
                    default_blockers=["unbounded risk", "missing validation", "ignored critical objection"],
                    defers_to={"researcher": ["evidence quality", "claim support"]},
                    failure_mode="can over-block when a documented caveat would be sufficient",
                ),
            ),
        ]
        return {agent.id: agent for agent in agents}

    def _load_reputation_from_events(self) -> None:
        """Replay the latest persisted reputation snapshot from JSONL events."""

        for event in reversed(self.events.list()):
            if event.type == "reputation_updated" and isinstance(event.payload.get("reputations"), dict):
                self.reputation.load_snapshot(event.payload["reputations"])
                return

    def _restore_from_events(self) -> None:
        """Reconstruct in-memory task/team/session state from the event log.

        The event log is the authoritative persistence store. On startup, this
        method replays events to find tasks that were interrupted mid-run or
        paused for user clarification and rebuilds the minimum in-memory state
        needed to resume or report them truthfully.

        Tasks paused for either readiness clarification or vote resolution are
        reconstructed with enough team/session state to resume. Tasks that
        were still ``running`` at shutdown remain interrupted summaries so
        callers see the honest truth rather than fabricated completion.
        """

        for task_id in self.events.task_ids():
            events = self.events.list(task_id)
            if not events:
                continue
            last_status = self._derive_final_status(events)
            if last_status != "waiting_for_user":
                continue
            pause_event = None
            for event in reversed(events):
                if event.type == "user_clarification_requested":
                    pause_event = event
                    break
            if pause_event is None:
                continue
            resume_phase = pause_event.payload.get("resume_phase")
            if task_id in self.tasks:
                continue
            if resume_phase == "vote_resolution":
                self._reconstruct_vote_resolution_task(task_id, events, pause_event)
            elif resume_phase == "team_composition":
                self._reconstruct_team_composition_task(task_id, events, pause_event)
            else:
                self._reconstruct_readiness_task(task_id, events, pause_event)

    def _reconcile_stale_running_tasks(self) -> None:
        """Mark event-ledger runs orphaned by a process restart as interrupted."""

        for task_id in self.events.task_ids():
            events = self.events.list(task_id)
            if not events or self._derive_final_status(events) != "running":
                continue
            self.events.append(
                SocietyEvent(
                    task_id=task_id,
                    type="task_interrupted",
                    message="Task was interrupted because the application process restarted.",
                    payload={"phase": "unknown", "reason": "process_restart"},
                )
            )

    @staticmethod
    def _derive_final_status(events: list[SocietyEvent]) -> str:
        """Walk events in order and return the last known task status."""

        status = "running"
        for event in events:
            if event.type == "task_complete":
                payload = event.payload if isinstance(event.payload, dict) else {}
                acc = payload.get("acceptance_status")
                if acc == "complete_with_warnings":
                    status = "complete_with_warnings"
                else:
                    status = "complete"
            elif event.type == "task_remediation" and status not in {"complete", "complete_with_warnings", "failed"}:
                status = "remediation"
            elif event.type == "task_interrupted" and status not in {"complete", "complete_with_warnings", "failed"}:
                status = "interrupted"
            elif event.type == "task_failed" and status not in {"complete", "complete_with_warnings", "interrupted"}:
                status = "failed"
            elif event.type == "user_clarification_requested" and status == "running":
                status = "waiting_for_user"
            elif event.type == "society_resumed" and status == "waiting_for_user":
                status = "running"
        return status

    def _reconstruct_vote_resolution_task(
        self,
        task_id: str,
        events: list[SocietyEvent],
        pause_event: SocietyEvent,
    ) -> None:
        """Rebuild TaskRun, Team, and session state for a vote-resolution pause.

        The event payload carries the full proposals, team roster, leader, and
        tally so that ``apply_user_clarification`` and
        ``continue_after_clarification`` can resume without rerunning pre-vote
        phases.
        """

        prompt = None
        for event in events:
            if event.type == "task_received":
                prompt = event.payload.get("prompt")
                break
        if prompt is None:
            prompt = pause_event.payload.get("task_prompt")
        if prompt is None:
            return

        task = TaskRun(prompt=prompt, status="waiting_for_user")
        task.id = task_id
        task.created_at = events[0].created_at
        task.updated_at = events[-1].created_at
        self.tasks[task_id] = task

        team_member_ids = list(pause_event.payload.get("team_member_ids", []))
        team_leader_id = pause_event.payload.get("team_leader_id")
        if not team_member_ids:
            return
        voter_ids = list(pause_event.payload.get("voter_ids", [])) or list(team_member_ids[:4])
        team = Team(task_id=task_id, member_ids=team_member_ids, voter_ids=voter_ids, leader_id=team_leader_id, status="active")
        self.teams[team.id] = team
        task.team_id = team.id

        proposals_raw = pause_event.payload.get("proposals", {})
        proposals: dict[str, str] = {}
        for cid, entry in proposals_raw.items():
            if isinstance(entry, dict):
                proposals[cid] = str(entry.get("proposal", ""))
            else:
                proposals[cid] = str(entry)

        state = initial_session_state(prompt, [])
        state["task_id"] = task_id
        state["phase"] = "voted"
        state["resume_phase"] = "vote_resolution"
        state["vote_resolution"] = {
            "proposals": proposals,
            "team_member_ids": team_member_ids,
            "team_leader_id": team_leader_id,
            "tally": dict(pause_event.payload.get("tally", {})),
            "candidates": list(pause_event.payload.get("valid_candidate_ids", [])),
        }
        state["user_clarification"] = {
            "status": "requested",
            "question": pause_event.payload.get("question", ""),
            "resume_phase": "vote_resolution",
            "tally": pause_event.payload.get("tally", {}),
            "valid_candidate_ids": list(pause_event.payload.get("valid_candidate_ids", [])),
        }
        state["proposals"] = {
            cid: {"proposal": proposals.get(cid, ""), "proposal_id": f"prop-id-{cid}"}
            for cid in proposals
        }
        self.session_states[task_id] = state

    def _reconstruct_readiness_task(
        self,
        task_id: str,
        events: list[SocietyEvent],
        pause_event: SocietyEvent,
    ) -> None:
        """Rebuild a readiness-paused task so clarification survives restart."""

        received = next((event for event in events if event.type == "task_received"), None)
        prompt = received.payload.get("prompt") if received is not None else None
        if not isinstance(prompt, str) or not prompt.strip():
            return

        team_data: dict[str, Any] = {}
        for event in events:
            if event.type == "team_formed" and isinstance(event.payload.get("team"), dict):
                team_data = event.payload["team"]
                break
        raw_members = team_data.get("member_ids") or list(self.agents.keys())[:4]
        member_ids = raw_members.split() if isinstance(raw_members, str) else list(raw_members)
        member_ids = [agent_id for agent_id in member_ids if agent_id in self.agents]
        if not member_ids:
            return
        raw_voters = team_data.get("voter_ids") or member_ids[:4]
        voter_ids = raw_voters.split() if isinstance(raw_voters, str) else list(raw_voters)
        voter_ids = [agent_id for agent_id in voter_ids if agent_id in member_ids]

        task = TaskRun(prompt=prompt, status="waiting_for_user")
        task.id = task_id
        task.created_at = events[0].created_at
        task.updated_at = events[-1].created_at
        team = Team(
            task_id=task_id,
            member_ids=member_ids,
            voter_ids=voter_ids,
            leader_id=team_data.get("leader_id"),
            status="active",
        )
        if isinstance(team_data.get("id"), str):
            team.id = team_data["id"]
        task.team_id = team.id

        roster = [self.agents[agent_id].model_dump() for agent_id in member_ids]
        state = initial_session_state(prompt, roster)
        state.update({
            "task_id": task_id,
            "phase": "waiting_for_user",
            "resume_phase": pause_event.payload.get("resume_phase") or "post_readiness",
            "readiness_tally": {
                "blockers": list(pause_event.payload.get("blockers", [])),
                "open_questions": list(pause_event.payload.get("open_questions", [])),
            },
            "user_clarification": {"status": "requested", **pause_event.payload},
            "reputations": self._reputation_snapshot(member_ids),
        })

        self.tasks[task_id] = task
        self.teams[team.id] = team
        self.session_states[task_id] = state
        self.workflows[task_id] = build_governance_workflow(
            state,
            db=self.agno_db,
            routing_enabled=self.settings.workflow_routing_enabled,
        )
        if self.settings.llm_enabled:
            self.agno_teams[team.id] = build_society_team(
                [self.agents[agent_id] for agent_id in member_ids],
                self.settings,
                session_state=state,
                session_id=task_id,
            )

    def _reconstruct_team_composition_task(
        self,
        task_id: str,
        events: list[SocietyEvent],
        pause_event: SocietyEvent,
    ) -> None:
        """Rebuild a task paused during team composition so restart can resume once."""

        received = next((event for event in events if event.type == "task_received"), None)
        prompt = received.payload.get("prompt") if received is not None else pause_event.payload.get("task_prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return

        team_payload = None
        for event in events:
            if event.type == "team_formed" and isinstance(event.payload.get("team"), dict):
                team_payload = event.payload["team"]
                break
        member_ids = [str(agent_id) for agent_id in pause_event.payload.get("team_member_ids", []) if str(agent_id)]
        voter_ids = [str(agent_id) for agent_id in pause_event.payload.get("voter_ids", []) if str(agent_id)]
        leader_id = pause_event.payload.get("team_leader_id")
        team_id = pause_event.payload.get("team_id")
        if not member_ids and isinstance(team_payload, dict):
            member_ids = [str(agent_id) for agent_id in team_payload.get("member_ids", []) if str(agent_id)]
        if not voter_ids and isinstance(team_payload, dict):
            voter_ids = [str(agent_id) for agent_id in team_payload.get("voter_ids", []) if str(agent_id)]
        if leader_id is None and isinstance(team_payload, dict):
            leader_id = team_payload.get("leader_id")
        if not isinstance(team_id, str) and isinstance(team_payload, dict) and isinstance(team_payload.get("id"), str):
            team_id = team_payload["id"]
        if not member_ids:
            return

        task = TaskRun(prompt=prompt, status="waiting_for_user")
        task.id = task_id
        task.created_at = events[0].created_at
        task.updated_at = events[-1].created_at

        team = Team(
            task_id=task_id,
            member_ids=member_ids,
            voter_ids=voter_ids,
            leader_id=leader_id,
            status="active",
        )
        if isinstance(team_id, str):
            team.id = team_id
        task.team_id = team.id

        roster = [self.agents[agent_id].model_dump() for agent_id in member_ids if agent_id in self.agents]
        state = initial_session_state(prompt, roster)
        state.update({
            "task_id": task_id,
            "phase": "waiting_for_user",
            "resume_phase": "team_composition",
            "working_brief": pause_event.payload.get("working_brief"),
            "team_coordination_brief": pause_event.payload.get("team_coordination_brief"),
            "readiness_tally": copy.deepcopy(pause_event.payload.get("readiness_tally", {})),
            "user_clarification": {"status": "requested", **pause_event.payload},
            "reputations": self._reputation_snapshot(member_ids),
        })
        composition_state = pause_event.payload.get("composition_state")
        if isinstance(composition_state, dict):
            state["team_composition"] = copy.deepcopy(composition_state)
        state["composition_execution_roster"] = copy.deepcopy(
            state.get("team_composition", {}).get("materialized_agents", [])
        )

        self.tasks[task_id] = task
        self.teams[team.id] = team
        self.session_states[task_id] = state
        self.workflows[task_id] = build_governance_workflow(
            state,
            db=self.agno_db,
            routing_enabled=self.settings.workflow_routing_enabled,
        )
        if self.settings.llm_enabled:
            self.agno_teams[team.id] = build_society_team(
                [self.agents[agent_id] for agent_id in member_ids if agent_id in self.agents],
                self.settings,
                session_state=state,
                session_id=task_id,
            )

    def shutdown(self) -> None:
        """Mark every in-process running task as interrupted in the event log.

        Graceful shutdown must not leave running tasks persisted with no worker.
        This method emits truthful ``task_interrupted`` and ``task_failed``
        events for any task still marked ``running`` in memory, then updates
        their status so summaries report the interruption rather than a
        fabricated completion.
        """

        for task_id, task in list(self.tasks.items()):
            cancellation_event = self._composition_cancellation_events.get(task_id)
            if cancellation_event is not None:
                cancellation_event.set()
            if task.status != "running":
                continue
            state = self._state(task_id)
            phase = state.get("phase", "unknown")
            state["interruption"] = {
                "reason": "graceful_shutdown",
                "phase": phase,
                "requested_at": now_iso(),
            }
            task.status = "interrupted"
            task.updated_at = now_iso()
            self._emit(
                task_id,
                "task_interrupted",
                f"Task was interrupted during {phase} by graceful shutdown.",
                payload={"phase": phase, "reason": "graceful_shutdown"},
            )
            self._emit(
                task_id,
                "task_failed",
                f"Task interrupted by graceful shutdown during {phase}.",
                payload={"phase": phase, "error": "graceful_shutdown", "interrupted": True},
            )

    def list_events(self, task_id: str | None = None) -> list[SocietyEvent]:
        return self.events.list(task_id)

    def list_task_summaries(self) -> list[dict]:
        """List persisted tasks so previous runs can be replayed after restart."""

        current = {task.id: task.model_dump() for task in self.tasks.values()}
        persisted = self.events.task_summaries()
        merged: list[dict] = []
        seen: set[str] = set()
        for summary in persisted:
            task_id = summary["id"]
            merged.append(current.get(task_id, summary))
            seen.add(task_id)
        for task_id, task in current.items():
            if task_id not in seen:
                merged.insert(0, task)
        return merged

    def get_task_summary(self, task_id: str) -> dict | None:
        """Return an in-memory task or a persisted replay summary."""

        task = self.tasks.get(task_id)
        if task is not None:
            return task.model_dump()
        return self.events.task_summary(task_id)

    def get_agent_memory(self, agent_id: str) -> list[dict]:
        """Return live memory plus durable memory-write records for an agent."""

        records = self.events.agent_memory(agent_id)
        agent = self.agents.get(agent_id)
        if agent is not None:
            records.extend(
                {
                    "task_id": None,
                    "memory": item,
                    "tags": ["live"],
                    "created_at": None,
                    "mode": "in_memory",
                }
                for item in agent.memory
            )
        return records

    def metrics_summary(self) -> dict:
        """Return aggregate metrics from live records plus persisted JSONL events."""

        records = self.metrics.all()
        live_ids = {record.task_id for record in records}
        for event in self.events.list():
            if event.type != "task_metrics" or event.task_id in live_ids:
                continue
            try:
                records.append(TaskMetrics.model_validate(event.payload))
            except Exception:
                continue
        return summarize_task_metrics(records)

    def submit(self, prompt: str) -> TaskRun:
        task = TaskRun(prompt=prompt, status="running")
        self.tasks[task.id] = task
        self._emit(task.id, "task_received", "Problem entered the society queue.", payload={"prompt": prompt})
        return task

    async def run_task(self, task_id: str) -> None:
        task = self.tasks[task_id]
        start_time = time.time()
        try:
            if not self.settings.llm_enabled:
                raise GovernanceToolError("LLM-backed execution is required; deterministic society mode is disabled.")
            team = self._form_team(task)
            await self._run_governance_workflow(task)
            await self._run_pre_execution_conversation(task, team)
            if self.settings.pre_execution_conversation_enabled and self._state(task.id).get("ready_to_proceed") is not True:
                self._pause_for_user_clarification(task)
                return
            await self._execute_after_readiness(task, team, start_time)
        except GovernanceToolError as exc:
            task.status = "failed"
            task.updated_at = now_iso()
            state = self._state(task.id)
            self._record_failure_recovery(task.id, str(exc), state)
            diagnosis = self._diagnose_failure(task.id)
            failure_payload = {
                "phase": state.get("phase", "unknown"),
                "blocking_reason": f"Governance tool failure during {state.get('phase', 'unknown')}: {exc}",
                "missing_inputs": diagnosis["missing_inputs"],
                "missing_evidence": diagnosis["missing_evidence"],
                "system_error": str(exc),
                "recoverable": False,
            }
            self._emit(
                task.id,
                "run_failed",
                f"Run failed during {state.get('phase', 'unknown')}: {exc}",
                payload=failure_payload,
            )
            self._emit(
                task.id,
                "task_failed",
                f"Governance tool failure during {state.get('phase', 'unknown')}: {exc}",
                payload={
                    "phase": state.get("phase", "unknown"),
                    "error": str(exc),
                    "current_actor": state.get("current_actor"),
                },
            )
        except Exception as exc:
            task.status = "failed"
            task.updated_at = now_iso()
            state = self._state(task.id)
            self._record_failure_recovery(task.id, str(exc), state)
            diagnosis = self._diagnose_failure(task.id)
            failure_payload = {
                "phase": state.get("phase", "unknown"),
                "blocking_reason": str(exc),
                "missing_inputs": diagnosis["missing_inputs"],
                "missing_evidence": diagnosis["missing_evidence"],
                "system_error": str(exc),
                "recoverable": False,
            }
            self._emit(
                task.id,
                "run_failed",
                f"Run failed with system error: {exc}",
                payload=failure_payload,
            )
            self._emit(
                task.id,
                "task_failed",
                str(exc),
                payload={
                    "phase": state.get("phase", "unknown"),
                    "error": str(exc),
                    "current_actor": state.get("current_actor"),
                },
            )

    async def _execute_after_readiness(self, task: TaskRun, team: Team, start_time: float) -> None:
        """Continue the society lifecycle after readiness is satisfied."""

        try:
            task.status = "running"
            task.updated_at = now_iso()
            await self._run_agno_team(task, team)
            await self._elect_leader(task, team)
            if not self.settings.efficient_society_enabled:
                await self._spawn_child_agent(task, team)
            else:
                self._state(task.id)["spawn_decision"] = {
                    "spawn": False,
                    "reason": "Defer spawning until a typed capability gap or validation gate exists.",
                }
                self._emit(
                    task.id,
                    "efficient_phase_skipped",
                    "A speculative specialist-spawn call was skipped.",
                    payload={"phase": "spawn_decision", "reason": "defer_until_typed_capability_gap"},
                )
            composition_status = await self._run_team_composition_phase(task, team)
            if composition_status == "paused":
                return
            if composition_status == "failed":
                return
            if composition_status != "completed":
                await self._delegate_subtasks(task, team)
            await self._continue_after_execution(task, team, start_time)
        except GovernanceToolError as exc:
            task.status = "failed"
            task.updated_at = now_iso()
            state = self._state(task.id)
            self._record_failure_recovery(task.id, str(exc), state)
            diagnosis = self._diagnose_failure(task.id)
            failure_payload = {
                "phase": state.get("phase", "unknown"),
                "blocking_reason": f"Governance tool failure during {state.get('phase', 'unknown')}: {exc}",
                "missing_inputs": diagnosis["missing_inputs"],
                "missing_evidence": diagnosis["missing_evidence"],
                "system_error": str(exc),
                "recoverable": False,
            }
            self._emit(
                task.id,
                "run_failed",
                f"Run failed during {state.get('phase', 'unknown')}: {exc}",
                payload=failure_payload,
            )
            self._emit(
                task.id,
                "task_failed",
                f"Governance tool failure during {state.get('phase', 'unknown')}: {exc}",
                payload={
                    "phase": state.get("phase", "unknown"),
                    "error": str(exc),
                    "current_actor": state.get("current_actor"),
                },
            )
        except Exception as exc:
            task.status = "failed"
            task.updated_at = now_iso()
            state = self._state(task.id)
            self._record_failure_recovery(task.id, str(exc), state)
            diagnosis = self._diagnose_failure(task.id)
            failure_payload = {
                "phase": state.get("phase", "unknown"),
                "blocking_reason": str(exc),
                "missing_inputs": diagnosis["missing_inputs"],
                "missing_evidence": diagnosis["missing_evidence"],
                "system_error": str(exc),
                "recoverable": False,
            }
            self._emit(
                task.id,
                "run_failed",
                f"Run failed with system error: {exc}",
                payload=failure_payload,
            )
            self._emit(
                task.id,
                "task_failed",
                str(exc),
                payload={
                    "phase": state.get("phase", "unknown"),
                    "error": str(exc),
                    "current_actor": state.get("current_actor"),
                },
            )

    async def _complete_after_vote(
        self,
        task: TaskRun,
        team: Team,
        winner: str,
        proposals: dict[str, str],
        start_time: float,
    ) -> None:
        """Run the post-vote completion path: monitor, answer, metrics, learning, dissolve."""

        efficient_mode = bool(getattr(getattr(self, "settings", None), "efficient_society_enabled", False))
        if not (
            efficient_mode and self._state(task.id).get("critique_source") == "lean_counterproposal"
        ):
            await self._monitor(task, team, winner, proposals)
        else:
            self._emit(
                task.id,
                "efficient_phase_skipped",
                "The recorded critic counterproposal already supplied peer review.",
                payload={"phase": "monitor", "reason": "lean_counterproposal_review"},
            )
        task.final_answer = self._compose_answer(task, team, winner, proposals)
        self._record_demo_proof(task, team)
        self._state(task.id)["prompt"] = task.prompt
        self._populate_acceptance_checks(task.id, team)
        await self._run_independent_validator(task, team)
        task.final_answer = self._apply_validation_gate(task, team)
        await self._record_evaluation_metrics(task, team, winner)
        await self._learn(task, team, winner)
        self._dissolve(task, team)

        state = self._state(task.id)
        proof = state.get("demo_proof") or {}
        proof_verified = bool(proof.get("verified"))
        missing_markers = list(proof.get("missing_markers", []))
        evidence = state.get("acceptance_evidence") or {}
        terminal = evidence.get("terminal_status", "complete")
        required_failures = list(evidence.get("required_failures", []))
        optional_failures = list(evidence.get("optional_failures", []))

        task.updated_at = now_iso()
        self._record_task_metrics(task, start_time)

        if terminal == "complete":
            task.status = "complete"
            self._emit(
                task.id,
                "task_complete",
                "The society produced a final answer.",
                payload={
                    "answer": task.final_answer,
                    "demo_proof_verified": proof_verified,
                    "missing_markers": missing_markers,
                    "acceptance_status": "complete",
                },
            )
        elif terminal == "complete_with_warnings":
            task.status = "complete_with_warnings"
            self._emit(
                task.id,
                "task_complete",
                "The society produced a final answer with warnings.",
                payload={
                    "answer": task.final_answer,
                    "demo_proof_verified": proof_verified,
                    "missing_markers": missing_markers,
                    "acceptance_status": "complete_with_warnings",
                    "optional_failures": optional_failures,
                },
            )
        elif terminal == "remediation":
            task.status = "remediation"
            self._emit(
                task.id,
                "task_remediation",
                "Required acceptance checks failed; remediation is possible.",
                payload={
                    "answer": task.final_answer,
                    "required_failures": required_failures,
                    "recoverable": True,
                    "acceptance_status": "remediation",
                },
            )
        else:
            task.status = "failed"
            diagnosis = self._diagnose_failure(task.id)
            self._emit(
                task.id,
                "run_failed",
                "Required acceptance checks failed with no recovery path.",
                payload={
                    "phase": state.get("phase", "validation"),
                    "blocking_reason": f"Required acceptance checks failed: {', '.join(required_failures)}",
                    "missing_inputs": diagnosis["missing_inputs"],
                    "missing_evidence": required_failures,
                    "system_error": None,
                    "recoverable": False,
                },
            )
            self._emit(
                task.id,
                "task_failed",
                f"Acceptance evidence exhausted: {', '.join(required_failures)}",
                payload={
                    "phase": state.get("phase", "validation"),
                    "error": f"Required acceptance checks failed: {', '.join(required_failures)}",
                    "required_failures": required_failures,
                    "acceptance_status": "failed",
                    "current_actor": state.get("current_actor"),
                },
            )

    def _pause_for_user_clarification(self, task: TaskRun) -> None:
        """Pause a run and expose the readiness blocker as a user prompt."""

        state = self._state(task.id)
        blockers = state.get("readiness_tally", {}).get("blockers", [])
        open_questions = []
        brief = state.get("working_brief")
        if isinstance(brief, dict):
            open_questions = list(brief.get("open_questions", []))
        prompt = (
            str(blockers[0])
            if blockers
            else str(open_questions[0])
            if open_questions
            else "Clarify the missing task detail before the society proceeds."
        )
        payload = {
            "question": prompt,
            "blockers": blockers,
            "open_questions": open_questions,
            "resume_phase": "post_readiness",
        }
        state["resume_phase"] = "post_readiness"
        state["user_clarification"] = {"status": "requested", **payload}
        task.status = "waiting_for_user"
        task.updated_at = now_iso()
        self._emit(
            task.id,
            "user_clarification_requested",
            "The society needs user clarification before continuing.",
            payload=payload,
        )

    def _pause_for_vote_resolution(
        self,
        task: TaskRun,
        team: Team,
        proposals: dict[str, str],
        tally: dict[str, int],
        candidates: list[str],
        reason: str = "no_valid_winner",
        tied_candidates: list[str] | None = None,
    ) -> None:
        """Pause after a vote with no clear winner; the elected leader asks the user to choose."""

        state = self._state(task.id)
        leader_id = team.leader_id or team.member_ids[0]
        leader = self.agents[leader_id]
        proposal_summaries = []
        for cid in candidates:
            entry = proposals.get(cid, "")
            if isinstance(entry, dict):
                text = str(entry.get("proposal", ""))
            else:
                text = str(entry)
            proposal_summaries.append({"candidate_id": cid, "proposal_summary": text[:200]})
        if reason == "tie" and tied_candidates:
            question = (
                f"{leader.name} reports: the vote ended in a tie among "
                f"{', '.join(tied_candidates)}. "
                f"Please select one candidate agent id to proceed."
            )
        elif reason == "empty_tally":
            question = (
                f"{leader.name} reports: no ballots were recorded, so there is no winner. "
                f"Please select one candidate agent id from {', '.join(candidates)} to proceed."
            )
        else:
            question = (
                f"{leader.name} reports: the tally did not produce a valid winner. "
                f"Tally: {tally}. "
                f"Please select one candidate agent id from {', '.join(candidates)} to proceed."
            )
        payload = {
            "question": question,
            "tally": tally,
            "valid_candidate_ids": list(candidates),
            "proposal_summaries": proposal_summaries,
            "reason": reason,
            "tied_candidates": list(tied_candidates) if tied_candidates else [],
            "leader_id": leader_id,
            "resume_phase": "vote_resolution",
            "proposals": {cid: proposals[cid] for cid in candidates},
            "team_member_ids": list(team.member_ids),
            "voter_ids": self._voter_ids(team),
            "team_leader_id": team.leader_id,
            "task_prompt": task.prompt,
        }
        state["resume_phase"] = "vote_resolution"
        state["vote_resolution"] = {
            "proposals": {cid: proposals[cid] for cid in candidates},
            "team_member_ids": list(team.member_ids),
            "voter_ids": self._voter_ids(team),
            "team_leader_id": team.leader_id,
            "tally": tally,
            "candidates": list(candidates),
        }
        state["user_clarification"] = {"status": "requested", **payload}
        task.status = "waiting_for_user"
        task.updated_at = now_iso()
        self._emit(
            task.id,
            "user_clarification_requested",
            "The elected leader needs the user to break a vote deadlock.",
            payload=payload,
        )

    def apply_user_clarification(self, task_id: str, answer: str) -> TaskRun:
        """Inject a user clarification into a paused task."""

        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status != "waiting_for_user":
            raise ValueError(f"Task is not waiting for clarification: {task.status}")
        state = self._state(task_id)
        team = self.teams.get(task.team_id or "")
        if team is None:
            raise ValueError("Task has no active team to resume")

        resume_phase = state.get("resume_phase") or "post_readiness"

        if resume_phase == "vote_resolution":
            vote_resolution = state.get("vote_resolution") or {}
            valid_candidates = list(vote_resolution.get("candidates", []))
            chosen = answer.strip()
            if chosen not in valid_candidates:
                raise ValueError(
                    f"Answer '{answer}' is not a valid candidate agent id. "
                    f"Valid candidates: {valid_candidates}"
                )
            state["winner_id"] = chosen
            state["phase"] = "voted"
            clarification = {
                "status": "answered",
                "answer": chosen,
                "answered_at": now_iso(),
                "resume_phase": "vote_resolution",
            }
            state["user_clarification"] = clarification
            self._emit(
                task.id,
                "user_decision_resolved",
                f"User selected {self.agents[chosen].name} to break the vote deadlock.",
                actor=chosen,
                payload={
                    "selected_winner": chosen,
                    "valid_candidate_ids": valid_candidates,
                    "tally": vote_resolution.get("tally", {}),
                },
            )
            self._emit(
                task.id,
                "society_resumed",
                "The society resumed from vote resolution.",
                payload={"resume_phase": "vote_resolution"},
            )
            task.status = "running"
            task.updated_at = now_iso()
            return task

        clarification = {
            "status": "answered",
            "answer": answer,
            "answered_at": now_iso(),
            "resume_phase": resume_phase,
        }
        state["user_clarification"] = clarification
        state["ready_to_proceed"] = True
        if not isinstance(state.get("working_brief"), dict):
            state["working_brief"] = {
                "summary": task.prompt[:300],
                "agreed_scope": task.prompt[:200],
                "success_criteria": [],
                "constraints": [],
                "open_questions": [],
                "blocked_items": [],
                "unresolved_dissent": [],
                "assumptions": [],
                "question_answers": [],
                "confidence": 0.5,
            }
        if isinstance(state["working_brief"], dict):
            state["working_brief"].setdefault("constraints", []).append(f"User clarification: {answer}")
            state["working_brief"].setdefault("assumptions", []).append(f"Clarified by user: {answer}")
            state["working_brief"]["confidence"] = max(float(state["working_brief"].get("confidence", 0.5)), 0.75)
        self._emit(
            task.id,
            "user_clarification_answered",
            "User clarification was added to the working brief.",
            payload=clarification,
        )
        self._emit(
            task.id,
            "society_resumed",
            "The society resumed from the blocked readiness phase.",
            payload={"resume_phase": clarification["resume_phase"]},
        )
        task.status = "running"
        task.updated_at = now_iso()
        return task

    async def continue_after_clarification(self, task_id: str) -> TaskRun:
        """Continue a task after user clarification has been applied."""

        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        team = self.teams.get(task.team_id or "")
        if team is None:
            raise ValueError("Task has no active team to resume")
        state = self._state(task_id)
        resume_phase = state.get("resume_phase")
        if resume_phase == "vote_resolution":
            vote_resolution = state.get("vote_resolution") or {}
            raw_proposals = vote_resolution.get("proposals", {})
            proposals: dict[str, str] = {}
            for cid, entry in raw_proposals.items():
                if isinstance(entry, dict):
                    proposals[cid] = str(entry.get("proposal", ""))
                else:
                    proposals[cid] = str(entry)
            winner = state.get("winner_id")
            if winner is None:
                raise ValueError("No winner set after vote resolution")
            await self._complete_after_vote(task, team, winner, proposals, time.time())
            return task
        if resume_phase == "team_composition":
            composition = self._team_composition_state(task_id)
            if composition.get("resume_used"):
                raise ValueError("Team composition clarification has already been resumed once")
            composition["resume_used"] = True
            composition["status"] = "resuming"
            outcome = await self._run_team_composition_phase(task, team, allow_started_resume=True)
            if outcome == "completed":
                await self._continue_after_execution(task, team, time.time())
            return task
        await self._execute_after_readiness(task, team, time.time())
        return task

    async def resume_with_clarification(self, task_id: str, answer: str) -> TaskRun:
        """Inject a user clarification and resume from the paused phase."""

        task = self.apply_user_clarification(task_id, answer)
        await self.continue_after_clarification(task_id)
        return task

    def _form_team(self, task: TaskRun) -> Team:
        member_ids = list(self.agents.keys())[:4]
        voter_ids: list[str] = []
        for agent_id in member_ids:
            registration = get_role_capabilities(resolve_role_key(self.agents[agent_id]))
            if registration is not None and registration.can_vote:
                voter_ids.append(agent_id)
        team = Team(task_id=task.id, member_ids=member_ids, voter_ids=voter_ids, status="active")
        self.teams[team.id] = team
        task.team_id = team.id
        roster = [self.agents[agent_id].model_dump() for agent_id in member_ids]
        state = initial_session_state(task.prompt, roster)
        state["task_id"] = task.id
        state["phase"] = "formed"
        state["reputations"] = self._reputation_snapshot(member_ids)
        self.session_states[task.id] = state
        self.workflows[task.id] = build_governance_workflow(
            state,
            db=self.agno_db,
            routing_enabled=self.settings.workflow_routing_enabled,
        )
        if self.settings.llm_enabled:
            self.agno_teams[team.id] = build_society_team(
                [self.agents[agent_id] for agent_id in member_ids],
                self.settings,
                session_state=state,
                session_id=task.id,
            )
        self._emit(task.id, "team_formed", "A temporary team formed for this problem.", payload={"team": team.model_dump()})
        for agent_id in member_ids:
            registration = get_role_capabilities(resolve_role_key(self.agents[agent_id]))
            self._emit(
                task.id,
                "role_selected",
                f"{self.agents[agent_id].name} joined for registered capabilities.",
                actor=agent_id,
                payload={
                    "agent_id": agent_id,
                    "role_key": registration.role if registration else resolve_role_key(self.agents[agent_id]),
                    "capabilities": registration.capabilities if registration else [],
                    "can_vote": agent_id in voter_ids,
                    "selection_reason": registration.description if registration else "existing team identity",
                },
            )
        return team

    @staticmethod
    def _voter_ids(team: Team) -> list[str]:
        """Return explicit voters, falling back for legacy persisted teams."""

        return list(team.voter_ids or team.member_ids)

    def _benchmark_equivalent_tie_winner(
        self,
        team: Team,
        proposals: dict[str, str],
        tied_candidates: list[str],
    ) -> str | None:
        """Resolve only exact-content ties when a benchmark cannot ask a user."""

        if not self.settings.benchmark_suite_tools_enabled or len(tied_candidates) < 2:
            return None
        canonical: list[str] = []
        for candidate_id in tied_candidates:
            proposal = proposals.get(candidate_id)
            if proposal is None:
                return None
            try:
                parsed = json.loads(proposal)
                canonical.append(json.dumps(parsed, sort_keys=True, separators=(",", ":")))
            except (json.JSONDecodeError, TypeError):
                canonical.append(str(proposal).strip())
        if len(set(canonical)) != 1:
            return None
        if team.leader_id in tied_candidates:
            return team.leader_id
        return next(
            (candidate_id for candidate_id in team.member_ids if candidate_id in tied_candidates),
            tied_candidates[0],
        )

    async def _run_governance_workflow(self, task: TaskRun) -> None:
        """Run the six-step Agno Workflow lifecycle spine for a task."""

        workflow = self.workflows.get(task.id)
        if workflow is None:
            return
        try:
            output = await asyncio.to_thread(
                workflow.run,
                input={"task_id": task.id, "prompt": task.prompt},
                session_id=task.id,
                session_state=self._state(task.id),
            )
        except Exception as exc:
            if self.settings.llm_enabled:
                raise GovernanceToolError(f"Workflow execution failed: {exc}") from exc
            self._emit(task.id, "workflow_checkpoint_failed", "Deterministic workflow execution failed.", payload={"error": str(exc)})
            return
        for checkpoint in self._state(task.id).get("workflow_checkpoints", []):
            self._emit(task.id, "workflow_checkpoint", f"Workflow checkpoint reached: {checkpoint['phase']}", payload=checkpoint)
        self._emit(task.id, "workflow_completed", "Agno Workflow executed the governance lifecycle spine.", payload={
            "status": str(getattr(output, "status", "unknown")),
        })

    def _parse_coordination_brief(self, content: str) -> dict[str, Any]:
        """Extract a structured coordination brief from raw team output.

        This is the fallback path when the Agno Team does not return a typed
        ``TeamCoordinationBrief`` via ``output_schema``. It attempts JSON
        parsing and extracts known fields; otherwise it returns a minimal
        dict with the raw content as ``summary``.
        """

        parsed = _parse_json_response(content)
        if parsed and isinstance(parsed, dict):
            confidence = parsed.get("confidence")
            return {
                "summary": str(parsed.get("summary", content[:300])),
                "proposed_subtasks": list(parsed.get("proposed_subtasks", [])),
                "open_questions": list(parsed.get("open_questions", [])),
                "recommended_focus": str(parsed.get("recommended_focus", "")),
                "confidence": float(confidence) if isinstance(confidence, (int, float)) else 0.5,
                "risk_signals": list(parsed.get("risk_signals", [])),
                "evidence_gaps": list(parsed.get("evidence_gaps", [])),
                "assumptions": list(parsed.get("assumptions", [])),
                "delegation_hints": list(parsed.get("delegation_hints", [])),
            }
        return {
            "summary": content[:500],
            "proposed_subtasks": [],
            "open_questions": [],
            "recommended_focus": "",
            "confidence": 0.5,
            "risk_signals": [],
            "evidence_gaps": [],
            "assumptions": [],
            "delegation_hints": [],
        }

    def _build_coordination_brief_from_response(
        self,
        response: Any,
        raw_content: str,
    ) -> TeamCoordinationBrief:
        """Extract a typed ``TeamCoordinationBrief`` from an Agno Team response.

        When the team was configured with ``output_schema=TeamCoordinationBrief``,
        ``response.content`` is already a validated instance. Otherwise, fall
        back to JSON parsing of the raw content string.
        """

        content = getattr(response, "content", response)
        if isinstance(content, TeamCoordinationBrief):
            return content
        if isinstance(content, dict):
            try:
                return TeamCoordinationBrief.model_validate(content)
            except Exception:
                pass
        parsed = self._parse_coordination_brief(raw_content)
        return TeamCoordinationBrief.model_validate(parsed)

    async def _run_agno_team(self, task: TaskRun, team: Team) -> None:
        """Invoke the Agno Team in LLM mode so coordination uses TeamMode.

        The team is configured with ``output_schema=TeamCoordinationBrief`` so
        the response content is a typed, truth-useful artifact. The structured
        brief is stored in session state and emitted as an event so delegation,
        working brief construction, and failure diagnostics can consume it
        directly.
        """

        agno_team = self.agno_teams.get(team.id)
        if agno_team is None or not self.settings.llm_enabled:
            return
        prompt = (
            f"Task: {task.prompt}\n"
            "Run one bounded coordination pass as the Qwendom society. "
            "Produce a structured TeamCoordinationBrief with summary, "
            "proposed_subtasks (each with agent_id, subtask, why_assigned, "
            "done_criteria, blocking_if_missing), open_questions, "
            "recommended_focus, confidence, risk_signals, evidence_gaps, "
            "assumptions, and delegation_hints."
        )
        try:
            response = await self._call_provider(
                lambda: agno_team.arun(
                    prompt,
                    session_id=task.id,
                    session_state=self._state(task.id),
                    add_session_state_to_context=True,
                ),
                task_id=task.id,
                actor="team",
                operation="team_coordination",
                timeout_seconds=max(self.settings.llm_timeout_seconds, 120),
            )
        except Exception as exc:
            brief = TeamCoordinationBrief(
                summary=task.prompt[:500],
                proposed_subtasks=[],
                open_questions=[],
                recommended_focus="Proceed from the working brief because team coordination was unavailable.",
                confidence=0.35,
                risk_signals=[f"Agno Team coordination unavailable: {type(exc).__name__}"],
                evidence_gaps=["Team coordination did not return a structured brief."],
                assumptions=["Use the pre-execution working brief as the source of truth."],
                delegation_hints=[],
            )
            brief_dict = brief.model_dump()
            self._state(task.id)["team_coordination_brief"] = brief_dict
            self._emit(task.id, "agno_team_ran", "Agno Team coordination was unavailable; continuing from the working brief.", payload={
                "content": str(exc)[:500],
                "team_coordination_brief": brief_dict,
                "recovered": True,
            })
            return
        self._capture_model_usage(task.id, response, "agno_team", team.leader_id or "team")
        raw_content = str(getattr(response, "content", response))[:1200]
        try:
            brief = self._build_coordination_brief_from_response(response, raw_content)
        except Exception as exc:
            raise GovernanceToolError(f"Coordination brief validation failed: {type(exc).__name__}: {exc!r}") from exc
        brief_dict = brief.model_dump()
        self._state(task.id)["team_coordination_brief"] = brief_dict
        self._emit(task.id, "agno_team_ran", "Agno Team coordinated the task context.", payload={
            "content": raw_content,
            "team_coordination_brief": brief_dict,
        })

    def _state(self, task_id: str) -> dict[str, Any]:
        """Return the mutable V3 session_state for a task."""

        return self.session_states.setdefault(task_id, initial_session_state("", []))

    def _safe_list_events(self, task_id: str) -> list[SocietyEvent]:
        """List events for a task, returning [] if the store is unavailable.

        Unit tests may construct the orchestrator without a real EventStore.
        This helper keeps production behavior intact while preventing
        AttributeError in test contexts.
        """

        try:
            return self.events.list(task_id)
        except Exception:
            return []

    def _team_composition_state(self, task_id: str) -> dict[str, Any]:
        """Return additive durable state for the typed composition executor."""

        state = self._state(task_id)
        current = state.get("team_composition")
        if not isinstance(current, dict):
            current = {}
        defaults = {
            "enabled": self._should_run_team_composition(task_id),
            "status": "not_started",
            "attempt_count": 0,
            "recomposed": False,
            "validation_issue_history": [],
            "selected_plan": None,
            "context_snapshot": None,
            "execution_result": None,
            "materialized_agents": [],
            "availability_blockers": [],
            "blocked": None,
            "resume_used": False,
        }
        for key, value in defaults.items():
            current.setdefault(key, copy.deepcopy(value))
        current["enabled"] = self._should_run_team_composition(task_id)
        state["team_composition"] = current
        state.setdefault("composition_execution_roster", [])
        return current

    def _should_run_team_composition(self, task_id: str | None = None, *, allow_started_resume: bool = False) -> bool:
        """Route new product missions to fixed specialists and preserve legacy replay.

        Fixed-specialist composition is the only production path for supported
        deliverables.  It intentionally remains selected when a provider or
        runtime prerequisite is unavailable so the composition phase can emit
        its typed terminal failure instead of falling through to the legacy
        demo delegation seam.  Explicit legacy-composer plans retain their
        existing flag-controlled behavior for historical replay and tests.
        """

        if allow_started_resume and task_id and self._is_legacy_composition_resume(task_id):
            return bool(self.settings.llm_enabled)
        return True

    def _is_legacy_composition_resume(self, task_id: str) -> bool:
        """Return whether durable replay state proves an old composer may resume."""

        state = self.session_states.get(task_id, {})
        composition = state.get("team_composition") if isinstance(state, dict) else None
        return bool(
            isinstance(state, dict)
            and state.get("resume_phase") == "team_composition"
            and isinstance(composition, dict)
            and composition.get("selection_strategy") == "legacy_composer"
        )

    def _composition_artifact_root(self, task_id: str) -> Path:
        """Return the per-task artifact directory for composition execution."""

        return Path(__file__).resolve().parent / "data" / "composition" / task_id

    def _team_composition_message(self, event_type: str, payload: dict[str, Any]) -> str:
        """Render concise human-readable event messages for composition events."""

        messages = {
            "team_composition_proposed": "Typed team composition proposed a candidate plan.",
            "team_composition_validated": "Typed team composition validated the execution plan.",
            "team_recomposition_requested": "Typed team composition requested one bounded recomposition.",
            "team_composition_blocked": "Typed team composition could not proceed safely.",
            "composition_assignment_materialized": "A bounded composition specialist identity was materialized.",
            "specialist_selection_proposed": "The elected leader proposed a fixed specialist team.",
            "specialist_selection_rejected": "The fixed specialist selection was rejected with typed blockers.",
            "specialist_selection_accepted": "The elected leader selected a verified fixed specialist team.",
            "specialist_invocation_approved": "The elected leader approved a selected specialist assignment.",
            "specialist_invocation_started": "An approved specialist assignment started after its dependencies cleared.",
            "work_node_started": f"Work node {payload.get('node_id', '?')} started.",
            "work_node_blocked": f"Work node {payload.get('node_id', '?')} is blocked.",
            "work_node_failed": f"Work node {payload.get('node_id', '?')} failed.",
            "work_node_retry_scheduled": f"Work node {payload.get('node_id', '?')} scheduled a retry.",
            "work_node_canceled": f"Work node {payload.get('node_id', '?')} was canceled.",
            "work_node_completed": f"Work node {payload.get('node_id', '?')} completed.",
            "artifact_validated": "A composed artifact passed independent validation.",
            "composition_assignment_cleanup_warning": "Composition runtime reported a cleanup warning.",
            "composition_assignment_cleanup_completed": "Composition runtime closed the assignment sandbox.",
        }
        return messages.get(event_type, event_type.replace("_", " "))

    def _emit_team_composition_event(self, task_id: str, event_type: str, payload: dict[str, Any]) -> None:
        """Persist a composition/runtime event into the existing SocietyEvent log."""

        event_payload = copy.deepcopy(payload)
        if event_type == "agentbay_artifact_exported":
            artifact_ref = event_payload.get("artifact_ref")
            if isinstance(artifact_ref, dict):
                local_path = artifact_ref.pop("path", None)
                if isinstance(local_path, str) and local_path:
                    artifact_root = self._composition_artifact_root(task_id).resolve()
                    candidate = Path(local_path).resolve()
                    if candidate != artifact_root and artifact_root in candidate.parents:
                        artifact_ref["storage_path"] = candidate.relative_to(artifact_root).as_posix()
        actor = event_payload.get("agent_id")
        actor_id = actor if isinstance(actor, str) and actor else None
        self._emit(
            task_id,
            event_type,
            self._team_composition_message(event_type, event_payload),
            actor=actor_id,
            payload=event_payload,
        )

    def _create_composition_runtime(self, task_id: str) -> CompositionRuntime:
        """Create the production composition runtime with the orchestrator event sink."""

        return CompositionRuntime(
            self.settings,
            self._composition_artifact_root(task_id),
            event_sink=lambda event_type, payload: self._emit_team_composition_event(task_id, event_type, dict(payload)),
        )

    def _create_team_composer(self, task_id: str) -> TeamComposer:
        """Create the production typed team composer for one task."""

        provider = AgnoTeamPlanProvider(model=build_model(self.settings))
        return TeamComposer(
            provider,
            lambda event_type, payload: self._emit_team_composition_event(task_id, event_type, dict(payload)),
        )

    async def _select_fixed_specialist_plan(
        self,
        task: TaskRun,
        team: Team,
        context: CompositionContext,
        runtime: CompositionRuntime,
    ) -> TeamCompositionResult:
        """Have the elected leader select immutable specialists without a composer.

        The first call lists the runtime-available repository catalog. The
        second call accepts only assignment fields controlled by the leader.
        Resolution attaches tools, skills, resource policy, and replay hashes
        after schema validation. One bounded correction is allowed.
        """

        leader = self.agents.get(team.leader_id or "")
        if leader is None:
            raise GovernanceToolError("Fixed specialist selection requires an elected leader.")
        coordinator = FixedSpecialistCoordinator(
            runtime.available_tool_ids().tool_ids,
            lambda event_type, payload: self._emit_team_composition_event(task.id, event_type, dict(payload)),
        )
        catalog = coordinator.list_specialists(ListSpecialistsCall())
        state = self._state(task.id)
        state["fixed_specialist_catalog"] = [entry.model_dump(mode="json") for entry in catalog]
        # Keep the leader's catalog-tool invocation as governance evidence, but
        # never use model-returned catalog content as routing authority. The
        # repository-resolved catalog above is the complete selectable set.
        await self._run_governance_tool(
            task,
            leader,
            list_specialists_tool,
            "list_specialists",
            ListSpecialistsResult,
            "List the fixed specialist templates available for this task before selecting a team.",
            ["Do not invent or modify catalog entries."],
        )

        authoritative_catalog = {"specialists": state["fixed_specialist_catalog"]}
        valid_template_ids = sorted(entry.template_id for entry in catalog)
        blockers: list[dict[str, Any]] = []
        resolved = None
        for attempt in (1, 2):
            correction = (
                "\nThe previous selection was rejected. Correct only these blockers: "
                + json.dumps(blockers, sort_keys=True)
                + f". Valid template IDs are exactly: {json.dumps(valid_template_ids)}."
                if blockers else ""
            )
            prompt = (
                "Select the smallest capable fixed-specialist team for the task. "
                "You may provide only template_id, assignment_id, objective, depends_on, "
                "owned_artifacts, and acceptance_requirements. Never provide tools, skills, "
                "credentials, locks, providers, or sandbox policy.\n"
                f"Task: {task.prompt}\n"
                f"Acceptance requirements: {json.dumps(context.acceptance_requirements)}\n"
                f"Authoritative repository catalog: {json.dumps(authoritative_catalog, sort_keys=True)}"
                f"{correction}"
            )
            selection_call = await self._run_governance_tool(
                task,
                leader,
                select_specialists_tool,
                "select_specialists",
                SelectSpecialistsCall,
                prompt,
                ["Builder artifacts requiring acceptance must have a dependent Test Engineer assignment."],
            )
            try:
                resolved = await coordinator.select_specialists(
                    selection_call,
                    task_summary=context.task_summary,
                    required_capabilities=context.required_capabilities,
                    required_acceptance_requirements=context.acceptance_requirements,
                )
                break
            except SpecialistSelectionError as exc:
                blockers = [blocker.model_dump(mode="json") for blocker in exc.blockers]
                if attempt == 2:
                    raise

        if resolved is None:  # pragma: no cover - defensive; loop either resolves or raises.
            raise GovernanceToolError("Fixed specialist selection produced no resolved plan.")
        if hasattr(runtime, "set_invocation_hook"):
            async def invoke_when_ready(node: Any, assignment: Any) -> None:
                await coordinator.invoke_specialist(
                    InvokeSpecialistCall(assignment_id=assignment.id),
                    satisfied_dependency_ids=node.depends_on,
                )

            runtime.set_invocation_hook(invoke_when_ready)
        state["fixed_specialist_selection"] = {
            "assignments": [item.model_dump(mode="json") for item in resolved.assignments],
            "strategy": "fixed_specialists",
        }
        return TeamCompositionResult(
            plan=resolved.plan,
            attempt_count=2 if blockers else 1,
            recomposed=bool(blockers),
            validation_issue_history=[],
        )

    @staticmethod
    def _serialize_composition_context(context: Any) -> dict[str, Any] | None:
        """Return a JSON-safe context snapshot for durable pause/resume state."""

        try:
            if isinstance(context, BaseModel):
                return context.model_dump(mode="json")
            if isinstance(context, dict):
                return json.loads(json.dumps(context))
        except (TypeError, ValueError):
            return None
        return None

    def _restore_composition_context_snapshot(self, task_id: str) -> CompositionContext | None:
        """Restore a persisted composition context snapshot when it validates."""

        composition = self._team_composition_state(task_id)
        snapshot = composition.get("context_snapshot")
        if not isinstance(snapshot, dict):
            return None
        try:
            return CompositionContext.model_validate(copy.deepcopy(snapshot))
        except ValidationError:
            return None

    def _build_team_composition_context(
        self,
        task: TaskRun,
        runtime: CompositionRuntime,
        *,
        allow_snapshot_resume: bool = False,
        include_user_clarification: bool = False,
    ) -> CompositionContext:
        """Build the truthful runtime composition context from current session state."""

        state = self._state(task.id)
        if allow_snapshot_resume:
            restored = self._restore_composition_context_snapshot(task.id)
            if restored is not None:
                context = restored.model_copy(deep=True)
                if include_user_clarification:
                    clarification = state.get("user_clarification") if isinstance(state.get("user_clarification"), dict) else {}
                    answer = clarification.get("answer")
                    if isinstance(answer, str) and answer.strip():
                        note = f"User clarification: {answer.strip()}"
                        if note not in context.unresolved_user_requirements:
                            context.unresolved_user_requirements.append(note)
                return context
        brief = state.get("working_brief") if isinstance(state.get("working_brief"), dict) else {}
        coordination = state.get("team_coordination_brief") if isinstance(state.get("team_coordination_brief"), dict) else {}
        acceptance_requirements: list[str] = []
        acceptance_requirements.extend(str(item) for item in brief.get("success_criteria", []) if str(item).strip())
        acceptance_requirements.extend(str(item) for item in coordination.get("dependencies", []) if str(item).strip())
        required_capabilities: list[str] = []
        seen_capabilities: set[str] = set()
        for item in coordination.get("proposed_subtasks", []):
            if not isinstance(item, dict):
                continue
            for capability in item.get("required_capabilities", []):
                text = str(capability).strip()
                if text and text not in seen_capabilities:
                    seen_capabilities.add(text)
                    required_capabilities.append(text)
        readiness = state.get("readiness_tally") if isinstance(state.get("readiness_tally"), dict) else {}
        unresolved_user_requirements = [
            str(item)
            for item in readiness.get("open_questions", [])
            if str(item).strip()
        ]
        return runtime.build_composition_context(
            task.id,
            task.prompt,
            acceptance_requirements,
            required_capabilities=required_capabilities,
            unresolved_user_requirements=unresolved_user_requirements,
        )

    def _persist_team_composition_plan(
        self,
        task_id: str,
        result: Any,
        availability_blockers: list[dict[str, Any]],
    ) -> None:
        """Persist the selected validated composition plan and metadata."""

        composition = self._team_composition_state(task_id)
        composition.update({
            "status": "validated",
            "attempt_count": int(getattr(result, "attempt_count", 0)),
            "recomposed": bool(getattr(result, "recomposed", False)),
            "validation_issue_history": [
                [issue.model_dump(mode="json") for issue in issue_group]
                for issue_group in getattr(result, "validation_issue_history", [])
            ],
            "selected_plan": result.plan.model_dump(mode="json"),
            "availability_blockers": availability_blockers,
            "blocked": None,
        })

    def _pause_for_team_composition(
        self,
        task: TaskRun,
        team: Team,
        blocked: TeamCompositionBlocked,
    ) -> None:
        """Pause through the existing clarification flow for missing user input."""

        state = self._state(task.id)
        composition = self._team_composition_state(task.id)
        issue_messages = [issue.message for issue in blocked.issues]
        question = issue_messages[0] if issue_messages else str(blocked)
        blocked_payload = {
            "category": blocked.category,
            "message": str(blocked),
            "issues": [issue.model_dump(mode="json") for issue in blocked.issues],
            "validation_issue_history": [
                [issue.model_dump(mode="json") for issue in issue_group]
                for issue_group in blocked.validation_issue_history
            ],
        }
        composition.update({
            "status": "waiting_for_user",
            "blocked": blocked_payload,
            "resume_used": False,
        })
        payload = {
            "question": question,
            "blockers": issue_messages,
            "open_questions": issue_messages,
            "resume_phase": "team_composition",
            "team_id": team.id,
            "team_member_ids": list(team.member_ids),
            "voter_ids": self._voter_ids(team),
            "team_leader_id": team.leader_id,
            "task_prompt": task.prompt,
            "working_brief": copy.deepcopy(state.get("working_brief")),
            "team_coordination_brief": copy.deepcopy(state.get("team_coordination_brief")),
            "readiness_tally": copy.deepcopy(state.get("readiness_tally", {})),
            "composition_state": copy.deepcopy(composition),
        }
        state["resume_phase"] = "team_composition"
        state["phase"] = "waiting_for_user"
        state["user_clarification"] = {"status": "requested", **payload}
        task.status = "waiting_for_user"
        task.updated_at = now_iso()
        self._emit(
            task.id,
            "user_clarification_requested",
            "The typed team composition phase needs user clarification before execution can continue.",
            payload=payload,
        )

    def _fail_team_composition(
        self,
        task: TaskRun,
        *,
        blocking_reason: str,
        category: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Record a truthful terminal failure for composition without fallback."""

        task.status = "failed"
        task.updated_at = now_iso()
        state = self._state(task.id)
        composition = self._team_composition_state(task.id)
        composition["status"] = "failed"
        if details:
            composition["blocked"] = copy.deepcopy(details)
        diagnosis = self._diagnose_failure(task.id)
        failure_payload = {
            "phase": "team_composition",
            "blocking_reason": blocking_reason,
            "missing_inputs": diagnosis["missing_inputs"],
            "missing_evidence": diagnosis["missing_evidence"],
            "system_error": blocking_reason,
            "recoverable": False,
            "blocker_category": category,
        }
        if details:
            failure_payload["details"] = details
        self._emit(
            task.id,
            "run_failed",
            f"Run failed during team composition: {blocking_reason}",
            payload=failure_payload,
        )
        self._emit(
            task.id,
            "task_failed",
            blocking_reason,
            payload={
                "phase": "team_composition",
                "error": blocking_reason,
                "blocker_category": category,
            },
        )

    def _record_composition_subtasks(
        self,
        task: TaskRun,
        team: Team,
        result: Any,
    ) -> None:
        """Map composed work nodes into durable session subtasks and events."""

        state = self._state(task.id)
        composition = self._team_composition_state(task.id)
        selected_plan = composition.get("selected_plan") or {}
        assignments_by_id = {
            str(item.get("id")): item
            for item in selected_plan.get("assignments", [])
            if isinstance(item, dict) and item.get("id")
        }
        nodes_by_id = {
            str(item.get("id")): item
            for item in selected_plan.get("work_graph", [])
            if isinstance(item, dict) and item.get("id")
        }
        materialized_by_assignment = {
            str(item.get("assignment_id")): item
            for item in composition.get("materialized_agents", [])
            if isinstance(item, dict) and item.get("assignment_id")
        }
        composed_subtasks: list[dict[str, Any]] = []
        leader_id = team.leader_id or (team.member_ids[0] if team.member_ids else None)
        for node_id, runtime_record in result.graph_result.nodes.items():
            assignment = assignments_by_id.get(str(runtime_record.assignment_id), {})
            node = nodes_by_id.get(str(node_id), {})
            materialized = materialized_by_assignment.get(str(runtime_record.assignment_id), {})
            output = result.node_outputs.get(node_id, {})
            artifact_refs = result.node_artifact_refs.get(node_id, [])
            attempt_records = [attempt.model_dump(mode="json") for attempt in runtime_record.attempts]
            terminal_attempt = attempt_records[-1] if attempt_records else {}
            blockers = [str(item) for item in runtime_record.blocked_dependency_ids]
            if terminal_attempt.get("message"):
                blockers.append(str(terminal_attempt["message"]))
            status = str(runtime_record.status)
            result_summary = ""
            if isinstance(output, dict) and output:
                result_summary = json.dumps(output, sort_keys=True)[:240]
            elif terminal_attempt.get("message"):
                result_summary = str(terminal_attempt["message"])[:240]
            elif blockers:
                result_summary = "; ".join(blockers)[:240]
            subtask = {
                "id": str(node_id),
                "assignment_id": str(runtime_record.assignment_id),
                "agent_id": str(materialized.get("id") or assignment.get("agent_template_id") or runtime_record.assignment_id),
                "agent_template_id": str(materialized.get("template_id") or assignment.get("agent_template_id") or ""),
                "execution_agent_id": str(materialized.get("id") or ""),
                "subtask": str(assignment.get("objective") or ""),
                "status": status,
                "outcome_status": status,
                "result_summary": result_summary,
                "outcome_summary": result_summary,
                "blockers": blockers,
                "evidence_refs": [str(item) for item in artifact_refs],
                "artifact_refs": [str(item) for item in artifact_refs],
                "outputs": copy.deepcopy(output),
                "depends_on": [str(item) for item in node.get("depends_on", [])],
                "conflict_domains": [str(item) for item in node.get("conflict_domains", [])],
                "required_capabilities": [str(item) for item in assignment.get("required_capabilities", [])],
                "tool_grants": copy.deepcopy(assignment.get("tool_grants", [])),
                "owned_paths": [str(item) for item in assignment.get("owned_paths", [])],
                "expected_artifacts": [str(item) for item in assignment.get("expected_artifacts", [])],
                "done_criteria": [str(item) for item in assignment.get("acceptance_checks", [])],
                "attempt_count": len(attempt_records),
                "attempts": attempt_records,
                "provenance": "team_composition",
                "source": "team_composition",
                "blocking_if_missing": True,
            }
            composed_subtasks.append(subtask)
            self._emit(
                task.id,
                "delegation_assigned",
                f"Typed team composition assigned work node {node_id} for execution.",
                actor=leader_id,
                payload={
                    "subtask_id": subtask["id"],
                    "agent_id": subtask["agent_id"],
                    "assigned_by": leader_id,
                    "objective": subtask["subtask"],
                    "why_assigned": "Validated team composition plan.",
                    "done_criteria": subtask["done_criteria"],
                    "blocking_if_missing": True,
                    "required_capabilities": subtask["required_capabilities"],
                    "provenance": "team_composition",
                    "status": "assigned",
                },
            )
            self._emit(
                task.id,
                "delegation_reported",
                f"Typed team composition reported terminal status for work node {node_id}.",
                actor=subtask["agent_id"],
                payload={
                    "subtask_id": subtask["id"],
                    "agent_id": subtask["agent_id"],
                    "status": subtask["status"],
                    "result_summary": subtask["result_summary"],
                    "blockers": subtask["blockers"],
                    "outcome_status": subtask["outcome_status"],
                    "outcome_summary": subtask["outcome_summary"],
                    "evidence_refs": subtask["evidence_refs"],
                    "provenance": "team_composition",
                },
            )
        state["subtasks"] = composed_subtasks

    async def _run_team_composition_phase(
        self,
        task: TaskRun,
        team: Team,
        *,
        allow_started_resume: bool = False,
    ) -> str:
        """Compose and execute the accepted typed team plan when enabled."""

        if not self._should_run_team_composition(task.id, allow_started_resume=allow_started_resume):
            return "disabled"
        state = self._state(task.id)
        composition = self._team_composition_state(task.id)
        composition["status"] = "running"
        state["phase"] = "team_composition"
        runtime = self._create_composition_runtime(task.id)
        context = self._build_team_composition_context(
            task,
            runtime,
            allow_snapshot_resume=allow_started_resume,
            include_user_clarification=allow_started_resume,
        )
        context_snapshot = self._serialize_composition_context(context)
        if context_snapshot is not None:
            composition["context_snapshot"] = context_snapshot
        availability = runtime.available_tool_ids()
        composition["availability_blockers"] = [
            blocker.model_dump(mode="json") for blocker in availability.blockers
        ]
        try:
            if self._is_legacy_composition_resume(task.id):
                composition["selection_strategy"] = "legacy_composer"
                composer = self._create_team_composer(task.id)
                composed = await composer.compose(context)
            else:
                composition["selection_strategy"] = "fixed_specialists"
                composed = await self._select_fixed_specialist_plan(task, team, context, runtime)
        except SpecialistSelectionError as exc:
            details = {
                "category": "fixed_specialist_selection_blocked",
                "message": str(exc),
                "blockers": [blocker.model_dump(mode="json") for blocker in exc.blockers],
            }
            composition["blocked"] = details
            self._fail_team_composition(
                task,
                blocking_reason=str(exc),
                category="fixed_specialist_selection_blocked",
                details=details,
            )
            return "failed"
        except GovernanceToolError as exc:
            details = {"code": "leader_specialist_tool_failed", "message": str(exc)}
            composition["blocked"] = details
            self._fail_team_composition(
                task,
                blocking_reason=str(exc),
                category="provider_system_blocker",
                details=details,
            )
            return "failed"
        except TeamCompositionBlocked as exc:
            blocked_details = {
                "category": exc.category,
                "message": str(exc),
                "issues": [issue.model_dump(mode="json") for issue in exc.issues],
                "validation_issue_history": [
                    [issue.model_dump(mode="json") for issue in issue_group]
                    for issue_group in exc.validation_issue_history
                ],
            }
            composition["blocked"] = blocked_details
            if exc.category == "missing_user_input":
                self._pause_for_team_composition(task, team, exc)
                return "paused"
            self._fail_team_composition(
                task,
                blocking_reason=str(exc),
                category=exc.category,
                details=blocked_details,
            )
            return "failed"
        except TeamCompositionProviderError as exc:
            details = {"code": exc.code, "message": str(exc)}
            composition["blocked"] = details
            self._fail_team_composition(
                task,
                blocking_reason=str(exc),
                category="provider_system_blocker",
                details=details,
            )
            return "failed"

        self._persist_team_composition_plan(
            task.id,
            composed,
            [blocker.model_dump(mode="json") for blocker in availability.blockers],
        )
        cancellation_event = asyncio.Event()
        self._composition_cancellation_events[task.id] = cancellation_event
        try:
            execution = await runtime.execute_plan(
                composed.plan,
                task.id,
                task.prompt,
                session_state=state,
                global_cancellation_event=cancellation_event,
            )
        finally:
            self._composition_cancellation_events.pop(task.id, None)
        composition.update({
            "materialized_agents": [item.model_dump(mode="json") for item in execution.materialized_agents],
            "execution_result": execution.model_dump(mode="json"),
            "availability_blockers": [item.model_dump(mode="json") for item in execution.availability_blockers],
        })
        state["composition_execution_roster"] = copy.deepcopy(composition["materialized_agents"])
        self._record_composition_subtasks(task, team, execution)
        terminal_status = str(execution.graph_result.terminal_status)
        composition["status"] = terminal_status
        if terminal_status != "completed":
            interruption = state.get("interruption") if isinstance(state.get("interruption"), dict) else {}
            if interruption.get("reason") == "graceful_shutdown" and terminal_status == "canceled":
                return "failed"
            self._fail_team_composition(
                task,
                blocking_reason=f"Composed execution ended with {terminal_status}.",
                category=f"composition_execution_{terminal_status}",
                details={"execution_result": execution.model_dump(mode="json")},
            )
            return "failed"
        state["phase"] = "delegating"
        return "completed"

    async def _continue_after_execution(self, task: TaskRun, team: Team, start_time: float) -> None:
        """Run the unchanged legacy post-delegation governance and final flow."""

        if self.settings.efficient_society_enabled:
            proposals = await self._negotiate_efficient(task, team)
            self._emit(
                task.id,
                "efficient_phase_skipped",
                "The critic counterproposal replaced a separate all-member opinion round.",
                payload={"phase": "proposal_opinions", "reason": "counterproposal_is_bounded_review"},
            )
        else:
            proposals = await self._negotiate(task, team)
            await self._collect_proposal_opinions(task, team, proposals)
        winner = await self._vote(task, team, proposals)
        if winner is None:
            return
        await self._complete_after_vote(task, team, winner, proposals, start_time)

    async def _run_pre_execution_conversation(self, task: TaskRun, team: Team) -> None:
        """Run goal discussion and readiness loop before leader election when enabled."""

        if not self.settings.pre_execution_conversation_enabled:
            return
        max_discussion_rounds = 3
        max_readiness_attempts = 3
        state = self._state(task.id)
        for attempt in range(1, max_readiness_attempts + 1):
            state["readiness_attempt_count"] = attempt
            await self._run_goal_discussion_round(task, team, max_discussion_rounds)
            if not self.settings.efficient_society_enabled:
                await self._run_targeted_question_exchange(task, team)
            if self.settings.readiness_voting_enabled:
                if self.settings.efficient_society_enabled:
                    ballot_results = self._combined_discussion_readiness_ballots(task, team, attempt)
                    passed = await self._run_readiness_vote(task, team, attempt, ballot_results)
                else:
                    passed = await self._run_readiness_vote(task, team, attempt)
                if passed:
                    break
                if attempt == max_readiness_attempts:
                    blockers = state.get("readiness_tally", {}).get("blockers", [])
                    if blockers:
                        state["ready_to_proceed"] = False
                        self._emit(task.id, "readiness_loop_exhausted", "Readiness loop exhausted with critical blockers.", payload={"blockers": blockers, "attempts": attempt})
                        return
                    state["ready_to_proceed"] = True
                    self._emit(task.id, "readiness_proceeded_with_assumptions", "Readiness passed on final attempt without critical blockers.", payload={"attempts": attempt})
                    break
            else:
                state["ready_to_proceed"] = True
                break
        await self._compose_working_brief(task, team)
        await self._collect_working_brief_positions(task, team)
        await self._collect_private_notes(task, team)
        self._emit_meeting_recap(task, team)

    async def _run_goal_discussion_round(self, task: TaskRun, team: Team, max_rounds: int) -> None:
        """Run a turn-taking discussion where agents react to prior speakers."""

        state = self._state(task.id)
        state["discussion_round_count"] += 1
        round_num = state["discussion_round_count"]
        prior_blockers = state.get("readiness_tally", {}).get("blockers", [])
        self._emit(task.id, "goal_discussion_started", f"Goal discussion round {round_num} started.", payload={"round": round_num, "max_rounds": max_rounds})
        for agent_id in team.member_ids:
            agent = self.agents[agent_id]
            blocker_context = "\n".join(f"- {item}" for item in prior_blockers) or "- None"
            prior_turns = [
                item
                for item in state.get("goal_discussions", [])
                if isinstance(item, dict) and item.get("round") == round_num
            ]
            transcript = "\n".join(
                (
                    f"{turn.get('agent_id')}: stance={turn.get('stance')}; "
                    f"unique={turn.get('unique_contribution') or turn.get('interpretation')}; "
                    f"question={turn.get('question_for_next') or 'none'}"
                )
                for turn in prior_turns
            ) or "- You are the first speaker. Frame the problem briefly and ask the next agent a useful question."
            previous_agent = prior_turns[-1].get("agent_id") if prior_turns else None
            try:
                statement = await self._run_governance_tool(
                    task=task,
                    actor_identity=agent,
                    tool_func=submit_goal_discussion_tool,
                    tool_name="submit_goal_discussion",
                    schema_class=GoalDiscussionStatement,
                    prompt=(
                    f"Task: {task.prompt}\n"
                    f"Your exact agent_id is {agent_id}.\n"
                    f"Discussion round: {round_num}.\n"
                    f"Prior readiness blockers:\n{blocker_context}\n"
                    f"Conversation so far:\n{transcript}\n"
                    f"Previous speaker to respond to: {previous_agent or 'none'}.\n"
                    "Act like a human teammate in a short planning meeting. "
                    "Do not restate the full task. Do not repeat prior points unless you explicitly challenge or refine them. "
                    "If you agree, say what you add. If you disagree, say what should change. "
                    "Set responds_to to the previous speaker when there is one. "
                    "Set stance to one of builds_on, challenges, clarifies, blocks. "
                    "Put the new value you add in unique_contribution. "
                    "Keep interpretation and suggested_scope concise. "
                    "Set spoken_turn to one or two short sentences that sound like a teammate in a meeting. "
                    "The spoken turn must respond to the previous speaker when there is one. "
                    "Ask question_for_next when the next agent should resolve something. "
                    "Also cast your readiness decision in this same tool call. Set ready=false and "
                    "critical_blocker=true only for missing_user_input, missing_system_capability, "
                    "or safety_or_policy. Future work and ordinary risk do not block execution. "
                    "When blocked, include blocker_category, required_clarification, blocker_owner, "
                    "and blocker_remediation. "
                        "Call submit_goal_discussion with your exact agent_id."
                    ),
                )
            except GovernanceToolError as exc:
                # Do not turn a transport failure into a fabricated employee
                # contribution. Preserve the failure as a system fact and let
                # the remaining employees continue their real discussion.
                self._emit(
                    task.id,
                    "agent_contribution_unavailable",
                    f"{agent.name}'s discussion contribution was unavailable.",
                    actor=agent_id,
                    payload={"phase": "goal_discussion", "reason": str(exc)[:300]},
                )
                continue
            live_discussions = state.setdefault("goal_discussions", [])
            if not any(
                isinstance(item, dict)
                and item.get("round") == statement.round
                and item.get("agent_id") == statement.agent_id
                for item in live_discussions
            ):
                live_discussions.append(statement.model_dump())
            self._emit(task.id, "agent_goal_opinion", f"{agent.name} shared their view of the goal.", actor=agent_id, payload=statement.model_dump())
            self._record_conversation_turn(task.id, statement, len(prior_turns) + 1, prior_turns[-1] if prior_turns else None)
            self._record_position_from_discussion(task.id, statement)

    async def _run_targeted_question_exchange(self, task: TaskRun, team: Team) -> None:
        """Resolve one named agent question before readiness voting.

        This keeps the meeting-room transcript conversational without adding an
        unbounded debate loop. The first unanswered question from the current
        round is assigned to the next roster member, answered, and carried into
        the working brief as evidence or an assumption.
        """

        state = self._state(task.id)
        if state.get("targeted_question_exchanges"):
            return
        round_num = int(state.get("discussion_round_count") or 1)
        current_round = [
            item
            for item in state.get("goal_discussions", [])
            if isinstance(item, dict) and item.get("round") == round_num
        ]
        question_turn = next((item for item in current_round if item.get("question_for_next")), None)
        if not question_turn:
            return
        asker_id = str(question_turn.get("agent_id"))
        asker_index = team.member_ids.index(asker_id) if asker_id in team.member_ids else -1
        target_id = team.member_ids[(asker_index + 1) % len(team.member_ids)]
        if target_id == asker_id and len(team.member_ids) > 1:
            target_id = team.member_ids[0]
        target = self.agents[target_id]
        question = str(question_turn.get("question_for_next"))
        answer = self._answer_targeted_question(target, question, task.prompt)
        resolved = not any(word in answer.lower() for word in ("cannot resolve", "need user", "blocked"))
        assumption = None if resolved else f"Proceed only if this remains acceptable: {question}"
        exchange = TargetedQuestionExchange(
            round=round_num,
            question_id=f"q{len(state.setdefault('targeted_question_exchanges', [])) + 1}",
            asker_id=asker_id,
            target_agent_id=target_id,
            question=question,
            answer=answer,
            resolved=resolved,
            assumption=assumption,
        )
        payload = exchange.model_dump()
        state.setdefault("targeted_question_exchanges", []).append(payload)
        self._emit(
            task.id,
            "targeted_question_answered",
            f"{target.name} answered {asker_id}'s targeted question.",
            actor=target_id,
            payload=payload,
        )
        answer_statement = GoalDiscussionStatement(
            round=round_num,
            agent_id=target_id,
            responds_to=asker_id,
            stance="clarifies" if resolved else "blocks",
            interpretation=f"Answering {asker_id}'s question before readiness.",
            unique_contribution=answer,
            success_criteria=[],
            concerns=[] if resolved else [question],
            suggested_scope="Use the answer as the handoff assumption for readiness.",
            question_for_next=None,
            spoken_turn=f"{target.name}: On {asker_id}'s question, {answer}",
        )
        state.setdefault("goal_discussions", []).append(answer_statement.model_dump())
        self._record_conversation_turn(task.id, answer_statement, len(current_round) + 1, question_turn)

    def _answer_targeted_question(self, agent: SocietyAgent, question: str, task_prompt: str) -> str:
        """Create a concise role-grounded answer for the targeted Q&A loop."""

        role = agent.role.lower()
        if "research" in role:
            return f"I can validate the weakest assumption first and label anything unverified; for now, treat '{question}' as an evidence gap."
        if "implementation" in role:
            return f"I can own the first runnable check and keep scope narrow; if '{question}' stays open, I will ship with that caveat visible."
        if "review" in role or "critic" in role:
            return f"I will accept proceeding only if the final answer carries this risk explicitly: {question}"
        return f"I will keep the system boundary clear and make '{question}' an explicit assumption in the plan for {task_prompt[:80]}."

    def _record_conversation_turn(
        self,
        task_id: str,
        statement: GoalDiscussionStatement,
        turn_index: int,
        prior_turn: dict[str, Any] | None,
    ) -> None:
        """Store and emit a readable transcript turn from a structured statement."""

        prior_quote = None
        if prior_turn:
            prior_quote = str(prior_turn.get("unique_contribution") or prior_turn.get("interpretation") or "")[:180] or None
        responds_to = statement.responds_to or (str(prior_turn.get("agent_id")) if prior_turn else None)
        says = statement.spoken_turn.strip() or self._fallback_spoken_turn(statement, prior_quote)
        if prior_quote and prior_quote not in says:
            says = f"On '{prior_quote}', {says}"
        turn = ConversationTurn(
            round=statement.round,
            turn_index=turn_index,
            agent_id=statement.agent_id,
            responds_to=responds_to,
            stance=statement.stance,
            says=says,
            quote_from_prior=prior_quote,
            question_for_next=statement.question_for_next,
        )
        payload = turn.model_dump()
        self._state(task_id).setdefault("conversation_transcript", []).append(payload)
        self._emit(
            task_id,
            "conversation_turn",
            f"{statement.agent_id} spoke in the meeting room.",
            actor=statement.agent_id,
            payload=payload,
        )

    def _fallback_spoken_turn(self, statement: GoalDiscussionStatement, prior_quote: str | None) -> str:
        """Create a compact meeting-room line when a model omits spoken_turn."""

        opener = "I want to clarify the task."
        if statement.stance == "builds_on":
            opener = "I agree with that direction, and I would add one thing."
        elif statement.stance == "challenges":
            opener = "I do not think we should accept that yet."
        elif statement.stance == "blocks":
            opener = "I think this blocks execution until it is resolved."
        reference = f" On the prior point, '{prior_quote}'" if prior_quote else ""
        contribution = statement.unique_contribution or statement.interpretation
        question = f" My question for the next speaker: {statement.question_for_next}" if statement.question_for_next else ""
        return f"{opener}{reference} {contribution}{question}".strip()

    def _combined_discussion_readiness_ballots(
        self,
        task: TaskRun,
        team: Team,
        attempt: int,
    ) -> list[tuple[str, ReadinessBallot]]:
        """Build ballots from each agent's actual combined discussion response."""

        state = self._state(task.id)
        round_num = state.get("discussion_round_count")
        latest_by_agent = {
            str(item.get("agent_id")): item
            for item in state.get("goal_discussions", [])
            if isinstance(item, dict) and item.get("round") == round_num
        }
        results: list[tuple[str, ReadinessBallot]] = []
        for agent_id in self._voter_ids(team):
            item = latest_by_agent.get(agent_id)
            if item is None:
                continue
            statement = GoalDiscussionStatement.model_validate(item)
            results.append((agent_id, ReadinessBallot(
                attempt=attempt,
                agent_id=agent_id,
                ready=statement.ready,
                critical_blocker=statement.critical_blocker,
                reason=statement.unique_contribution or statement.interpretation,
                required_clarification=statement.required_clarification,
                blocker_category=statement.blocker_category,
                owner=statement.blocker_owner or agent_id,
                phase="pre_execution",
                remediation=statement.blocker_remediation,
            )))
        return results

    async def _run_readiness_vote(
        self,
        task: TaskRun,
        team: Team,
        attempt: int,
        ballot_results: list[tuple[str, ReadinessBallot]] | None = None,
    ) -> bool:
        """Collect readiness ballots from each team member and tally."""

        state = self._state(task.id)
        combined = ballot_results is not None
        if ballot_results is None:
            ballot_results = await self._collect_readiness_ballots_concurrent(task, team, attempt)
        ready_count = 0
        not_ready_count = 0
        blockers: list[str] = []
        structured_blockers: list[ReadinessBlocker] = []
        for agent_id, ballot in ballot_results:
            agent = self.agents[agent_id]
            if ballot.critical_blocker and not _is_structured_execution_blocker(ballot):
                original = ballot.model_dump()
                ballot = ballot.model_copy(update={
                    "ready": True,
                    "critical_blocker": False,
                    "required_clarification": None,
                    "reason": (
                        "Ready for execution; the original objection is not a blocking category."
                    ),
                })
                self._emit(
                    task.id,
                    "readiness_blocker_reclassified",
                    "A non-blocking readiness objection was reclassified.",
                    actor=agent_id,
                    payload={"original_ballot": original, "effective_ballot": ballot.model_dump()},
                )
            if ballot.ready:
                ready_count += 1
            else:
                not_ready_count += 1
            if ballot.critical_blocker:
                blockers.append(f"{agent_id}: {ballot.reason}")
                structured_blockers.append(ReadinessBlocker(
                    category=ballot.blocker_category or "missing_user_input",
                    owner=ballot.owner or agent_id,
                    phase=ballot.phase or "pre_execution",
                    remediation=ballot.remediation or ballot.required_clarification or "Resolve the blocker before execution.",
                    reason=ballot.reason,
                ))
                self._record_objection(
                    task.id,
                    ObjectionRecord(
                        agent_id=agent_id,
                        severity="critical",
                        objection=ballot.reason,
                        resolution_condition=ballot.required_clarification or "Clarify the blocker before execution.",
                        blocks_execution=True,
                    ),
                )
            self._emit(task.id, "readiness_vote_cast", f"{agent.name} cast a readiness vote.", actor=agent_id, payload=ballot.model_dump())
            self._emit_tool_call(
                task.id,
                "cast_readiness_vote",
                agent_id,
                f"attempt={attempt}",
                ballot.model_dump(),
                "deterministic_no_key" if combined else "native_agno",
                not ballot.reason.startswith("Recovered from readiness tool issue"),
            )
        total = ready_count + not_ready_count
        passed = ready_count > total / 2 and not blockers
        tally = ReadinessTally(
            attempt=attempt,
            ready_count=ready_count,
            not_ready_count=not_ready_count,
            total=total,
            passed=passed,
            blockers=blockers,
            structured_blockers=structured_blockers,
        )
        state["readiness_tally"] = tally.model_dump()
        state["ready_to_proceed"] = passed if passed else state.get("ready_to_proceed")
        self._emit(task.id, "readiness_vote_tallied", f"Readiness vote tallied for attempt {attempt}.", payload=tally.model_dump())
        return passed

    async def _collect_readiness_ballots_concurrent(
        self,
        task: TaskRun,
        team: Team,
        attempt: int,
    ) -> list[tuple[str, ReadinessBallot]]:
        """Collect readiness ballots in parallel with bounded concurrency.

        Each concurrent model call receives a unique derived session_id and a
        deep-copied snapshot of session state. No live state is mutated and no
        events are emitted during collection. Results are returned in the
        original roster order.
        """

        state = self._state(task.id)
        roster = self._voter_ids(team)
        limit = max(1, int(self.settings.readiness_concurrency))
        semaphore = asyncio.Semaphore(limit)

        async def _collect_one(agent_id: str) -> tuple[str, ReadinessBallot]:
            agent = self.agents[agent_id]
            latest_statement = next(
                (
                    item
                    for item in reversed(state.get("goal_discussions", []))
                    if isinstance(item, dict)
                    and item.get("agent_id") == agent_id
                    and item.get("round") == state.get("discussion_round_count")
                ),
                {},
            )
            derived_session_id = f"{task.id}:readiness:{agent_id}:{uuid4().hex[:8]}"
            state_snapshot = copy.deepcopy(state)
            prompt = (
                f"Task: {task.prompt}\n"
                f"Your exact agent_id is {agent_id}.\n"
                f"Readiness attempt: {attempt}.\n"
                f"Your latest discussion statement: {latest_statement}\n"
                "Vote whether the society is ready to execute. "
                "Set critical_blocker=true only if execution would be misleading without user clarification. "
                "When critical_blocker=true, set blocker_category to one of: "
                "missing_user_input, missing_system_capability, safety_or_policy, future_work, risk. "
                "Only missing_user_input, missing_system_capability, and safety_or_policy block execution. "
                "future_work and risk are recorded but do not block. "
                "Set owner to the agent or role responsible, phase to the relevant phase, "
                "and remediation to what must happen to resolve the blocker. "
                "Do not block because research, implementation, validation, votes, or final artifacts are not yet complete; "
                "those are future_work, not blocking prerequisites. "
                "Block only for ambiguous user intent, contradictory constraints, "
                "unavailable required capabilities, or a decision that only the user can make. "
                "Call cast_readiness_vote with your exact agent_id."
            )
            async with semaphore:
                ballot = await self._run_governance_tool_isolated(
                    task=task,
                    actor_identity=agent,
                    tool_func=cast_readiness_vote_tool,
                    tool_name="cast_readiness_vote",
                    schema_class=ReadinessBallot,
                    prompt=prompt,
                    derived_session_id=derived_session_id,
                    state_snapshot=state_snapshot,
                )
            return agent_id, ballot

        tasks = [asyncio.create_task(_collect_one(agent_id)) for agent_id in roster]
        results: list[tuple[str, ReadinessBallot]] = []
        for coro in asyncio.as_completed(tasks):
            results.append(await coro)
        order = {agent_id: idx for idx, agent_id in enumerate(roster)}
        results.sort(key=lambda pair: order[pair[0]])
        return results

    async def _collect_votes_concurrent(
        self,
        task: TaskRun,
        team: Team,
        candidates: list[str],
    ) -> list[tuple[str, VoteDecision]]:
        """Collect proposal votes in parallel with bounded concurrency.

        Each concurrent model call receives a unique derived session_id and a
        deep-copied snapshot of session state. No live state is mutated and no
        events are emitted during collection. Results are returned in the
        original roster order.
        """

        state = self._state(task.id)
        roster = self._voter_ids(team)
        limit = max(1, int(self.settings.readiness_concurrency))
        semaphore = asyncio.Semaphore(limit)
        summary = "\n".join(f"- {cid}: {self.agents[cid].name}, role={self.agents[cid].role}" for cid in candidates)

        if self.settings.native_voting_enabled:
            tool_func = cast_ballot_tool
            tool_name = "cast_ballot"
            tail = "Call the cast_ballot tool with your exact voter_id and decision."
        else:
            tool_func = cast_vote_tool
            tool_name = "cast_vote"
            tail = "Call the cast_vote tool with your decision."

        async def _collect_one(voter_id: str) -> tuple[str, VoteDecision]:
            voter = self.agents[voter_id]
            derived_session_id = f"{task.id}:vote:{voter_id}:{uuid4().hex[:8]}"
            state_snapshot = copy.deepcopy(state)
            prompt = (
                f"Task: {task.prompt}\n"
                f"Your exact voter_id is {voter_id}.\n"
                f"Candidates:\n{summary}\n"
                f"Vote for the best proposal. The choice must be one of: {', '.join(candidates)}.\n"
                f"{tail}"
            )
            async with semaphore:
                decision = await self._run_governance_tool_isolated(
                    task=task,
                    actor_identity=voter,
                    tool_func=tool_func,
                    tool_name=tool_name,
                    schema_class=VoteDecision,
                    prompt=prompt,
                    derived_session_id=derived_session_id,
                    state_snapshot=state_snapshot,
                )
            return voter_id, decision

        tasks = [asyncio.create_task(_collect_one(voter_id)) for voter_id in roster]
        results: list[tuple[str, VoteDecision]] = []
        for coro in asyncio.as_completed(tasks):
            results.append(await coro)
        order = {voter_id: idx for idx, voter_id in enumerate(roster)}
        results.sort(key=lambda pair: order[pair[0]])
        return results

    async def _compose_working_brief(self, task: TaskRun, team: Team) -> None:
        """Freeze the shared task understanding after readiness passes.

        Incorporates the structured team coordination brief's open_questions,
        risk_signals, evidence_gaps, and assumptions so the working brief
        carries forward the team's identified truth signals.
        """

        state = self._state(task.id)
        discussions = state.get("goal_discussions", [])
        all_criteria: list[str] = []
        all_concerns: list[str] = []
        unresolved_dissent: list[str] = []
        scopes: list[str] = []
        for disc in discussions:
            if isinstance(disc, dict):
                all_criteria.extend(disc.get("success_criteria", []))
                all_concerns.extend(disc.get("concerns", []))
                if disc.get("stance") in {"challenges", "blocks"}:
                    dissent = disc.get("unique_contribution") or disc.get("interpretation") or disc.get("concerns")
                    if dissent:
                        unresolved_dissent.append(str(dissent))
                scope = disc.get("suggested_scope", "")
                if scope:
                    scopes.append(scope)
        exchanges = [
            TargetedQuestionExchange.model_validate(item)
            for item in state.get("targeted_question_exchanges", [])
            if isinstance(item, dict)
        ]
        unanswered_questions = [
            str(disc.get("question_for_next"))
            for disc in discussions
            if isinstance(disc, dict)
            and disc.get("question_for_next")
            and not any(exchange.question == disc.get("question_for_next") for exchange in exchanges)
        ]
        assumptions = [
            exchange.assumption
            for exchange in exchanges
            if exchange.assumption
        ]
        blockers = state.get("readiness_tally", {}).get("blockers", [])
        unresolved_dissent.extend(str(item) for item in blockers)
        coord_brief = state.get("team_coordination_brief")
        if isinstance(coord_brief, dict):
            for q in coord_brief.get("open_questions", []):
                text = str(q).strip()
                if text:
                    unanswered_questions.append(text)
            for gap in coord_brief.get("evidence_gaps", []):
                text = str(gap).strip()
                if text:
                    all_concerns.append(f"evidence gap: {text}")
            for assumption in coord_brief.get("assumptions", []):
                text = str(assumption).strip()
                if text:
                    assumptions.append(text)
            for risk in coord_brief.get("risk_signals", []):
                text = str(risk).strip()
                if text:
                    unresolved_dissent.append(f"risk: {text}")
        has_blocking_objections = any(
            obj.get("blocks_execution") for obj in state.get("public_room", {}).get("objections", [])
            if isinstance(obj, dict)
        )
        proceeding_with_dissent = bool(unresolved_dissent) and not has_blocking_objections and state.get("ready_to_proceed", False)
        coord_confidence = coord_brief.get("confidence") if isinstance(coord_brief, dict) else None
        base_confidence = 0.8 if state.get("ready_to_proceed") else 0.5
        final_confidence = max(base_confidence, float(coord_confidence)) if isinstance(coord_confidence, (int, float)) else base_confidence
        brief = WorkingBrief(
            summary=coord_brief.get("summary", task.prompt[:300]) if isinstance(coord_brief, dict) and coord_brief.get("summary") else task.prompt[:300],
            agreed_scope=scopes[-1] if scopes else (coord_brief.get("recommended_focus", task.prompt[:200]) if isinstance(coord_brief, dict) and coord_brief.get("recommended_focus") else task.prompt[:200]),
            success_criteria=list(dict.fromkeys(all_criteria))[:10],
            constraints=[],
            open_questions=list(dict.fromkeys([*all_concerns, *unanswered_questions]))[:10],
            blocked_items=blockers,
            unresolved_dissent=list(dict.fromkeys(unresolved_dissent))[:10],
            proceeding_with_dissent=proceeding_with_dissent,
            assumptions=list(dict.fromkeys([item for item in assumptions if item]))[:10],
            question_answers=exchanges,
            confidence=final_confidence,
        )
        state["working_brief"] = brief.model_dump()
        self._emit(task.id, "working_brief_finalized", "Working brief finalized after readiness.", payload=brief.model_dump())

    def _emit_meeting_recap(self, task: TaskRun, team: Team) -> None:
        """Emit a user-facing narrative recap of the collaboration."""

        state = self._state(task.id)
        brief = state.get("working_brief") if isinstance(state.get("working_brief"), dict) else {}
        transcript = [item for item in state.get("conversation_transcript", []) if isinstance(item, dict)]
        exchanges = [item for item in state.get("targeted_question_exchanges", []) if isinstance(item, dict)]
        influenced_by = [
            f"{turn.get('agent_id')} responded to {turn.get('responds_to')} on {turn.get('stance')}"
            for turn in transcript
            if turn.get("responds_to")
        ][:6]
        plan_changes = [
            f"{exchange.get('target_agent_id')} answered: {exchange.get('answer')}"
            for exchange in exchanges
        ]
        if brief.get("agreed_scope"):
            plan_changes.append(f"Agreed scope became: {brief.get('agreed_scope')}")
        recap = MeetingRecap(
            summary=(
                f"{len(transcript)} meeting turns produced a working brief, "
                f"{len(exchanges)} targeted Q&A exchange, and "
                f"{len(brief.get('unresolved_dissent', [])) if isinstance(brief, dict) else 0} unresolved dissent item(s)."
            ),
            influenced_by=influenced_by,
            plan_changes=plan_changes[:6],
            unresolved_dissent=list(brief.get("unresolved_dissent", []))[:6] if isinstance(brief, dict) else [],
            saved_lessons=list(brief.get("assumptions", []))[:6] if isinstance(brief, dict) else [],
        )
        state["meeting_recap"] = recap.model_dump()
        self._emit(task.id, "meeting_recap", "Meeting recap prepared for replay.", payload=recap.model_dump())

    async def _collect_working_brief_positions(self, task: TaskRun, team: Team) -> None:
        """Collect explicit public stances on the working brief."""

        if not self.settings.social_tools_enabled:
            return
        if self.settings.efficient_society_enabled:
            self._emit(
                task.id,
                "efficient_phase_skipped",
                "Skipped repeated working-brief positions; readiness ballots already captured every voter's stance.",
                payload={"phase": "working_brief_positions", "replacement": "readiness_ballots"},
            )
            return
        state = self._state(task.id)
        brief = state.get("working_brief")
        if not isinstance(brief, dict):
            return
        brief_summary = brief.get("summary", "")
        agreed_scope = brief.get("agreed_scope", "")
        blockers = brief.get("blocked_items", [])
        for agent_id in team.member_ids:
            agent = self.agents[agent_id]
            if not self.settings.llm_enabled:
                stance = "block" if blockers and agent.profile.risk_tolerance == "low" else "support"
                reason = (
                    f"{agent.name} {'blocks' if stance == 'block' else 'supports'} the working brief "
                    f"using profile bias: {agent.profile.decision_bias or agent.role}."
                )
                position = AgentPosition(
                    agent_id=agent_id,
                    phase="working_brief",
                    stance=stance,
                    reason=reason,
                    confidence=0.8,
                    conditions=[str(item) for item in blockers] if blockers else list(agent.profile.default_blockers[:1]),
                )
                self._record_position(task.id, position)
                self._emit_tool_call(
                    task.id,
                    "state_position",
                    agent_id,
                    "deterministic",
                    position.model_dump(),
                    "deterministic_no_key",
                    True,
                )
                continue
            try:
                position = await self._run_governance_tool(
                    task=task,
                    actor_identity=agent,
                    tool_func=state_position_tool,
                    tool_name="state_position",
                    schema_class=AgentPosition,
                    prompt=(
                    f"Task: {task.prompt}\n"
                    f"Your exact agent_id is {agent_id}.\n"
                    f"Working brief summary: {brief_summary}\n"
                    f"Agreed scope: {agreed_scope}\n"
                    f"Blocked items: {blockers}\n"
                    "State your public position on this working brief. "
                    "Use stance support, oppose, uncertain, defer, or block. "
                    "Choose block only for a critical issue that should stop execution. "
                        "Call state_position with phase='working_brief' and your exact agent_id."
                    ),
                )
            except GovernanceToolError as exc:
                self._emit(task.id, "agent_contribution_unavailable", f"{agent.name}'s brief position was unavailable.", actor=agent_id, payload={"phase": "working_brief", "reason": str(exc)[:300]})
                continue
            if self.settings.social_trace_enabled:
                self._emit(
                    task.id,
                    "agent_position_stated",
                    f"{agent.name} is {position.stance} on the working brief.",
                    actor=agent_id,
                    payload=position.model_dump(),
                )

    async def _collect_private_notes(self, task: TaskRun, team: Team) -> None:
        """Let each agent keep a private reservation and publish only useful notes."""

        if not self.settings.social_tools_enabled:
            return
        if self.settings.efficient_society_enabled:
            self._emit(
                task.id,
                "efficient_phase_skipped",
                "Skipped private-note model calls in efficient mode; blockers remain visible in readiness evidence.",
                payload={"phase": "private_notes", "replacement": "readiness_blockers"},
            )
            return
        state = self._state(task.id)
        brief = state.get("working_brief")
        if not isinstance(brief, dict):
            return
        brief_summary = brief.get("summary", "")
        open_questions = brief.get("open_questions", [])
        blockers = brief.get("blocked_items", [])
        for agent_id in team.member_ids:
            agent = self.agents[agent_id]
            if not self.settings.llm_enabled:
                note = self._deterministic_private_note(agent, brief_summary, open_questions, blockers)
                self._record_private_note(task.id, note)
                self._emit_tool_call(
                    task.id,
                    "record_private_note",
                    agent_id,
                    "deterministic",
                    note.model_dump(),
                    "deterministic_no_key",
                    True,
                )
                if note.may_publish:
                    self._publish_private_note(task.id, note)
                    self._emit_tool_call(
                        task.id,
                        "publish_private_note",
                        agent_id,
                        "deterministic",
                        note.model_dump(),
                        "deterministic_no_key",
                        True,
                    )
                continue

            try:
                note = await self._run_governance_tool(
                    task=task,
                    actor_identity=agent,
                    tool_func=record_private_note_tool,
                    tool_name="record_private_note",
                    schema_class=PrivateNote,
                    prompt=(
                    f"Task: {task.prompt}\n"
                    f"Your exact agent_id is {agent_id}.\n"
                    f"Working brief summary: {brief_summary}\n"
                    f"Open questions: {open_questions}\n"
                    f"Blocked items: {blockers}\n"
                    "Record a short private note before execution. "
                    "This is your private scratchpad: name a reservation, evidence gap, handoff risk, or focus area. "
                    "Set may_publish=true only if the team should see the note before work starts. "
                        "Call record_private_note with phase='working_brief' and your exact agent_id."
                    ),
                )
            except GovernanceToolError as exc:
                self._emit(task.id, "agent_contribution_unavailable", f"{agent.name}'s private note was unavailable.", actor=agent_id, payload={"phase": "working_brief", "reason": str(exc)[:300]})
                continue
            if note.may_publish:
                published = await self._run_governance_tool(
                    task=task,
                    actor_identity=agent,
                    tool_func=publish_private_note_tool,
                    tool_name="publish_private_note",
                    schema_class=PrivateNote,
                    prompt=(
                        f"Your exact agent_id is {agent_id}.\n"
                        f"Publish this selected private note for the team: {note.note}\n"
                        "Call publish_private_note with phase='working_brief' and your exact agent_id."
                    ),
                )
                self._emit_published_private_note(task.id, published)

    def _deterministic_private_note(
        self,
        agent: SocietyAgent,
        brief_summary: str,
        open_questions: list[Any],
        blockers: list[Any],
    ) -> PrivateNote:
        """Create a role-shaped private note for no-key local runs."""

        topic = str(open_questions[0]) if open_questions else str(blockers[0]) if blockers else brief_summary[:120]
        role_focus = {
            "architect": "Watch for scope drift before assigning ownership.",
            "researcher": "Verify the weakest assumption before treating the brief as evidence-backed.",
            "builder": "Keep the first implementation step narrow enough to validate quickly.",
            "critic": "Do not let execution start if the acceptance check remains vague.",
        }
        note = role_focus.get(agent.role, f"Track the main uncertainty: {topic}")
        if topic and topic not in note:
            note = f"{note} Current private concern: {topic}"
        return PrivateNote(
            agent_id=agent.id,
            phase="working_brief",
            note=note,
            may_publish=agent.role in {"critic", "researcher"} or bool(blockers),
        )


    def _register_artifact(
        self,
        task_id: str,
        artifact_type: str,
        producer: str,
        phase: str,
        content: dict[str, Any],
        depends_on: list[str] | None = None,
        confidence: float = 1.0,
        status: str = "draft",
    ) -> ArtifactRecord | None:
        """Append a typed artifact to session state when rollout flag is enabled."""

        if not self.settings.artifact_tracking_enabled:
            return None
        state = self._state(task_id)
        record = ArtifactRecord(
            id=f"artifact-{uuid4().hex[:10]}",
            type=artifact_type,
            producer=producer,
            phase=phase,
            content=content,
            depends_on=depends_on or [],
            confidence=confidence,
            status=status,
            created_at=now_iso(),
        )
        artifacts = state.setdefault("artifacts", [])
        artifacts.append(record.model_dump())
        state["artifact_index"] = {item["id"]: item for item in artifacts}
        return record

    def _record_shared_artifact_revision(
        self,
        task_id: str,
        artifact_id: str | None,
        section: str,
        editor_id: str,
        reviewer_id: str,
        change_summary: str,
        critique: str,
    ) -> None:
        """Record who changed a shared artifact and why."""

        payload = {
            "artifact_id": artifact_id,
            "section": section,
            "editor_id": editor_id,
            "reviewer_id": reviewer_id,
            "change_summary": change_summary,
            "critique": critique,
        }
        self._state(task_id).setdefault("shared_artifact_revisions", []).append(payload)
        self._emit(
            task_id,
            "artifact_section_critiqued",
            f"{reviewer_id} attached critique to {section}.",
            actor=reviewer_id,
            payload=payload,
        )
        self._emit(
            task_id,
            "shared_artifact_revised",
            f"{editor_id} revised {section} after critique.",
            actor=editor_id,
            payload=payload,
        )

    def _reputation_snapshot(self, agent_ids: list[str]) -> dict[str, dict]:
        snapshot = self.reputation.snapshot()
        for agent_id in agent_ids:
            if agent_id not in snapshot:
                snapshot[agent_id] = {
                    "legacy_score": self.agents[agent_id].reputation,
                    "election_score": self.agents[agent_id].reputation,
                    "history": [],
                }
        return snapshot

    def _capture_model_usage(
        self,
        task_id: str,
        response: Any,
        call_kind: str,
        actor_id: str,
    ) -> None:
        """Capture factual Agno RunOutput.metrics for benchmark accounting.

        Reads ``response.metrics.to_dict()`` when present and appends a usage
        record to ``session_state['model_usage']``.  Tolerates missing or
        partial metrics without raising.  Does not alter the response, returned
        values, or error propagation.

        Each record stores: task_id, call_kind, actor_id, model,
        model_provider, input/output/total/cache_read/cache_write/reasoning
        tokens, cost, duration, time_to_first_token, and a ``metrics_present``
        flag that is *True* only when the response carried a non-empty metrics
        dict.
        """

        state = self._state(task_id)
        usage_log: list[dict] = state.setdefault("model_usage", [])
        record: dict[str, Any] = {
            "task_id": task_id,
            "call_kind": call_kind,
            "actor_id": actor_id,
            "model": None,
            "model_provider": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "cost": None,
            "duration": 0.0,
            "time_to_first_token": 0.0,
            "metrics_present": False,
        }
        metrics_obj = getattr(response, "metrics", None)
        if metrics_obj is not None:
            try:
                raw = metrics_obj.to_dict() if callable(getattr(metrics_obj, "to_dict", None)) else {}
            except Exception:
                raw = {}
            if raw and isinstance(raw, dict):
                record["metrics_present"] = True
                record["model"] = raw.get("model") or raw.get("model_id")
                record["model_provider"] = raw.get("model_provider") or raw.get("provider")
                record["input_tokens"] = int(raw.get("input_tokens") or raw.get("prompt_tokens") or 0)
                record["output_tokens"] = int(raw.get("output_tokens") or raw.get("completion_tokens") or 0)
                record["total_tokens"] = int(raw.get("total_tokens") or (record["input_tokens"] + record["output_tokens"]))
                record["cache_read_tokens"] = int(raw.get("cache_read_input_tokens") or raw.get("cache_read_tokens") or 0)
                record["cache_write_tokens"] = int(raw.get("cache_creation_input_tokens") or raw.get("cache_write_tokens") or 0)
                record["reasoning_tokens"] = int(raw.get("reasoning_tokens") or 0)
                if "cost" in raw:
                    record["cost"] = float(raw["cost"])
                elif "total_cost" in raw:
                    record["cost"] = float(raw["total_cost"])
                record["duration"] = float(raw.get("duration") or raw.get("response_time") or 0.0)
                record["time_to_first_token"] = float(raw.get("time_to_first_token") or raw.get("ttft") or 0.0)
        fallback_model = getattr(response, "model", None) or getattr(response, "model_id", None)
        if fallback_model and not record["model"]:
            record["model"] = str(fallback_model)
        fallback_provider = getattr(response, "model_provider", None)
        if fallback_provider and not record["model_provider"]:
            record["model_provider"] = str(fallback_provider)
        usage_log.append(record)

    def model_usage_summary(self, task_id: str) -> dict:
        """Deterministically sum model-usage calls for *task_id*.

        Returns a dict with aggregate calls, token counts, cost, and duration.
        ``usage_complete`` is *False* only when at least one successful model
        response lacked metrics data (``metrics_present`` is *False*).  When
        no calls have been recorded the summary is zeroed and ``usage_complete``
        is *True* (nothing is missing).
        """

        state = self._state(task_id)
        records: list[dict] = state.get("model_usage", [])
        total_calls = len(records)
        return {
            "task_id": task_id,
            "total_calls": total_calls,
            "input_tokens": sum(r.get("input_tokens", 0) for r in records),
            "output_tokens": sum(r.get("output_tokens", 0) for r in records),
            "total_tokens": sum(r.get("total_tokens", 0) for r in records),
            "cache_read_tokens": sum(r.get("cache_read_tokens", 0) for r in records),
            "cache_write_tokens": sum(r.get("cache_write_tokens", 0) for r in records),
            "reasoning_tokens": sum(r.get("reasoning_tokens", 0) for r in records),
            "cost": (
                sum(r["cost"] for r in records)
                if records and all(r.get("cost") is not None for r in records)
                else None
            ),
            "cost_complete": bool(records) and all(r.get("cost") is not None for r in records),
            "duration": sum(r.get("duration", 0.0) for r in records),
            "usage_complete": not any(not r.get("metrics_present", False) for r in records),
        }

    async def _run_governance_tool(
        self,
        task: TaskRun,
        actor_identity: SocietyAgent,
        tool_func: Any,
        tool_name: str,
        schema_class: Type[T],
        prompt: str,
        extra_instructions: list[str] | None = None,
    ) -> T:
        """Run a governance tool call via native Agno and return the validated result.

        Emits a tool_call event with mode and success status.
        Raises GovernanceToolError on failure.
        """
        instructions = [
            *NATIVE_TOOL_INSTRUCTIONS,
            f"Use only valid agent ids from this roster: {', '.join(self.agents.keys())}.",
            *(extra_instructions or []),
        ]
        self._state(task.id)["current_actor"] = actor_identity.id

        agno_agent = build_agno_agent(
            identity=actor_identity,
            settings=self.settings,
            tools=[tool_func],
            tool_choice={"type": "function", "function": {"name": tool_name}},
            extra_instructions=instructions,
            tool_call_limit=1,
            session_id=task.id,
            session_state=self._state(task.id),
        )

        result_mode = "native_agno"
        try:
            response = await self._call_provider(
                lambda: agno_agent.arun(prompt),
                task_id=task.id,
                actor=actor_identity.id,
                operation=tool_name,
            )
        except asyncio.TimeoutError as exc:
            self._emit_tool_call(task.id, tool_name, actor_identity.id, prompt[:200], {}, "native_agno", False)
            raise GovernanceToolError(f"Timeout calling {tool_name}: {exc}") from exc
        except Exception as exc:
            self._emit_tool_call(task.id, tool_name, actor_identity.id, prompt[:200], {}, "native_agno", False)
            raise GovernanceToolError(f"Agent run failed for {tool_name}: {exc}") from exc

        self._capture_model_usage(task.id, response, "governance_tool", actor_identity.id)
        try:
            result = _extract_tool_result(response, tool_name, schema_class)
        except ValueError as exc:
            retry_prompt = f"{prompt}\n\n{_json_only_retry_instruction(tool_name, schema_class)}"
            try:
                retry_response = await self._call_provider(
                    lambda: agno_agent.arun(retry_prompt),
                    task_id=task.id,
                    actor=actor_identity.id,
                    operation=f"{tool_name}_structured_retry",
                )
                self._capture_model_usage(task.id, retry_response, "governance_tool_retry", actor_identity.id)
                result = _extract_tool_result(retry_response, tool_name, schema_class)
            except (asyncio.TimeoutError, ValueError) as retry_exc:
                try:
                    result = await self._run_schema_json_retry(
                        task, actor_identity, tool_name, schema_class, prompt
                    )
                    result_mode = "structured_output_recovered"
                except (asyncio.TimeoutError, ValueError) as json_retry_exc:
                    retry_exc = json_retry_exc
                else:
                    result_dict = result.model_dump()
                    self._emit_tool_call(task.id, tool_name, actor_identity.id, retry_prompt[:200], result_dict, result_mode, True)
                    return result
                self._emit_tool_call(task.id, tool_name, actor_identity.id, retry_prompt[:200], {}, "native_agno", False)
                raise GovernanceToolError(
                    f"Could not extract valid tool result for {tool_name} after retry: {retry_exc}"
                ) from retry_exc
            except Exception as retry_exc:
                self._emit_tool_call(task.id, tool_name, actor_identity.id, retry_prompt[:200], {}, "native_agno", False)
                raise GovernanceToolError(f"Agent retry failed for {tool_name}: {retry_exc}") from retry_exc

        result_dict = result.model_dump()
        self._emit_tool_call(task.id, tool_name, actor_identity.id, prompt[:200], result_dict, result_mode, True)
        return result

    async def _run_schema_json_retry(
        self,
        task: TaskRun,
        actor_identity: SocietyAgent,
        tool_name: str,
        schema_class: Type[T],
        prompt: str,
    ) -> T:
        """Recover a Qwen governance decision through strict JSON, not a fake vote.

        DashScope can ignore forced function calls while still returning a valid
        JSON decision. This deliberately uses a separate no-tools request so
        the response transport is unambiguous, then accepts only an exact
        schema-valid object.
        """

        instructions = [
            f"You are {actor_identity.name}, a specialist in {', '.join(actor_identity.skills)}.",
            _json_only_retry_instruction(tool_name, schema_class),
        ]
        agent = Agent(
            name=actor_identity.name,
            role=actor_identity.role,
            model=build_model(self.settings),
            output_schema=schema_class,
            structured_outputs=True,
            instructions=instructions,
            markdown=False,
            session_id=f"{task.id}:json-retry:{actor_identity.id}:{tool_name}",
        )
        response = await self._call_provider(
            lambda: agent.arun(prompt),
            task_id=task.id,
            actor=actor_identity.id,
            operation=f"{tool_name}_schema_recovery",
        )
        self._capture_model_usage(task.id, response, "schema_json_retry", actor_identity.id)
        return _extract_tool_result(response, tool_name, schema_class)

    async def _run_governance_tool_isolated(
        self,
        task: TaskRun,
        actor_identity: SocietyAgent,
        tool_func: Any,
        tool_name: str,
        schema_class: Type[T],
        prompt: str,
        derived_session_id: str,
        state_snapshot: dict[str, Any],
        extra_instructions: list[str] | None = None,
    ) -> T:
        """Run a governance tool call without mutating live state or emitting events.

        Each concurrent caller receives a unique ``derived_session_id`` and a
        deep-copied ``state_snapshot`` so parallel model calls cannot observe or
        corrupt each other. Timeout and fallback semantics match the live path.
        """

        instructions = [
            *NATIVE_TOOL_INSTRUCTIONS,
            f"Use only valid agent ids from this roster: {', '.join(self.agents.keys())}.",
            *(extra_instructions or []),
        ]

        agno_agent = build_agno_agent(
            identity=actor_identity,
            settings=self.settings,
            tools=[tool_func],
            tool_choice={"type": "function", "function": {"name": tool_name}},
            extra_instructions=instructions,
            tool_call_limit=1,
            session_id=derived_session_id,
            session_state=state_snapshot,
        )

        try:
            response = await self._call_provider(
                lambda: agno_agent.arun(prompt),
                task_id=task.id,
                actor=actor_identity.id,
                operation=f"{tool_name}_isolated",
            )
        except asyncio.TimeoutError as exc:
            raise GovernanceToolError(f"Timeout calling {tool_name}: {exc}") from exc
        except Exception as exc:
            raise GovernanceToolError(f"Agent run failed for {tool_name}: {exc}") from exc

        self._capture_model_usage(task.id, response, "governance_tool_isolated", actor_identity.id)
        try:
            result = _extract_tool_result(response, tool_name, schema_class)
        except ValueError as exc:
            retry_prompt = f"{prompt}\n\n{_json_only_retry_instruction(tool_name, schema_class)}"
            try:
                retry_response = await self._call_provider(
                    lambda: agno_agent.arun(retry_prompt),
                    task_id=task.id,
                    actor=actor_identity.id,
                    operation=f"{tool_name}_isolated_retry",
                )
                self._capture_model_usage(task.id, retry_response, "governance_tool_isolated_retry", actor_identity.id)
                result = _extract_tool_result(retry_response, tool_name, schema_class)
            except (asyncio.TimeoutError, ValueError) as retry_exc:
                raise GovernanceToolError(
                    f"Could not extract valid tool result for {tool_name} after retry: {retry_exc}"
                ) from retry_exc
            except Exception as retry_exc:
                raise GovernanceToolError(f"Agent retry failed for {tool_name}: {retry_exc}") from exc

        return result

    def _emit_tool_call(
        self,
        task_id: str,
        tool_name: str,
        actor: str,
        input_summary: str,
        result: dict,
        mode: str,
        success: bool,
    ) -> None:
        state = self.session_states.get(task_id)
        if state is not None:
            metrics = state.setdefault("metrics", {})
            metrics["tool_calls"] = metrics.get("tool_calls", 0) + 1
            if not success:
                metrics["tool_calls_failed"] = metrics.get("tool_calls_failed", 0) + 1
            if tool_name == "memory_write":
                metrics["memory_writes"] = metrics.get("memory_writes", 0) + 1
        payload = ToolCallPayload(
            tool_name=tool_name,
            actor=actor,
            input_summary=input_summary,
            result=result,
            mode=mode,
            success=success,
        )
        self._emit(task_id, "tool_call", f"Tool call: {tool_name}", actor=actor, payload=payload.model_dump())

    def _public_room(self, task_id: str) -> dict[str, list[dict]]:
        """Return the public social room for product-visible society behavior."""

        return self._state(task_id).setdefault(
            "public_room",
            {
                "positions": [],
                "objections": [],
                "endorsements": [],
                "mind_changes": [],
                "published_private_notes": [],
                "collaboration_actions": [],
            },
        )

    def _record_position(self, task_id: str, position: AgentPosition) -> None:
        """Persist and emit one public agent position."""

        if not self.settings.social_tools_enabled:
            return
        payload = position.model_dump()
        self._public_room(task_id).setdefault("positions", []).append(payload)
        if not self.settings.social_trace_enabled:
            return
        agent = self.agents.get(position.agent_id)
        name = agent.name if agent else position.agent_id
        self._emit(
            task_id,
            "agent_position_stated",
            f"{name} is {position.stance} during {position.phase}.",
            actor=position.agent_id,
            payload=payload,
        )

    def _record_position_from_discussion(self, task_id: str, statement: GoalDiscussionStatement) -> None:
        """Map a discussion statement into the public social trace."""

        if not self.settings.social_tools_enabled:
            return
        stance_map = {
            "builds_on": "support",
            "challenges": "oppose",
            "clarifies": "uncertain",
            "blocks": "block",
        }
        conditions = statement.success_criteria or statement.concerns
        self._record_position(
            task_id,
            AgentPosition(
                agent_id=statement.agent_id,
                phase="goal_discussion",
                stance=stance_map.get(statement.stance, "uncertain"),
                target=statement.responds_to,
                reason=statement.unique_contribution or statement.interpretation,
                confidence=0.65 if statement.stance != "blocks" else 0.9,
                conditions=conditions,
            ),
        )

    def _record_objection(self, task_id: str, objection: ObjectionRecord) -> None:
        """Persist and emit a typed objection.

        Objections carry governance truth (blocking vs non-blocking dissent),
        so the event is always emitted when social tools are enabled. The
        social_trace flag only gates verbose non-critical social signals.
        """

        if not self.settings.social_tools_enabled:
            return
        payload = objection.model_dump()
        self._public_room(task_id).setdefault("objections", []).append(payload)
        agent = self.agents.get(objection.agent_id)
        name = agent.name if agent else objection.agent_id
        self._emit(
            task_id,
            "agent_objection_registered",
            f"{name} raised a {objection.severity} objection.",
            actor=objection.agent_id,
            payload=payload,
        )

    def _record_endorsement(self, task_id: str, endorsement: EndorsementRecord) -> None:
        """Persist and emit a public endorsement."""

        if not self.settings.social_tools_enabled:
            return
        payload = endorsement.model_dump()
        self._public_room(task_id).setdefault("endorsements", []).append(payload)
        if not self.settings.social_trace_enabled:
            return
        agent = self.agents.get(endorsement.agent_id)
        endorsed = self.agents.get(endorsement.endorsed_agent_id)
        actor_name = agent.name if agent else endorsement.agent_id
        endorsed_name = endorsed.name if endorsed else endorsement.endorsed_agent_id
        self._emit(
            task_id,
            "agent_endorsed_peer",
            f"{actor_name} deferred to {endorsed_name} for {endorsement.domain}.",
            actor=endorsement.agent_id,
            payload=payload,
        )

    def _record_mind_change(self, task_id: str, mind_change: MindChangeRecord) -> None:
        """Persist and emit one explicit stance change."""

        if not self.settings.social_tools_enabled:
            return
        payload = mind_change.model_dump()
        self._public_room(task_id).setdefault("mind_changes", []).append(payload)
        if not self.settings.social_trace_enabled:
            return
        agent = self.agents.get(mind_change.agent_id)
        actor_name = agent.name if agent else mind_change.agent_id
        self._emit(
            task_id,
            "agent_changed_mind",
            f"{actor_name} changed position after new input.",
            actor=mind_change.agent_id,
            payload=payload,
        )

    def _private_agent_state(self, task_id: str, agent_id: str) -> dict[str, list[dict]]:
        """Return one agent's private task-local state."""

        private_state = self._state(task_id).setdefault("private_agent_state", {})
        return private_state.setdefault(agent_id, {"private_notes": []})

    def _record_private_note(self, task_id: str, note: PrivateNote) -> None:
        """Persist one private note without exposing its content publicly."""

        if not self.settings.social_tools_enabled:
            return
        self._private_agent_state(task_id, note.agent_id).setdefault("private_notes", []).append(note.model_dump())

    def _publish_private_note(self, task_id: str, note: PrivateNote) -> None:
        """Move a selected private note into the public room."""

        if not self.settings.social_tools_enabled:
            return
        published = note.model_copy(update={"may_publish": True})
        self._public_room(task_id).setdefault("published_private_notes", []).append(published.model_dump())
        self._emit_published_private_note(task_id, published)

    def _emit_published_private_note(self, task_id: str, note: PrivateNote) -> None:
        """Emit a public trace event for a note an agent chose to share."""

        if not self.settings.social_trace_enabled:
            return
        agent = self.agents.get(note.agent_id)
        actor_name = agent.name if agent else note.agent_id
        self._emit(
            task_id,
            "private_note_published",
            f"{actor_name} published a private working note.",
            actor=note.agent_id,
            payload=note.model_dump(),
        )

    def _record_collaboration_action(self, task_id: str, action: CollaborationAction) -> None:
        """Persist and emit a human-like collaboration move."""

        if not self.settings.social_tools_enabled:
            return
        payload = action.model_dump()
        self._public_room(task_id).setdefault("collaboration_actions", []).append(payload)
        if not self.settings.social_trace_enabled:
            return
        event_type = {
            "help_requested": "agent_help_requested",
            "ownership_deferred": "agent_deferred_ownership",
            "coalition_joined": "agent_joined_coalition",
        }[action.action]
        agent = self.agents.get(action.agent_id)
        target = self.agents.get(action.target_agent_id or "")
        actor_name = agent.name if agent else action.agent_id
        target_name = target.name if target else action.target_agent_id
        message = f"{actor_name} recorded {action.action.replace('_', ' ')}."
        if target_name:
            message = f"{actor_name} recorded {action.action.replace('_', ' ')} with {target_name}."
        self._emit(task_id, event_type, message, actor=action.agent_id, payload=payload)

    def _record_failure_recovery(self, task_id: str, error: str, state: dict[str, Any]) -> None:
        """Record a bounded recovery handoff before a failed run is surfaced."""

        roster = [item.get("id") for item in state.get("roster", []) if isinstance(item, dict) and item.get("id")]
        current = state.get("current_actor")
        recovery_agent = next((agent_id for agent_id in roster if agent_id != current), roster[0] if roster else None)
        payload = {
            "phase": state.get("phase", "unknown"),
            "failed_actor": current,
            "recovery_agent": recovery_agent,
            "error": error[:500],
            "safe_to_continue": False,
            "recovery_action": (
                "Ask another agent to inspect the failed step and continue with caveats "
                "only after the external blocker is resolved."
            ),
        }
        state.setdefault("failure_recovery", []).append(payload)
        self._emit(
            task_id,
            "failure_recovery_attempted",
            "The society assigned a recovery owner before surfacing the blocker.",
            actor=recovery_agent,
            payload=payload,
        )

    def _diagnose_failure(self, task_id: str) -> dict[str, list[str]]:
        """Diagnose missing inputs and evidence from current session state.

        Returns a dict with 'missing_inputs' and 'missing_evidence' lists
        derived from working brief, readiness blockers, delegation state,
        research evidence, coordination brief truth signals, and failed
        acceptance checks.
        """

        state = self._state(task_id)
        missing_inputs: list[str] = []
        missing_evidence: list[str] = []

        brief = state.get("working_brief")
        if isinstance(brief, dict):
            for q in brief.get("open_questions", []):
                text = str(q).strip()
                if text:
                    missing_inputs.append(f"open question: {text}")
            for b in brief.get("blocked_items", []):
                text = str(b).strip()
                if text:
                    missing_inputs.append(f"blocked item: {text}")

        blockers = state.get("readiness_tally", {}).get("blockers", [])
        for b in blockers:
            text = str(b).strip()
            if text:
                missing_inputs.append(f"readiness blocker: {text}")

        clarification = state.get("user_clarification")
        if isinstance(clarification, dict) and clarification.get("status") == "requested":
            question = str(clarification.get("question", "")).strip()
            if question:
                missing_inputs.append(f"user clarification needed: {question}")

        coord_brief = state.get("team_coordination_brief")
        if isinstance(coord_brief, dict):
            for gap in coord_brief.get("evidence_gaps", []):
                text = str(gap).strip()
                if text:
                    missing_evidence.append(f"coordination evidence gap: {text}")
            for risk in coord_brief.get("risk_signals", []):
                text = str(risk).strip()
                if text:
                    missing_inputs.append(f"coordination risk: {text}")

        research_evidence = state.get("research_evidence", [])
        for evidence in research_evidence:
            if isinstance(evidence, dict) and not evidence.get("success"):
                error_text = str(evidence.get("error", "evidence collection failed"))
                missing_evidence.append(f"research evidence: {error_text}")

        for subtask in state.get("subtasks", []):
            if not isinstance(subtask, dict):
                continue
            task_blockers = subtask.get("blockers", [])
            for b in task_blockers:
                text = str(b).strip()
                if text:
                    missing_evidence.append(f"subtask {subtask.get('id', '?')}: {text}")

        for check in state.get("failed_checks", []):
            if isinstance(check, dict):
                reason = str(check.get("reason", "")).strip()
                check_name = str(check.get("check", "")).strip()
                if reason:
                    missing_evidence.append(f"failed check {check_name}: {reason}")

        seen_inputs: set[str] = set()
        deduped_inputs: list[str] = []
        for item in missing_inputs:
            if item not in seen_inputs:
                seen_inputs.add(item)
                deduped_inputs.append(item)

        seen_evidence: set[str] = set()
        deduped_evidence: list[str] = []
        for item in missing_evidence:
            if item not in seen_evidence:
                seen_evidence.add(item)
                deduped_evidence.append(item)

        return {
            "missing_inputs": deduped_inputs[:10],
            "missing_evidence": deduped_evidence[:10],
        }

    def _record_leader_endorsements(self, task_id: str, team: Team, leader_id: str, reason: str) -> None:
        """Record lightweight deferral signals after leader election."""

        if not self.settings.social_tools_enabled:
            return
        for agent_id in team.member_ids:
            if agent_id == leader_id:
                continue
            agent = self.agents.get(agent_id)
            leader = self.agents.get(leader_id)
            if agent is None or leader is None:
                continue
            self._record_endorsement(
                task_id,
                EndorsementRecord(
                    agent_id=agent_id,
                    endorsed_agent_id=leader_id,
                    domain="task leadership",
                    reason=f"Defers to {leader.name} as elected coordinator: {reason}",
                    confidence=0.75,
                ),
            )
            self._state(task_id).setdefault("trust_updates", []).append(
                TrustUpdate(
                    evaluator_id=agent_id,
                    target_agent_id=leader_id,
                    domain="endorsement",
                    delta=0.02,
                    reason=f"Deferred to {leader.name} for task leadership.",
                ).model_dump()
            )
            self._record_collaboration_action(
                task_id,
                CollaborationAction(
                    agent_id=agent_id,
                    action="ownership_deferred",
                    target_agent_id=leader_id,
                    phase="leader_election",
                    reason=f"{agent.name} lets {leader.name} coordinate because {reason}.",
                    confidence=0.75,
                ),
            )

    def _record_trust_update(self, task_id: str, update: TrustUpdate) -> None:
        """Persist and emit a contextual trust signal."""

        if not self.settings.social_tools_enabled:
            return
        payload = update.model_dump()
        self._state(task_id).setdefault("trust_updates", []).append(payload)
        if not self.settings.social_trace_enabled:
            return
        self._emit(
            task_id,
            "trust_updated",
            f"{update.evaluator_id} updated trust for {update.target_agent_id}.",
            actor=update.evaluator_id,
            payload=payload,
        )

    def _leadership_score(self, agent_id: str, task_class: str) -> float:
        """Return the active leadership score for a task class."""

        if not self.settings.contextual_trust_enabled:
            return self.reputation.election_score(agent_id, self.agents[agent_id].reputation)
        return self.reputation.contextual_election_score(agent_id, task_class, self.agents[agent_id].reputation)

    async def _elect_leader(self, task: TaskRun, team: Team) -> None:
        state = self._state(task.id)
        if self.settings.pre_execution_conversation_enabled:
            if not state.get("ready_to_proceed"):
                self._emit(task.id, "leader_election_blocked", "Leader election blocked: readiness not achieved.", payload={"ready_to_proceed": state.get("ready_to_proceed")})
                return
            brief = state.get("working_brief")
            brief_summary = brief.get("summary", "") if isinstance(brief, dict) else ""
            self._emit(task.id, "leader_election_started", "Leader election started after readiness.", payload={"working_brief_summary": brief_summary})
        if self.settings.efficient_society_enabled or not self.settings.llm_enabled:
            task_class = str(state.get("task_class") or "planning")
            leader_id = max(team.member_ids, key=lambda aid: self._leadership_score(aid, task_class) + len(self.agents[aid].skills) * 0.05)
            team.leader_id = leader_id
            state = self._state(task.id)
            state["phase"] = "leader_elected"
            state["leader_id"] = leader_id
            state["reputations"] = self._reputation_snapshot(team.member_ids)
            state["metrics"]["governance_rounds"] += 1
            reason = f"computed contextual capability score for {task_class}"
            self._emit(task.id, "leader_elected", f"{self.agents[leader_id].name} was elected task leader.", actor=leader_id, payload={"reason": reason, "task_class": task_class, "leadership_score": self._leadership_score(leader_id, task_class)})
            self._emit_tool_call(task.id, "elect_leader", leader_id, "computed", {"leader_id": leader_id, "reason": reason, "confidence": 1.0}, "deterministic_no_key", True)
            self._record_leader_endorsements(task.id, team, leader_id, "deterministic election score")
            return

        roster = ", ".join(f"{self.agents[aid].id}({self.agents[aid].name}, skills={self.agents[aid].skills})" for aid in team.member_ids)
        reputations = self._reputation_snapshot(team.member_ids)
        task_class = str(state.get("task_class") or "planning")
        reputation_lines = ", ".join(
            f"{aid}(election_score={reputations[aid].get('election_score', 1.0):.2f}, contextual_{task_class}={self._leadership_score(aid, task_class):.2f})"
            for aid in team.member_ids
        )
        coordinator = self.agents[team.member_ids[0]]
        brief_context = ""
        if self.settings.pre_execution_conversation_enabled and state.get("working_brief"):
            brief = state["working_brief"]
            brief_context = f"Working brief summary: {brief.get('summary', '')}\nAgreed scope: {brief.get('agreed_scope', '')}\n"
        prompt = (
            f"Task: {task.prompt}\n"
            f"{brief_context}"
            f"Agents on the roster: {roster}\n"
            f"Reputation scores: {reputation_lines}\n"
            f"Pick the best leader for this task from the roster.\n"
            f"Call the elect_leader tool with your decision."
        )

        decision = await self._run_governance_tool(
            task=task,
            actor_identity=coordinator,
            tool_func=elect_leader_tool,
            tool_name="elect_leader",
            schema_class=LeaderDecision,
            prompt=prompt,
        )

        leader_id = decision.leader_id
        if leader_id not in team.member_ids:
            raise GovernanceToolError(f"Elected leader_id '{leader_id}' is not in the team roster: {team.member_ids}")

        team.leader_id = leader_id
        state = self._state(task.id)
        state["phase"] = "leader_elected"
        state["leader_id"] = leader_id
        state["reputations"] = self._reputation_snapshot(team.member_ids)
        state["metrics"]["governance_rounds"] += 1
        self._emit(task.id, "leader_elected", f"{self.agents[leader_id].name} was elected task leader.", actor=leader_id, payload={"reason": decision.reason, "confidence": decision.confidence, "task_class": task_class, "leadership_score": self._leadership_score(leader_id, task_class)})
        self._record_leader_endorsements(task.id, team, leader_id, decision.reason)

    async def _spawn_child_agent(self, task: TaskRun, team: Team) -> None:
        leader = self.agents[team.leader_id or team.member_ids[0]]

        if not self.settings.llm_enabled:
            if len(task.prompt.split()) <= 28:
                self._state(task.id)["spawn_decision"] = {"spawn": False, "reason": "prompt length below threshold"}
                self._emit(task.id, "no_spawn", "Prompt too short; no child agent needed.", actor=leader.id, payload={"reason": "prompt length below threshold"})
                self._emit_tool_call(task.id, "decide_spawn", leader.id, "deterministic", {"spawn": False, "reason": "prompt length below threshold"}, "deterministic_no_key", True)
                return
            self._do_spawn(task, team, leader, "deterministic fallback: long prompt", "Scope Reduction Specialist")
            self._emit_tool_call(task.id, "decide_spawn", leader.id, "deterministic", {"spawn": True, "reason": "deterministic fallback: long prompt"}, "deterministic_no_key", True)
            return

        prompt = (
            f"Task: {task.prompt}\n"
            f"Should a child specialist be spawned for this task?\n"
            f"Call the decide_spawn tool with your decision."
        )

        decision = await self._run_governance_tool(
            task=task,
            actor_identity=leader,
            tool_func=decide_spawn_tool,
            tool_name="decide_spawn",
            schema_class=SpawnDecision,
            prompt=prompt,
        )

        if not decision.spawn:
            self._state(task.id)["spawn_decision"] = {"spawn": False, "reason": decision.reason}
            self._emit(task.id, "no_spawn", "Leader decided no child agent needed.", actor=leader.id, payload={"reason": decision.reason})
            return
        self._do_spawn(task, team, leader, decision.reason, decision.specialist_role or "Task Specialist")

    def _do_spawn(self, task: TaskRun, team: Team, leader: SocietyAgent, reason: str, specialist_role: str) -> None:
        normalized_role = specialist_role.strip().casefold()
        if any(
            str(item.get("role", "")).strip().casefold() == normalized_role
            for item in self._state(task.id).get("child_agents", [])
            if isinstance(item, dict)
        ):
            self._emit(
                task.id,
                "specialist_spawn_skipped",
                "A duplicate specialist role was not spawned.",
                actor=leader.id,
                payload={"specialist_role": specialist_role, "reason": "duplicate_role"},
            )
            return
        child_id = f"child-{uuid4().hex[:8]}"
        role_words = [part.capitalize() for part in specialist_role.replace("-", " ").split()[:2]]
        child_name = "".join(word[0] for word in role_words) or "SP"
        child = SocietyAgent(
            id=child_id,
            name=f"{child_name}-{child_id[-4:]}",
            role=specialist_role,
            skills=self._skills_for_specialist(specialist_role),
            profile=self._profile_for_specialist(specialist_role, leader.id),
            parent_id=leader.id,
            memory=[f"Spawned during task {task.id} to reduce workload."],
        )
        self.agents[child.id] = child
        team.member_ids.append(child.id)
        state = self._state(task.id)
        child_record = child.model_dump()
        registration = get_role_capabilities(resolve_role_key(child))
        child_record["capability_registration"] = registration.model_dump() if registration else None
        child_record["selection_reason"] = reason
        state.setdefault("child_agents", []).append(child_record)
        state["spawn_decision"] = {"spawn": True, "reason": reason, "child_id": child.id, "specialist_role": specialist_role}
        self._emit(task.id, "child_agent_spawned", "The leader spawned a specialist child agent.", actor=leader.id, payload={
            "child": child_record,
            "reason": reason,
            "can_vote": bool(registration and registration.can_vote),
            "capabilities": registration.capabilities if registration else child.skills,
        })

    async def _delegate_subtasks(self, task: TaskRun, team: Team) -> None:
        """Assign and report planned subtasks behind the delegation feature flag.

        Planned subtasks are created by capability tools or derived from the
        structured team coordination brief. The coordination brief is the
        primary delegation input when it contains typed subtask hints; the
        orchestrator enriches them with provenance and done criteria before
        running the assignment tool.
        """

        if not self.settings.delegation_tools_enabled:
            return
        state = self._state(task.id)
        subtasks = state.setdefault("subtasks", [])
        planned = [
            subtask
            for subtask in subtasks
            if subtask.get("status") == "planned" and subtask.get("agent_id") in team.member_ids
        ]
        coord_brief = state.get("team_coordination_brief")
        if not planned and coord_brief and isinstance(coord_brief, dict):
            for item in coord_brief.get("proposed_subtasks", []):
                if not isinstance(item, dict):
                    continue
                agent_id = item.get("agent_id", "")
                if agent_id not in team.member_ids:
                    continue
                subtask_text = item.get("subtask", str(item))
                if not subtask_text:
                    continue
                done_criteria = list(item.get("done_criteria", []))
                why_assigned = str(item.get("why_assigned", ""))
                blocking = bool(item.get("blocking_if_missing", False))
                subtasks.append({
                    "id": f"subtask-{uuid4().hex[:10]}",
                    "agent_id": agent_id,
                    "subtask": subtask_text,
                    "status": "planned",
                    "done_criteria": done_criteria,
                    "why_assigned": why_assigned,
                    "blocking_if_missing": blocking,
                    "source": "coordination_brief",
                    "provenance": "coordination_brief",
                })
            planned = [
                s for s in subtasks
                if s.get("status") == "planned" and s.get("agent_id") in team.member_ids
            ]
        if not planned and coord_brief and isinstance(coord_brief, dict):
            evidence_gaps = coord_brief.get("evidence_gaps", [])
            if evidence_gaps and "researcher" in team.member_ids:
                gap_text = "; ".join(str(gap) for gap in evidence_gaps[:3])
                subtasks.append({
                    "id": f"subtask-{uuid4().hex[:10]}",
                    "agent_id": "researcher",
                    "subtask": f"Resolve evidence gaps identified by the team: {gap_text}",
                    "status": "planned",
                    "done_criteria": [f"Addressed: {gap}" for gap in evidence_gaps[:3]],
                    "why_assigned": "Team coordination brief identified evidence gaps requiring researcher attention",
                    "blocking_if_missing": True,
                    "source": "coordination_brief_evidence_gaps",
                    "provenance": "coordination_brief",
                })
                planned = [
                    s for s in subtasks
                    if s.get("status") == "planned" and s.get("agent_id") in team.member_ids
                ]
        if not planned and self.settings.pre_execution_conversation_enabled and isinstance(state.get("working_brief"), dict):
            brief = state["working_brief"]
            leader_id = team.leader_id or team.member_ids[0]
            owner_id = next((agent_id for agent_id in team.member_ids if agent_id != leader_id), team.member_ids[0])
            subtasks.append({
                "id": f"subtask-{uuid4().hex[:10]}",
                "agent_id": owner_id,
                "subtask": brief.get("agreed_scope") or brief.get("summary") or task.prompt,
                "status": "planned",
                "done_criteria": brief.get("success_criteria", []),
                "source": "working_brief",
            })
            planned = [
                s for s in subtasks
                if s.get("status") == "planned" and s.get("agent_id") in team.member_ids
            ]
        if not planned:
            return
        # Benchmark suites measure analytical coordination against a shared
        # answer contract. Product-delivery keywords inside those fixtures must
        # not trigger the unrelated collaborative-notes execution gate.
        requires_implementation = (
            not self.settings.benchmark_suite_tools_enabled
            and _prompt_requires_implementation(task.prompt)
        )
        has_builder_subtask = any(
            subtask.get("agent_id") == "builder"
            and subtask.get("status") in {"planned", "assigned"}
            for subtask in subtasks
        )
        if requires_implementation and "builder" in team.member_ids and not has_builder_subtask:
            builder_subtask = {
                "id": f"subtask-{uuid4().hex[:10]}",
                "agent_id": "builder",
                "subtask": (
                    "Produce the concrete implementation deliverable for this task: "
                    "the runnable artifact, integration steps, and acceptance checks. "
                    "No mocks, stubs, or simulated behavior."
                ),
                "status": "planned",
                "done_criteria": ["Deliverable is executable or verifiable", "Acceptance checks are explicit"],
                "why_assigned": "User brief requires implementation; builder owns delivery",
                "blocking_if_missing": True,
                "source": "required_brief_deliverable",
                "provenance": "user_brief",
            }
            subtasks.append(builder_subtask)
            planned.append(builder_subtask)
            self._emit(
                task.id,
                "builder_subtask_required",
                "A builder implementation subtask was inserted because the user brief requires a deliverable.",
                actor="builder",
                payload=builder_subtask,
            )
        requires_validation = _prompt_requires_validation(task.prompt)
        has_critic_subtask = any(
            subtask.get("agent_id") == "critic"
            and subtask.get("status") in {"planned", "assigned"}
            for subtask in subtasks
        )
        if requires_validation and "critic" in team.member_ids and not has_critic_subtask:
            critic_subtask = {
                "id": f"subtask-{uuid4().hex[:10]}",
                "agent_id": "critic",
                "subtask": (
                    "Validate the implementation deliverable: run acceptance checks, "
                    "identify failure modes, and confirm quality gates before the final answer."
                ),
                "status": "planned",
                "done_criteria": ["Acceptance checks executed", "Failure modes documented"],
                "why_assigned": "User brief requires validation; critic owns quality gates",
                "blocking_if_missing": True,
                "source": "required_brief_deliverable",
                "provenance": "user_brief",
            }
            subtasks.append(critic_subtask)
            planned.append(critic_subtask)
            self._emit(
                task.id,
                "critic_subtask_required",
                "A critic validation subtask was inserted because the user brief requires validation.",
                actor="critic",
                payload=critic_subtask,
            )
        role_order = {"researcher": 0, "builder": 1, "critic": 2, "architect": 3}
        planned.sort(key=lambda item: role_order.get(str(item.get("agent_id")), 4))
        leader_id = team.leader_id or team.member_ids[0]
        processed: list[dict[str, Any]] = []
        for index, subtask in enumerate(planned, start=1):
            subtask.setdefault("id", f"subtask-{uuid4().hex[:10]}")
            subtask.setdefault("deadline_step", index)
            agent_id = subtask["agent_id"]
            if agent_id not in team.member_ids:
                continue
            agent = self.agents[agent_id]
            role_key = resolve_role_key(agent)
            required_capabilities = subtask.setdefault(
                "required_capabilities",
                default_required_capabilities(role_key),
            )
            existing_done_criteria = subtask.get("done_criteria", [])
            existing_provenance = subtask.get("source", "") or subtask.get("provenance", "")
            existing_why = subtask.get("why_assigned", "")
            existing_blocking = subtask.get("blocking_if_missing", False)
            assignment_prompt = (
                f"Task: {task.prompt}\n"
                f"Assign the planned subtask id {subtask['id']} to agent {agent_id}.\n"
                f"Subtask text: {subtask.get('subtask', '')}\n"
                f"Why assigned: {existing_why}\n"
                f"Done criteria: {existing_done_criteria}\n"
                f"Blocking if missing: {existing_blocking}\n"
                f"Provenance: {existing_provenance or 'leader_plan'}\n"
                "Call assign_subtask with the exact subtask_id, agent_id, subtask text, "
                "a short why_assigned rationale, done_criteria list, blocking_if_missing flag, "
                "and provenance origin."
            )
            if self.settings.efficient_society_enabled:
                assignment = SubtaskAssignment(
                    id=str(subtask["id"]),
                    subtask_id=str(subtask["id"]),
                    status="assigned",
                    agent_id=agent_id,
                    subtask=str(subtask.get("subtask", "")),
                    deadline_step=index,
                    assigned_by=leader_id,
                    why_assigned=existing_why or "Assigned from the typed team coordination brief.",
                    done_criteria=[str(item) for item in existing_done_criteria],
                    depends_on=[],
                    blocking_if_missing=bool(existing_blocking),
                    provenance=existing_provenance or "leader_plan",
                )
            else:
                try:
                    assignment = await self._run_governance_tool(
                        task=task,
                        actor_identity=self.agents[leader_id],
                        tool_func=assign_subtask_tool,
                        tool_name="assign_subtask",
                        schema_class=SubtaskAssignment,
                        prompt=assignment_prompt,
                    )
                except GovernanceToolError as exc:
                    assignment = SubtaskAssignment(
                        id=str(subtask["id"]),
                        subtask_id=str(subtask["id"]),
                        status="assigned",
                        agent_id=agent_id,
                        subtask=str(subtask.get("subtask", "")),
                        deadline_step=index,
                        assigned_by=leader_id,
                        why_assigned=existing_why or f"Recovered from assignment tool issue: {str(exc)[:120]}",
                        done_criteria=[str(item) for item in existing_done_criteria],
                        depends_on=[],
                        blocking_if_missing=bool(existing_blocking),
                        provenance=existing_provenance or "leader_plan",
                    )
                    self._emit_tool_call(task.id, "assign_subtask", leader_id, assignment_prompt[:200], assignment.model_dump(), "native_agno", False)
            update_fields = assignment.model_dump()
            update_fields["id"] = update_fields.get("id") or update_fields.get("subtask_id") or subtask["id"]
            update_fields.pop("subtask_id", None)
            update_fields.pop("subtask", None)
            subtask.update(update_fields)
            resolved_provenance = existing_provenance or assignment.provenance or "leader_plan"
            resolved_why = existing_why or assignment.why_assigned or update_fields.get("why_assigned", "")
            resolved_done_criteria = existing_done_criteria or assignment.done_criteria
            resolved_blocking = existing_blocking or assignment.blocking_if_missing or bool(update_fields.get("blocking_if_missing"))
            self._emit(
                task.id,
                "delegation_assigned",
                f"{self.agents[leader_id].name} assigned subtask {subtask['id']} to {agent.name}.",
                actor=leader_id,
                payload={
                    "subtask_id": subtask["id"],
                    "agent_id": agent_id,
                    "assigned_by": leader_id,
                    "objective": subtask.get("subtask", ""),
                    "why_assigned": resolved_why,
                    "done_criteria": resolved_done_criteria,
                    "blocking_if_missing": resolved_blocking,
                    "required_capabilities": required_capabilities,
                    "provenance": resolved_provenance,
                    "status": "assigned",
                },
            )
            self._record_collaboration_action(
                task.id,
                CollaborationAction(
                    agent_id=agent_id,
                    action="help_requested",
                    target_agent_id=leader_id,
                    phase="subtask_assignment",
                    artifact_id=str(subtask.get("id", "")),
                    reason=(
                        f"{agent.name} asks {self.agents[leader_id].name} to keep the assignment unblocked: "
                        f"{subtask.get('subtask', '')}"
                    ),
                    confidence=0.65,
                ),
            )

            work_prompt = (
                f"Complete this assigned subtask for the team.\n\n"
                f"Main task: {task.prompt}\n"
                f"Subtask id: {subtask['id']}\n"
                f"Subtask: {subtask.get('subtask', '')}\n\n"
                "Return the concrete work product, blockers if any, and the evidence you used. "
                "If you have role-specific tools, use them before answering when they are relevant."
            )
            if _prompt_has_no_mock_constraint(task.prompt):
                work_prompt += (
                    "\n\nCONSTRAINT: The user brief explicitly forbids mocks, fabrication, "
                    "and simulated behavior. Do not recommend or produce mock, stub, fake, "
                    "or offline-simulated deliverables. Use only real evidence and real "
                    "executable artifacts."
                )
            work_result, work_agent, work_agent_id = await self._execute_subtask_with_retry(
                task, team, subtask, agent, agent_id, work_prompt,
            )
            if work_result is None:
                processed.append(subtask)
                continue

            execution_evidence: dict[str, Any] | None = None
            if agent_id == "builder" and _prompt_requires_collaborative_notes_demo(task.prompt):
                evidence_items = self._state(task.id).get("execution_evidence", [])
                execution_evidence = next(
                    (item for item in reversed(evidence_items) if isinstance(item, dict) and item.get("passed")),
                    None,
                )
                execution_evidence = execution_evidence or load_notes_demo_evidence(task.id)
                if execution_evidence is not None and execution_evidence.get("passed"):
                    self._state(task.id).setdefault("execution_evidence", []).append(execution_evidence)
                    self._emit_tool_call(
                        task.id,
                        "execute_notes_demo",
                        agent_id,
                        "architecture=centralized",
                        execution_evidence,
                        "native_agno",
                        True,
                    )
                if execution_evidence is None and requires_implementation:
                    blocker = "Builder did not produce passed execute_notes_demo tool evidence."
                    subtask.update({
                        "status": "blocked",
                        "outcome_status": "blocked",
                        "result_summary": blocker,
                        "outcome_summary": blocker,
                        "blockers": [blocker],
                        "evidence_refs": [],
                        "provenance": "missing_builder_execution_evidence",
                    })
                    self._emit(
                        task.id,
                        "delegation_reported",
                        f"{agent.name} did not produce executable evidence for subtask {subtask['id']}.",
                        actor=agent_id,
                        payload={
                            "subtask_id": subtask["id"], "agent_id": agent_id, "status": "blocked",
                            "result_summary": blocker, "blockers": [blocker], "outcome_status": "blocked",
                            "outcome_summary": blocker, "evidence_refs": [],
                            "provenance": "missing_builder_execution_evidence",
                        },
                    )
                    processed.append(subtask)
                    continue

            report_prompt = (
                f"Task: {task.prompt}\n"
                f"Report the result for subtask {subtask['id']}: {subtask.get('subtask', '')}\n"
                f"Work product to report:\n{work_result[:3000]}\n"
                "Call report_subtask with the exact subtask_id and your agent_id. "
                "Include result_summary (one-line), evidence_refs (list of evidence sources used), "
                "outcome_status (completed, partial, blocked, or failed), "
                "outcome_summary (one-line outcome for cockpit), and provenance."
            )
            if self.settings.efficient_society_enabled:
                concise_result = " ".join(work_result.strip().split())[:240]
                report = SubtaskReport(
                    id=str(subtask["id"]),
                    subtask_id=str(subtask["id"]),
                    status="completed",
                    agent_id=agent_id,
                    result=work_result,
                    result_summary=concise_result,
                    blockers=[],
                    evidence_refs=[],
                    outcome_status="completed",
                    outcome_summary=concise_result,
                    provenance="direct_agent_work_product",
                )
            else:
                try:
                    report = await self._run_governance_tool(
                        task=task,
                        actor_identity=agent,
                        tool_func=report_subtask_tool,
                        tool_name="report_subtask",
                        schema_class=SubtaskReport,
                        prompt=report_prompt,
                    )
                except GovernanceToolError as exc:
                    report = SubtaskReport(
                        id=str(subtask["id"]),
                        subtask_id=str(subtask["id"]),
                        status="blocked",
                        agent_id=agent_id,
                        result=work_result,
                        result_summary=work_result[:240],
                        blockers=[f"report_subtask failed: {str(exc)[:160]}"],
                        evidence_refs=[],
                        outcome_status="blocked",
                        outcome_summary=f"The work product could not be truthfully registered: {str(exc)[:160]}",
                        provenance="agent_report_failure",
                    )
                    self._emit_tool_call(task.id, "report_subtask", agent_id, report_prompt[:200], report.model_dump(), "native_agno", False)
            update_fields = report.model_dump()
            if agent_id == "critic" and load_notes_demo_evidence(task.id):
                identified_risks = [str(item) for item in update_fields.get("blockers", []) if str(item).strip()]
                update_fields["identified_risks"] = identified_risks
                update_fields["blockers"] = []
                update_fields["status"] = "completed"
                update_fields["outcome_status"] = "completed"
                if identified_risks:
                    update_fields["outcome_summary"] = (
                        update_fields.get("outcome_summary")
                        or f"Validation completed with {len(identified_risks)} carried risk finding(s)."
                    )
            if update_fields.get("status") == "not_found":
                update_fields["status"] = "blocked"
                update_fields["blockers"] = [
                    "report_subtask did not match an assigned subtask in session state."
                ]
            subtask.update(update_fields)
            resolved_outcome_status = subtask.get("outcome_status") or report.outcome_status or subtask.get("status", "completed")
            resolved_result_summary = report.result_summary or report.result[:240]
            resolved_outcome_summary = report.outcome_summary or resolved_result_summary
            resolved_provenance = report.provenance or "agent_report"
            resolved_evidence_refs = report.evidence_refs or subtask.get("evidence_refs", [])
            if execution_evidence is not None:
                resolved_evidence_refs = [
                    f"execution:{execution_evidence['artifact_dir']}",
                    *[str(path) for path in execution_evidence.get("files", [])],
                ]
                subtask["evidence_refs"] = resolved_evidence_refs
            self._emit(
                task.id,
                "delegation_reported",
                f"{agent.name} reported on subtask {subtask['id']}.",
                actor=agent_id,
                payload={
                    "subtask_id": subtask["id"],
                    "agent_id": agent_id,
                    "status": subtask.get("status", "completed"),
                    "result_summary": resolved_result_summary,
                    "blockers": subtask.get("blockers", []),
                    "outcome_status": resolved_outcome_status,
                    "outcome_summary": resolved_outcome_summary,
                    "evidence_refs": resolved_evidence_refs,
                    "provenance": resolved_provenance,
                },
            )
            processed.append(subtask)
        if processed:
            self._emit(
                task.id,
                "subtasks_assigned_from_brief",
                "Leader assigned and tracked subtasks for coordinated execution.",
                actor=leader_id,
                payload={
                    "leader_id": leader_id,
                    "subtask_ids": [subtask["id"] for subtask in processed],
                    "assignments": [
                        {
                            "id": subtask.get("id"),
                            "agent_id": subtask.get("agent_id"),
                            "objective": subtask.get("subtask", ""),
                            "status": subtask.get("status"),
                            "done_criteria": subtask.get("done_criteria", []),
                            "why_assigned": subtask.get("why_assigned", ""),
                            "blocking_if_missing": subtask.get("blocking_if_missing", False),
                            "provenance": subtask.get("provenance", subtask.get("source", "")),
                            "result_summary": subtask.get("result_summary", ""),
                            "outcome_status": subtask.get("outcome_status", ""),
                            "evidence_refs": subtask.get("evidence_refs", []),
                        }
                        for subtask in processed
                    ],
                },
            )

    async def _execute_subtask_with_retry(
        self,
        task: TaskRun,
        team: Team,
        subtask: dict[str, Any],
        agent: SocietyAgent,
        agent_id: str,
        work_prompt: str,
    ) -> tuple[str | None, SocietyAgent, str]:
        """Execute the _ask_agent work-product call with durable retry/reassignment.

        Returns (work_result, effective_agent, effective_agent_id) on success,
        or (None, agent, agent_id) when the subtask is exhausted/blocked.
        """

        state = self._state(task.id)
        attempts_store: dict[str, Any] = state.setdefault("subtask_attempts", {})
        idempotency_store: dict[str, str] = state.setdefault("subtask_idempotency_keys", {})
        subtask_id = subtask["id"]
        max_attempts = int(self.settings.subtask_max_attempts)
        self._restore_subtask_attempt_state(task.id, subtask_id, max_attempts)
        attempt_state: dict[str, Any] = attempts_store.setdefault(subtask_id, {
            "attempt_count": 0,
            "max_attempts": max_attempts,
            "attempts": [],
            "status": "pending",
            "agent_id": agent_id,
            "exhaustion_reason": None,
        })
        attempt_state["max_attempts"] = max_attempts

        current_agent = agent
        current_agent_id = agent_id
        if attempt_state.get("status") in {"completed", "blocked"}:
            return None, current_agent, current_agent_id

        any_attempt_executed = False

        while attempt_state["attempt_count"] < attempt_state["max_attempts"]:
            attempt_state["attempt_count"] += 1
            attempt_num = attempt_state["attempt_count"]
            idem_key = make_idempotency_key(task.id, subtask_id, current_agent_id, attempt_num)

            if idem_key in idempotency_store:
                continue
            idempotency_store[idem_key] = "in_progress"
            any_attempt_executed = True

            attempt_record = build_attempt_record(
                subtask_id=subtask_id,
                agent_id=current_agent_id,
                attempt_number=attempt_num,
                max_attempts=attempt_state["max_attempts"],
                idempotency_key=idem_key,
                model=self.settings.active_model,
                provider=str(self.settings.provider),
            )
            attempt_state["attempts"].append(attempt_record)
            usage_start = len(state.get("model_usage", []))

            self._emit(
                task.id,
                "subtask_attempt_started",
                f"Subtask {subtask_id} attempt {attempt_num} by {current_agent_id}.",
                actor=current_agent_id,
                payload={
                    "subtask_id": subtask_id,
                    "agent_id": current_agent_id,
                    "attempt_number": attempt_num,
                    "max_attempts": attempt_state["max_attempts"],
                    "idempotency_key": idem_key,
                },
            )

            try:
                work_result = await self._ask_agent(current_agent, work_prompt, task_id=task.id)
                attempt_record["finished_at"] = time.time()
                attempt_record["duration_seconds"] = round(attempt_record["finished_at"] - attempt_record["started_at"], 3)
                self._populate_attempt_usage(attempt_record, state.get("model_usage", [])[usage_start:])
                attempt_record["category"] = None
                attempt_record["next_action"] = "success"
                idempotency_store[idem_key] = "completed"
                attempt_state["status"] = "completed"
                self._emit(
                    task.id,
                    "subtask_attempt_completed",
                    f"Subtask {subtask_id} attempt {attempt_num} completed.",
                    actor=current_agent_id,
                    payload={
                        "subtask_id": subtask_id,
                        "agent_id": current_agent_id,
                        "attempt_number": attempt_num,
                        "max_attempts": attempt_state["max_attempts"],
                        "idempotency_key": idem_key,
                    },
                )
                return work_result, current_agent, current_agent_id
            except (asyncio.TimeoutError, Exception) as exc:
                attempt_record["finished_at"] = time.time()
                attempt_record["duration_seconds"] = round(attempt_record["finished_at"] - attempt_record["started_at"], 3)
                self._populate_attempt_usage(attempt_record, state.get("model_usage", [])[usage_start:])
                error_text = f"{type(exc).__name__}: {str(exc)[:200]}"
                category = classify_error(exc, work_prompt[:200])
                attempt_record["category"] = category
                attempt_record["error_message"] = error_text

                self._emit_tool_call(
                    task.id,
                    "agent_work_product",
                    current_agent_id,
                    work_prompt[:200],
                    {"result": None, "recovered": False, "error": error_text, "category": category},
                    "native_agno",
                    False,
                )
                self._emit(
                    task.id,
                    "subtask_attempt_failed",
                    f"Subtask {subtask_id} attempt {attempt_num} failed ({category}).",
                    actor=current_agent_id,
                    payload={
                        "subtask_id": subtask_id,
                        "agent_id": current_agent_id,
                        "attempt_number": attempt_num,
                        "category": category,
                        "error": error_text,
                        "idempotency_key": idem_key,
                    },
                )

                if category in ("user_decision", "deterministic"):
                    attempt_record["next_action"] = "stop"
                    attempt_record["exhaustion_reason"] = f"non_retryable:{category}"
                    attempt_state["status"] = "blocked"
                    attempt_state["exhaustion_reason"] = f"non_retryable:{category}"
                    idempotency_store[idem_key] = "exhausted"
                    self._mark_subtask_blocked(task.id, subtask, current_agent_id, error_text, category)
                    self._emit(
                        task.id,
                        "subtask_exhausted",
                        f"Subtask {subtask_id} exhausted: {category} is not retryable.",
                        actor=current_agent_id,
                        payload={
                            "subtask_id": subtask_id,
                            "agent_id": current_agent_id,
                            "category": category,
                            "exhaustion_reason": f"non_retryable:{category}",
                            "attempts": attempt_state["attempt_count"],
                        },
                    )
                    return None, current_agent, current_agent_id

                if category == "capability":
                    reassigned = self._try_reassign_subtask(task, team, subtask, current_agent_id)
                    if reassigned is not None:
                        new_agent_id, new_agent = reassigned
                        attempt_record["next_action"] = "reassigned"
                        idempotency_store[idem_key] = "reassigned"
                        self._emit(
                            task.id,
                            "subtask_reassigned",
                            f"Subtask {subtask_id} reassigned from {current_agent_id} to {new_agent_id}.",
                            actor=current_agent_id,
                            payload={
                                "subtask_id": subtask_id,
                                "from_agent_id": current_agent_id,
                                "to_agent_id": new_agent_id,
                                "reason": f"capability_failure:{error_text[:120]}",
                            },
                        )
                        attempt_state["agent_id"] = new_agent_id
                        current_agent = new_agent
                        current_agent_id = new_agent_id
                        subtask["agent_id"] = new_agent_id
                        continue
                    else:
                        attempt_record["next_action"] = "stop"
                        attempt_record["exhaustion_reason"] = "capability:no_alternative"
                        attempt_state["status"] = "blocked"
                        attempt_state["exhaustion_reason"] = "capability:no_alternative"
                        idempotency_store[idem_key] = "exhausted"
                        self._mark_subtask_blocked(task.id, subtask, current_agent_id, error_text, category)
                        self._emit(
                            task.id,
                            "subtask_exhausted",
                            f"Subtask {subtask_id} exhausted: no capable alternative for capability failure.",
                            actor=current_agent_id,
                            payload={
                                "subtask_id": subtask_id,
                                "agent_id": current_agent_id,
                                "category": category,
                                "exhaustion_reason": "capability:no_alternative",
                                "attempts": attempt_state["attempt_count"],
                            },
                        )
                        return None, current_agent, current_agent_id

                if is_retryable(category) and attempt_state["attempt_count"] < attempt_state["max_attempts"]:
                    delay = compute_backoff(
                        attempt_num,
                        base=self.settings.subtask_backoff_base_seconds,
                        cap=self.settings.subtask_backoff_cap_seconds,
                    )
                    attempt_record["next_action"] = "retry"
                    idempotency_store[idem_key] = "retry_scheduled"
                    self._emit(
                        task.id,
                        "subtask_retry_scheduled",
                        f"Subtask {subtask_id} retry scheduled in {delay}s (attempt {attempt_num + 1}/{attempt_state['max_attempts']}).",
                        actor=current_agent_id,
                        payload={
                            "subtask_id": subtask_id,
                            "agent_id": current_agent_id,
                            "next_attempt": attempt_num + 1,
                            "max_attempts": attempt_state["max_attempts"],
                            "backoff_seconds": delay,
                            "category": category,
                        },
                    )
                    await asyncio.sleep(delay)
                    continue

                attempt_record["next_action"] = "stop"
                attempt_record["exhaustion_reason"] = f"budget_exhausted:{category}"
                attempt_state["status"] = "blocked"
                attempt_state["exhaustion_reason"] = f"budget_exhausted:{category}"
                idempotency_store[idem_key] = "exhausted"
                self._mark_subtask_blocked(task.id, subtask, current_agent_id, error_text, category)
                self._emit(
                    task.id,
                    "subtask_exhausted",
                    f"Subtask {subtask_id} exhausted after {attempt_state['attempt_count']} attempts.",
                    actor=current_agent_id,
                    payload={
                        "subtask_id": subtask_id,
                        "agent_id": current_agent_id,
                        "category": category,
                        "exhaustion_reason": f"budget_exhausted:{category}",
                        "attempts": attempt_state["attempt_count"],
                    },
                )
                return None, current_agent, current_agent_id

        if not any_attempt_executed:
            return None, current_agent, current_agent_id

        attempt_state["status"] = "blocked"
        attempt_state["exhaustion_reason"] = "budget_exhausted:max_attempts_reached"
        self._mark_subtask_blocked(task.id, subtask, current_agent_id, "max_attempts_reached", "transient")
        self._emit(
            task.id,
            "subtask_exhausted",
            f"Subtask {subtask_id} exhausted: max attempts reached.",
            actor=current_agent_id,
            payload={
                "subtask_id": subtask_id,
                "agent_id": current_agent_id,
                "exhaustion_reason": "budget_exhausted:max_attempts_reached",
                "attempts": attempt_state["attempt_count"],
            },
        )
        return None, current_agent, current_agent_id

    def _restore_subtask_attempt_state(
        self,
        task_id: str,
        subtask_id: str,
        max_attempts: int,
    ) -> None:
        """Rebuild attempt budgets from durable events after process restart.

        A started attempt without a terminal event is treated as interrupted and
        consumes its attempt number. This favors at-most-once execution over
        silently repeating potentially side-effecting work.
        """

        state = self._state(task_id)
        attempts_store = state.setdefault("subtask_attempts", {})
        if subtask_id in attempts_store:
            return
        idempotency_store = state.setdefault("subtask_idempotency_keys", {})
        try:
            events = self.events.list(task_id)
        except Exception:
            return
        task_events = [
            event for event in events
            if str(event.payload.get("subtask_id", "")) == subtask_id
        ]
        relevant = [event for event in task_events if event.type.startswith("subtask_attempt_")]
        exhausted_event = next(
            (event for event in reversed(task_events) if event.type == "subtask_exhausted"),
            None,
        )
        if not relevant and exhausted_event is None:
            return
        records: dict[int, dict[str, Any]] = {}
        status = "pending"
        agent_id = ""
        for event in relevant:
            payload = event.payload
            number = int(payload.get("attempt_number", 0) or 0)
            if number <= 0:
                continue
            agent_id = str(payload.get("agent_id") or event.actor or agent_id)
            key = str(payload.get("idempotency_key", ""))
            record = records.setdefault(number, {
                "subtask_id": subtask_id,
                "agent_id": agent_id,
                "attempt_number": number,
                "max_attempts": int(payload.get("max_attempts", max_attempts)),
                "idempotency_key": key,
                "started_at": 0.0,
                "finished_at": None,
                "next_action": "interrupted_after_restart",
            })
            if event.type == "subtask_attempt_started":
                idempotency_store[key] = "interrupted"
                status = "blocked"
            elif event.type == "subtask_attempt_failed":
                record["category"] = payload.get("category")
                record["error_message"] = payload.get("error")
                record["next_action"] = "retry"
                idempotency_store[key] = "retry_scheduled"
                status = "pending"
            elif event.type == "subtask_attempt_completed":
                record["next_action"] = "success"
                idempotency_store[key] = "completed"
                status = "completed"
        exhaustion_reason = None
        if exhausted_event is not None:
            status = "blocked"
            exhaustion_reason = str(
                exhausted_event.payload.get("exhaustion_reason", "attempt_budget_exhausted")
            )
        elif status == "blocked":
            exhaustion_reason = "interrupted_requires_reconciliation"
        attempts_store[subtask_id] = {
            "attempt_count": max(records, default=0),
            "max_attempts": max_attempts,
            "attempts": [records[number] for number in sorted(records)],
            "status": status,
            "agent_id": agent_id,
            "exhaustion_reason": exhaustion_reason,
            "restored_from_events": True,
        }

    @staticmethod
    def _populate_attempt_usage(attempt_record: dict[str, Any], records: list[dict[str, Any]]) -> None:
        """Attach usage from model calls made during one subtask attempt."""

        for field in ("input_tokens", "output_tokens", "total_tokens"):
            attempt_record[field] = sum(
                int(record.get(field) or 0)
                for record in records
                if isinstance(record, dict)
            )

    def _mark_subtask_blocked(
        self,
        task_id: str,
        subtask: dict[str, Any],
        agent_id: str,
        error_text: str,
        category: str,
    ) -> None:
        subtask.update({
            "status": "blocked",
            "outcome_status": "blocked",
            "result_summary": f"Agent work-product call failed after retries ({category}).",
            "outcome_summary": error_text,
            "blockers": [error_text],
            "evidence_refs": [],
            "provenance": f"agent_work_product_failure:{category}",
        })
        self._emit(
            task_id,
            "delegation_reported",
            f"Subtask {subtask.get('id', '?')} blocked after {category} failure.",
            actor=agent_id,
            payload={
                "subtask_id": subtask.get("id", ""),
                "agent_id": agent_id,
                "status": "blocked",
                "result_summary": subtask["result_summary"],
                "blockers": subtask["blockers"],
                "outcome_status": "blocked",
                "outcome_summary": error_text,
                "evidence_refs": [],
                "provenance": subtask["provenance"],
                "error_category": category,
            },
        )

    def _try_reassign_subtask(
        self,
        task: TaskRun,
        team: Team,
        subtask: dict[str, Any],
        failed_agent_id: str,
    ) -> tuple[str, SocietyAgent] | None:
        attempts = self._state(task.id).get("subtask_attempts", {}).get(subtask["id"], {}).get("attempts", [])
        used_ids = {
            str(attempt.get("agent_id"))
            for attempt in attempts
            if isinstance(attempt, dict) and attempt.get("agent_id")
        }
        used_ids.add(failed_agent_id)
        required = [str(item) for item in subtask.get("required_capabilities", [])]
        candidate_id = find_capable_team_member(
            team.member_ids,
            self.agents,
            required,
            exclude_ids=used_ids,
        )
        if candidate_id is None:
            return None
        return candidate_id, self.agents[candidate_id]

    def _skills_for_specialist(self, specialist_role: str) -> list[str]:
        """Derive scoped child-agent skills from the requested specialist role."""

        role = specialist_role.lower()
        skills = ["focused delegation", "status reporting"]
        if "research" in role or "evidence" in role:
            skills.append("evidence gathering")
        if "risk" in role or "critic" in role or "review" in role:
            skills.append("risk analysis")
        if "implement" in role or "build" in role or "engineer" in role:
            skills.append("implementation planning")
        if len(skills) == 2:
            skills.append("scope reduction")
        return skills

    def _profile_for_specialist(self, specialist_role: str, leader_id: str) -> AgentProfile:
        """Derive a behavior profile for a temporary specialist child agent."""

        role = specialist_role.lower()
        if "research" in role or "evidence" in role:
            return AgentProfile(
                values=["focused evidence gathering", "uncertainty reduction", "source quality"],
                communication_style="brief, evidence-first, and explicit about unknowns",
                risk_tolerance="low",
                decision_bias="prefer a small verified answer over a broad unsupported answer",
                default_blockers=["missing evidence", "unclear source quality", "unsupported assumption"],
                defers_to={"researcher": ["evidence quality"], leader_id: ["task priority"]},
                failure_mode="can over-focus on evidence when the task needs a fast bounded answer",
            )
        if "risk" in role or "critic" in role or "review" in role:
            return AgentProfile(
                values=["bounded risk", "validation", "failure-mode discovery"],
                communication_style="skeptical, compact, and blocker-oriented",
                risk_tolerance="low",
                decision_bias="prefer surfacing the strongest blocker before endorsing execution",
                default_blockers=["missing validation", "unbounded risk", "unresolved critical objection"],
                defers_to={"critic": ["quality gates"], leader_id: ["task priority"]},
                failure_mode="can over-block instead of proposing a bounded mitigation",
            )
        if "implement" in role or "build" in role or "engineer" in role:
            return AgentProfile(
                values=["deliverable slices", "clear done criteria", "integration path"],
                communication_style="practical, direct, and output-oriented",
                risk_tolerance="high",
                decision_bias="prefer the smallest runnable step that proves value",
                default_blockers=["unclear deliverable", "missing acceptance check", "unowned integration risk"],
                defers_to={"builder": ["implementation sequencing"], leader_id: ["scope priority"]},
                failure_mode="can move too quickly past ambiguous requirements",
            )
        return AgentProfile(
            values=["scope reduction", "focused execution", "team throughput"],
            communication_style="concise, task-specific, and status-oriented",
            risk_tolerance="medium",
            decision_bias="prefer narrowing the task to a clear owned contribution",
            default_blockers=["unclear ownership", "oversized scope", "missing done criteria"],
            defers_to={leader_id: ["task priority"]},
            failure_mode="can optimize for narrow scope and miss wider context",
        )

    def _role_key(self, agent: SocietyAgent) -> str:
        """Map persistent and spawned agents to a stable product role."""

        role_text = f"{agent.id} {agent.role} {' '.join(agent.skills)}".lower()
        if "research" in role_text or "evidence" in role_text or "memory" in role_text:
            return "researcher"
        if "risk" in role_text or "critic" in role_text or "review" in role_text or "validation" in role_text:
            return "critic"
        if "build" in role_text or "implement" in role_text or "engineer" in role_text:
            return "builder"
        if "architect" in role_text or "system" in role_text or "decompose" in role_text:
            return "architect"
        return "builder"

    def _role_tool_bundle(self, agent: SocietyAgent) -> dict[str, Any]:
        """Return the role-specific tool bundle that shapes agent behavior."""

        specs: dict[str, dict[str, Any]] = {
            "architect": (
                {
                    "primary": (decompose_task_tool, "decompose_task", TaskDecomposition),
                    "support_tools": ["state_position", "endorse_agent", "record_private_note"],
                    "instruction": "Clarify the objective, break it into ordered steps, and assign responsibilities to valid agent ids.",
                    "behavior": "coordinate scope, dependencies, and ownership before implementation starts.",
                }
            ),
            "researcher": (
                {
                    "primary": (memory_lookup_tool, "memory_lookup", MemoryLookup),
                    "support_tools": ["record_private_note", "publish_private_note", "state_position"],
                    "instruction": "Look up relevant collaboration memories and extract the lesson that should guide this task.",
                    "behavior": "reduce uncertainty, cite memory, and publish evidence gaps when they affect the team.",
                }
            ),
            "builder": (
                {
                    "primary": (implementation_plan_tool, "implementation_plan", ImplementationPlan),
                    "support_tools": ["state_position", "change_mind", "record_private_note"],
                    "instruction": "Plan the artifact, milestones, and acceptance checks needed to deliver a usable solution.",
                    "behavior": "turn the brief into a runnable delivery path with explicit checks.",
                }
            ),
            "critic": (
                {
                    "primary": (risk_assessment_tool, "risk_assessment", RiskAssessment),
                    "support_tools": ["register_objection", "evaluate_peer", "publish_private_note"],
                    "instruction": "Identify failure modes, mitigations, and the quality gate required before accepting the solution.",
                    "behavior": "surface blockers, validate mitigations, and update trust only from observed behavior.",
                }
            ),
        }
        role_key = self._role_key(agent) if self.settings.role_specific_tools_enabled else "builder"
        bundle = dict(specs.get(role_key, specs["builder"]))
        bundle["role_key"] = role_key
        return bundle

    async def _use_capability_tool(self, task: TaskRun, agent: SocietyAgent, context: str) -> BaseModel:
        """Run the primary native capability from this agent's role bundle."""

        bundle = self._role_tool_bundle(agent)
        tool_func, tool_name, schema_class = bundle["primary"]
        instruction = str(bundle["instruction"])
        support_tools = list(bundle.get("support_tools", []))
        state = self._state(task.id)
        state.setdefault("tool_bundles", {})[agent.id] = {
            "role_key": bundle["role_key"],
            "primary_tool": tool_name,
            "support_tools": support_tools,
            "behavior": bundle["behavior"],
        }
        self._emit(
            task.id,
            "agent_tool_bundle_selected",
            f"{agent.name} is using the {bundle['role_key']} tool bundle.",
            actor=agent.id,
            payload=state["tool_bundles"][agent.id],
        )
        memory = "\n".join(f"- {item}" for item in agent.memory[-5:]) or "- No prior memories yet."
        prompt = (
            f"Task: {task.prompt}\n"
            f"Agent: {agent.name} ({agent.role})\n"
            f"Role tool bundle: {bundle['role_key']}.\n"
            f"Primary tool: {tool_name}.\n"
            f"Support tools available in other phases: {', '.join(support_tools)}.\n"
            f"Role behavior: {bundle['behavior']}\n"
            f"Recent memory:\n{memory}\n"
            f"{context}\n"
            f"{instruction}\n"
            f"Call the {tool_name} tool with the structured result."
        )
        try:
            result = await self._run_governance_tool(
                task=task,
                actor_identity=agent,
                tool_func=tool_func,
                tool_name=tool_name,
                schema_class=schema_class,
                prompt=prompt,
                extra_instructions=[instruction, f"Stay inside your {bundle['role_key']} tool bundle behavior: {bundle['behavior']}"],
            )
        except GovernanceToolError as exc:
            if schema_class is TaskDecomposition:
                result = TaskDecomposition(
                    objective=task.prompt[:300],
                    steps=[
                        "Clarify the required output.",
                        "Assign one owner per required part.",
                        "Define acceptance checks and caveats.",
                    ],
                    delegation_plan={agent.id: task.prompt[:200]},
                )
            elif schema_class is MemoryLookup:
                result = MemoryLookup(
                    query=task.prompt[:200],
                    relevant_memories=[],
                    lesson=f"Recovered from memory lookup issue: {str(exc)[:160]}",
                )
            elif schema_class is ImplementationPlan:
                result = ImplementationPlan(
                    artifact=task.prompt[:160],
                    milestones=["Draft the answer", "Attach owners and checks", "Carry caveats into the final synthesis"],
                    acceptance_checks=["Final answer names owners", "Final answer includes acceptance checks", "Final answer labels evidence gaps"],
                )
            elif schema_class is RiskAssessment:
                result = RiskAssessment(
                    risks=[f"Recovered from risk tool issue: {str(exc)[:160]}"],
                    mitigations=["Proceed with an explicit caveat and review the output before treating it as final."],
                    quality_gate="Final answer must preserve caveats from recovered tool calls.",
                )
            else:
                raise
            self._emit_tool_call(task.id, tool_name, agent.id, prompt[:200], result.model_dump(), "native_agno", False)
        if isinstance(result, TaskDecomposition):
            self._record_planned_subtasks(task.id, result)
        return result

    def _record_planned_subtasks(self, task_id: str, decomposition: TaskDecomposition) -> None:
        """Persist planned subtasks from a validated decomposition result."""

        state = self._state(task_id)
        subtasks = state.setdefault("subtasks", [])
        for agent_id, subtask in decomposition.delegation_plan.items():
            if not isinstance(agent_id, str) or agent_id not in self.agents:
                continue
            if any(
                item.get("agent_id") == agent_id
                and item.get("subtask") == subtask
                and item.get("status") in {"planned", "assigned", "completed", "blocked"}
                for item in subtasks
            ):
                continue
            subtasks.append(
                {
                    "id": f"subtask-{uuid4().hex[:10]}",
                    "agent_id": agent_id,
                    "subtask": subtask,
                    "status": "planned",
                }
            )

    def _readable_proposal_text(self, capability_result: Any) -> str:
        """Extract a human-readable summary from a capability tool result.

        Default-mode proposals must be readable in Review. Raw capability
        output is stored separately when useful.
        """

        if isinstance(capability_result, str):
            text = capability_result
        else:
            text = capability_result.model_dump_json() if hasattr(capability_result, "model_dump_json") else str(capability_result)
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            for key in ("plan", "summary", "implementation_steps", "steps", "approach", "recommendation", "memory"):
                value = parsed.get(key)
                if isinstance(value, str) and value:
                    return value[:600]
                if isinstance(value, list) and value:
                    return "; ".join(str(item) for item in value[:5])[:600]
            if parsed:
                return "; ".join(f"{k}={v}" for k, v in list(parsed.items())[:4])[:600]
        return text[:600]

    async def _negotiate_efficient(self, task: TaskRun, team: Team) -> dict[str, str]:
        """Produce one leader synthesis and one independent counterproposal.

        Completed subtask work is the evidence base. Asking only the leader and
        critic for full candidate answers removes repetitive proposals while
        preserving a real disagreement opportunity and member vote.
        """

        state = self._state(task.id)
        state["phase"] = "debating"
        state["metrics"]["debate_rounds"] += 1
        completed_work = [
            {
                "agent_id": item.get("agent_id"),
                "objective": item.get("subtask"),
                "result": item.get("outputs") or item.get("result"),
                "result_summary": item.get("result_summary"),
                "evidence_refs": item.get("evidence_refs", []),
                "expected_artifacts": item.get("expected_artifacts", []),
                "status": item.get("outcome_status") or item.get("status"),
            }
            for item in state.get("subtasks", [])
            if isinstance(item, dict) and item.get("status") in {"completed", "partial"}
        ]
        work_context = json.dumps(completed_work, sort_keys=True, default=str)[:16000]
        evidence_types = {
            "agentbay_browser_render_succeeded",
            "agentbay_artifact_exported",
            "local_independent_validation_reported",
            "composition_assignment_cleanup_completed",
        }
        runtime_evidence = [
            {"type": event.type, "payload": event.payload}
            for event in self._safe_list_events(task.id)
            if event.type in evidence_types
        ]
        runtime_evidence_context = json.dumps(runtime_evidence, sort_keys=True, default=str)[:16000]
        leader_id = team.leader_id or team.member_ids[0]
        critic_id = next(
            (
                agent_id
                for agent_id in team.member_ids
                if agent_id != leader_id and "risk analysis" in self.agents[agent_id].skills
            ),
            next((agent_id for agent_id in team.member_ids if agent_id != leader_id), leader_id),
        )
        proposal_order = [leader_id] + ([critic_id] if critic_id != leader_id else [])
        proposals: dict[str, str] = {}

        for index, agent_id in enumerate(proposal_order):
            agent = self.agents[agent_id]
            prior = proposals.get(leader_id, "")
            instruction = (
                "Synthesize the completed specialist work into one complete candidate answer. "
                "Follow the user's requested output schema exactly and do not invent evidence."
                if index == 0
                else
                "Act as an independent adversarial reviewer. Submit a corrected counterproposal, "
                "not commentary. Preserve correct parts, repair unsupported claims, and follow "
                "the requested output schema exactly."
            )
            try:
                record = await self._run_governance_tool(
                    task=task,
                    actor_identity=agent,
                    tool_func=propose_tool,
                    tool_name="propose",
                    schema_class=ProposalRecord,
                    prompt=(
                        f"Task: {task.prompt}\n"
                        f"Your exact agent_id is {agent_id}.\n"
                        f"Completed specialist work: {work_context}\n"
                        f"Authoritative runtime evidence: {runtime_evidence_context}\n"
                        "A durable host artifact reference is the exported copy of its workspace artifact, "
                        "not evidence of a workspace-path mismatch. Treat successful render, validation, "
                        "and cleanup events as authoritative.\n"
                        f"Leader proposal to review: {prior or 'none'}\n"
                        f"{instruction}\n"
                        "Call propose with your exact agent_id, complete proposal, and concise rationale."
                    ),
                )
            except GovernanceToolError:
                if index == 0:
                    raise
                self._emit(
                    task.id,
                    "agent_contribution_unavailable",
                    f"{agent.name}'s counterproposal was unavailable.",
                    actor=agent_id,
                    payload={"phase": "lean_counterproposal"},
                )
                continue

            proposal_id = record.proposal_id or f"prop-{uuid4().hex[:10]}"
            proposals[agent_id] = record.proposal
            state.setdefault("proposal_id_map", {})[agent_id] = proposal_id
            state["proposals"][agent_id] = {
                "proposal_id": proposal_id,
                "proposal": record.proposal,
                "rationale": record.rationale,
                "round": state["metrics"]["debate_rounds"],
            }
            self._register_artifact(
                task.id,
                "proposal",
                agent_id,
                "debating",
                state["proposals"][agent_id],
                confidence=0.85,
                status="draft",
            )
            self._emit(
                task.id,
                "agent_proposal_submitted",
                f"{agent.name} submitted {'the synthesis' if index == 0 else 'a counterproposal'}.",
                actor=agent_id,
                payload={
                    "proposal_id": proposal_id,
                    "agent_id": agent_id,
                    "proposal": record.proposal,
                    "rationale": record.rationale,
                    "round": state["metrics"]["debate_rounds"],
                    "status": "recorded",
                    "created_from_phase": "lean_debating",
                },
            )
            self._emit(task.id, "agent_negotiated", record.proposal, actor=agent_id)
            if index == 1:
                state["critique_source"] = "lean_counterproposal"
                state["critique"] = {
                    "reviewed": leader_id,
                    "critique": record.rationale,
                    "risks": [],
                    "improvements": ["Use the corrected counterproposal if it wins the vote."],
                    "confidence": 0.8,
                }
                self._emit(
                    task.id,
                    "peer_monitor_report",
                    f"{agent.name} reviewed the leader proposal through a counterproposal.",
                    actor=agent_id,
                    payload=state["critique"],
                )

        self._emit(
            task.id,
            "debate_round_completed",
            "The leader synthesis and bounded counterproposal round completed.",
            payload={
                "round": state["metrics"]["debate_rounds"],
                "proposal_count": len(proposals),
                "mode": "efficient_counterproposal",
            },
        )
        self._emit(task.id, "negotiation_closed", "The bounded proposal review closed.")
        return proposals

    async def _negotiate(self, task: TaskRun, team: Team) -> dict[str, str]:
        proposals: dict[str, str] = {}
        state = self._state(task.id)
        state["phase"] = "debating"
        state["metrics"]["debate_rounds"] += 1
        brief = state.get("team_coordination_brief")
        brief_context = ""
        if brief and isinstance(brief, dict) and brief.get("summary"):
            brief_context = (
                f"Team coordination brief: {brief['summary']}\n"
                f"Recommended focus: {brief.get('recommended_focus', 'none')}\n"
            )
        for agent_id in team.member_ids:
            agent = self.agents[agent_id]
            prior = "\n".join(f"- {self.agents[aid].name}: {p}" for aid, p in proposals.items())
            context = f"Prior proposals so far:\n{prior}" if prior else ""
            if brief_context:
                context = f"{brief_context}\n{context}" if context else brief_context
            rationale = f"{agent.name} contribution during negotiation."
            if self.settings.llm_enabled:
                if self.settings.native_debate_enabled:
                    record = await self._run_governance_tool(
                        task=task,
                        actor_identity=agent,
                        tool_func=propose_tool,
                        tool_name="propose",
                        schema_class=ProposalRecord,
                        prompt=(
                            f"Task: {task.prompt}\n"
                            f"Your exact agent_id is {agent_id}.\n"
                            f"{context}\n"
                            "Submit your proposal using the propose tool. "
                            "Use your exact agent_id in the tool arguments."
                        ),
                    )
                    contribution = record.proposal
                    proposals[agent_id] = contribution
                    rationale = record.rationale
                    if record.proposal_id:
                        state.setdefault("proposal_id_map", {})[agent_id] = record.proposal_id
                else:
                    capability = await self._use_capability_tool(task, agent, context)
                    context = f"{context}\n\nCapability tool result:\n{capability.model_dump_json()}".strip()
                    raw_capability = capability.model_dump_json()
                    contribution = self._readable_proposal_text(capability)
                    proposals[agent_id] = contribution
                    state.setdefault("raw_capability_outputs", {})[agent_id] = raw_capability
            else:
                bundle = self._role_tool_bundle(agent)
                tool_func, tool_name, _schema_class = bundle["primary"]
                support_tools = list(bundle.get("support_tools", []))
                state.setdefault("tool_bundles", {})[agent.id] = {
                    "role_key": bundle["role_key"],
                    "primary_tool": tool_name,
                    "support_tools": support_tools,
                    "behavior": bundle["behavior"],
                }
                self._emit(
                    task.id,
                    "agent_tool_bundle_selected",
                    f"{agent.name} is using the {bundle['role_key']} tool bundle.",
                    actor=agent.id,
                    payload=state["tool_bundles"][agent.id],
                )
                fallback = {
                    "agent": agent.id,
                    "skills": agent.skills,
                    "tool_bundle": state["tool_bundles"][agent.id],
                    "summary": f"{agent.name} used deterministic local capability planning.",
                }
                self._emit_tool_call(task.id, "capability_fallback", agent.id, "deterministic", fallback, "deterministic_no_key", True)
                contribution = await self._ask_agent(agent, task.prompt, context, task.id)
            proposals[agent_id] = contribution
            proposal_id = state.get("proposal_id_map", {}).get(agent_id) or f"prop-{uuid4().hex[:10]}"
            state["proposals"][agent_id] = {
                "proposal_id": proposal_id,
                "proposal": contribution,
                "rationale": rationale,
                "round": state["metrics"]["debate_rounds"],
            }
            self._register_artifact(
                task.id,
                "proposal",
                agent_id,
                "debating",
                state["proposals"][agent_id],
                confidence=1.0,
                status="draft",
            )
            self._emit(
                task.id,
                "agent_proposal_submitted",
                f"{agent.name} submitted a proposal.",
                actor=agent_id,
                payload={
                    "proposal_id": proposal_id,
                    "agent_id": agent_id,
                    "proposal": contribution,
                    "rationale": rationale,
                    "round": state["metrics"]["debate_rounds"],
                    "status": "recorded",
                    "created_from_phase": "debating",
                },
            )
            self._emit(task.id, "agent_negotiated", contribution, actor=agent_id)
        if not should_skip_phase(state, "revision"):
            revision_budget = get_loop_budget(state)
            for _revision_round in range(revision_budget):
                await self._run_debate_revision_round(task, team, proposals)
        self._emit(task.id, "debate_round_completed", "Agents recorded a first V3 debate round.", payload={
            "round": state["metrics"]["debate_rounds"],
            "proposal_count": len(state["proposals"]),
            "challenges_count": len(state["challenges"]),
            "revisions_count": len(state["revisions"]),
        })
        self._emit(task.id, "negotiation_closed", "Agents challenged and refined competing proposals.")
        return proposals

    async def _run_debate_revision_round(self, task: TaskRun, team: Team, proposals: dict[str, str]) -> None:
        """Run one challenge/revision pass before voting."""

        state = self._state(task.id)
        if not proposals:
            return
        critic_id = next((aid for aid in team.member_ids if "risk analysis" in self.agents[aid].skills), team.member_ids[-1])
        target_id = next((aid for aid in proposals if aid != critic_id), next(iter(proposals)))
        critic = self.agents[critic_id]
        target = self.agents[target_id]

        if self.settings.llm_enabled and self.settings.native_debate_enabled:
            challenge_record = await self._run_governance_tool(
                task=task,
                actor_identity=critic,
                tool_func=challenge_tool,
                tool_name="challenge",
                schema_class=ChallengeRecord,
                prompt=(
                    f"Task: {task.prompt}\n"
                    f"Your exact challenger_id is {critic_id}.\n"
                    f"Challenge the proposal from {target.name} (agent id: {target_id}).\n"
                    f"Proposal: {proposals[target_id]}\n"
                    "Call the challenge tool with the exact challenger_id and target_agent ids, "
                    "plus your objection and suggested revision."
                ),
            )
            challenge = {
                "challenger": challenge_record.challenger,
                "target": challenge_record.target,
                "objection": challenge_record.objection,
                "suggested_revision": challenge_record.suggested_revision,
                "round": challenge_record.round,
            }
        else:
            objection = f"{critic.name} challenges {target.name}'s highest-risk assumption before voting."
            suggested_revision = "State the acceptance check and narrow the first implementation step."
            challenge = {
                "challenger": critic_id,
                "target": target_id,
                "objection": objection,
                "suggested_revision": suggested_revision,
                "round": state["metrics"]["debate_rounds"],
            }

        if not (
            self.settings.llm_enabled
            and self.settings.native_debate_enabled
            and any(
                item.get("challenger") == challenge["challenger"]
                and item.get("target") == challenge["target"]
                and item.get("objection") == challenge["objection"]
                for item in state.get("challenges", [])
            )
        ):
            state["challenges"].append(challenge)
        self._emit(task.id, "proposal_challenged", f"{critic.name} challenged {target.name}'s proposal.", actor=critic_id, payload=challenge)

        if self.settings.llm_enabled and self.settings.native_debate_enabled:
            revision_record = await self._run_governance_tool(
                task=task,
                actor_identity=target,
                tool_func=revise_tool,
                tool_name="revise",
                schema_class=RevisionRecord,
                prompt=(
                    f"Task: {task.prompt}\n"
                    f"Your exact agent_id is {target_id}.\n"
                    f"Challenge from {critic.name}: {challenge['objection']}\n"
                    f"Suggested revision: {challenge['suggested_revision']}\n"
                    "Revise your proposal using the revise tool. "
                    "Use your exact agent_id in the tool arguments."
                ),
            )
            revised = revision_record.revised_proposal
            proposals[target_id] = revised
            revision = {
                "agent_id": target_id,
                "revised_proposal": revised,
                "changes": revision_record.changes,
                "round": revision_record.round,
            }
        else:
            revision_context = (
                f"Challenge from {critic.name}: {challenge['objection']}\n"
                f"Suggested revision: {challenge['suggested_revision']}\n"
                "Revise your proposal concisely before the vote."
            )
            if self.settings.llm_enabled:
                revised = f"{proposals[target_id]}\nRevision: {challenge['suggested_revision']}"
            else:
                revised = await self._ask_agent(target, task.prompt, revision_context, task.id)
            proposals[target_id] = revised
            revision = {
                "agent_id": target_id,
                "revised_proposal": revised,
                "changes": [challenge["suggested_revision"]],
                "round": state["metrics"]["debate_rounds"],
            }

        state["revisions"][target_id] = revision
        state["proposals"][target_id]["proposal"] = revised
        state["proposals"][target_id]["revised"] = True
        proposal_artifact = next(
            (
                item["id"]
                for item in reversed(state.get("artifacts", []))
                if item.get("type") == "proposal" and item.get("producer") == target_id
            ),
            None,
        )
        self._register_artifact(
            task.id,
            "revision",
            target_id,
            "debating",
            revision,
            depends_on=[proposal_artifact] if proposal_artifact else [],
            confidence=1.0,
            status="draft",
        )
        changes = revision.get("changes") or []
        change_summary = "; ".join(str(item) for item in changes[:2]) if isinstance(changes, list) else str(changes)
        self._record_shared_artifact_revision(
            task.id,
            proposal_artifact,
            "proposal.acceptance_check",
            target_id,
            critic_id,
            change_summary or challenge["suggested_revision"],
            challenge["objection"],
        )
        self._emit(task.id, "proposal_revised", f"{target.name} revised a proposal before voting.", actor=target_id, payload=revision)
        self._record_mind_change(
            task.id,
            MindChangeRecord(
                agent_id=target_id,
                previous_stance="defend_original_proposal",
                new_stance="revised_after_challenge",
                trigger_agent_id=critic_id,
                reason=(
                    f"{target.name} accepted pressure from {critic.name}: {challenge['objection']} "
                    f"Revision focus: {change_summary or challenge['suggested_revision']}"
                ),
            ),
        )

    async def _call_provider(
        self,
        call: Callable[[], Awaitable[Any]],
        *,
        task_id: str | None,
        actor: str,
        operation: str,
        timeout_seconds: float | None = None,
    ) -> Any:
        """Run an Agno model call through the provider-neutral retry policy."""

        def record_retry(payload: dict[str, Any]) -> None:
            if task_id is None:
                return
            self._emit(
                task_id,
                "provider_retry_scheduled",
                f"Transient provider failure; retrying {operation}.",
                actor=actor,
                payload={"operation": operation, **payload},
            )

        return await run_provider_call(
            call,
            timeout_seconds=timeout_seconds or self.settings.llm_timeout_seconds,
            max_attempts=self.settings.provider_max_attempts,
            backoff_base_seconds=self.settings.provider_backoff_base_seconds,
            backoff_cap_seconds=self.settings.provider_backoff_cap_seconds,
            on_retry=record_retry,
        )

    async def _ask_agent(self, identity: SocietyAgent, prompt: str, context: str = "", task_id: str | None = None) -> str:
        if not self.settings.llm_enabled:
            return fallback_contribution(identity, prompt)
        full_prompt = f"{prompt}\n\n{context}" if context else prompt

        def record_tool_intent(intent: dict[str, Any]) -> None:
            if task_id is None:
                return
            self._state(task_id).setdefault("tool_intents", []).append(intent)
            self._emit(
                task_id,
                "tool_intent_selected",
                "Researcher selected an evidence tool.",
                actor=identity.id,
                payload=intent,
            )

        def record_tool_result(result: dict[str, Any]) -> None:
            if task_id is None:
                return
            self._emit(
                task_id,
                "research_evidence_collected",
                "Researcher evaluated Context7 evidence.",
                actor=identity.id,
                payload=result,
            )

        async with role_tool_context(
            identity,
            self.settings,
            on_tool_intent=record_tool_intent,
            on_tool_result=record_tool_result,
        ) as role_tools:
            agno_agent = build_agno_agent(
                identity,
                self.settings,
                tools=role_tools or None,
                extra_instructions=role_tool_instructions(identity, self.settings),
                session_id=task_id,
                session_state=self._state(task_id) if task_id is not None else None,
            )
            response = await self._call_provider(
                lambda: agno_agent.arun(full_prompt),
                task_id=task_id,
                actor=identity.id,
                operation="agent_work",
            )
            if task_id is not None:
                self._capture_model_usage(task_id, response, "agent_work", identity.id)
                if self.settings.benchmark_suite_tools_enabled:
                    benchmark_tool_names = {"lookup_record", "lookup_dataset", "calculate"}
                    if self.settings.benchmark_suite_version == "v3":
                        from benchmarks.tools_v3 import TOOL_NAMES as V3_TOOL_NAMES
                        benchmark_tool_names = set(V3_TOOL_NAMES)
                    for call in getattr(response, "tools", None) or []:
                        name = getattr(call, "tool_name", None) or getattr(call, "name", None)
                        arguments = getattr(call, "tool_args", None) or getattr(call, "arguments", None) or {}
                        if isinstance(call, dict):
                            name = call.get("tool_name") or call.get("name") or name
                            arguments = call.get("tool_args") or call.get("arguments") or arguments
                        if name in benchmark_tool_names:
                            self._emit(
                                task_id,
                                "benchmark_tool_used",
                                f"{identity.name} used {name}.",
                                actor=identity.id,
                                payload={
                                    "name": name,
                                    "arguments": arguments if isinstance(arguments, dict) else {},
                                },
                            )
        return str(getattr(response, "content", response))

    async def _collect_proposal_opinions(self, task: TaskRun, team: Team, proposals: dict[str, str]) -> None:
        """Collect public opinions about each proposal before voting.

        Emits proposal_opinion_recorded events with proposal_id, agent_id,
        stance, opinion, confidence, and phase. Model calls are isolated and
        bounded in parallel; their events are recorded in stable roster order.
        """

        state = self._state(task.id)
        proposal_id_map = state.get("proposal_id_map", {})
        if self.settings.llm_enabled and self.settings.native_debate_enabled:
            limit = max(1, int(self.settings.readiness_concurrency))
            semaphore = asyncio.Semaphore(limit)
            if self.settings.efficient_society_enabled:
                # One independent cross-review per member preserves dissent and
                # proposal coverage without the previous O(members*proposals)
                # all-pairs call matrix.
                jobs = []
                proposal_ids = list(proposals)
                for index, agent_id in enumerate(team.member_ids):
                    candidates = [proposer_id for proposer_id in proposal_ids if proposer_id != agent_id]
                    if not candidates:
                        continue
                    proposer_id = candidates[index % len(candidates)]
                    jobs.append((
                        agent_id,
                        proposer_id,
                        proposals[proposer_id],
                        proposal_id_map.get(proposer_id) or f"prop-{proposer_id}",
                    ))
            else:
                jobs = [
                    (agent_id, proposer_id, proposal_text, proposal_id_map.get(proposer_id) or f"prop-{proposer_id}")
                    for agent_id in team.member_ids
                    for proposer_id, proposal_text in proposals.items()
                    if proposer_id != agent_id
                ]

            async def _collect_one(
                agent_id: str,
                proposer_id: str,
                proposal_text: str,
                proposal_id: str,
            ) -> tuple[str, str, str, ProposalOpinionRecord]:
                agent = self.agents[agent_id]
                prompt = (
                    f"Task: {task.prompt}\n"
                    f"Your exact agent_id is {agent_id}.\n"
                    f"Proposal from {proposer_id} (id: {proposal_id}):\n{proposal_text}\n"
                    "State your honest opinion of this proposal. "
                    "Use stance: support, oppose, uncertain, or neutral. "
                    "If you have no meaningful view, use neutral. "
                    "Call record_proposal_opinion with your exact agent_id and the proposal_id."
                )
                async with semaphore:
                    opinion = await self._run_governance_tool_isolated(
                        task=task,
                        actor_identity=agent,
                        tool_func=record_proposal_opinion_tool,
                        tool_name="record_proposal_opinion",
                        schema_class=ProposalOpinionRecord,
                        prompt=prompt,
                        derived_session_id=f"{task.id}:opinion:{agent_id}:{proposal_id}:{uuid4().hex[:8]}",
                        state_snapshot=copy.deepcopy(state),
                    )
                return agent_id, proposer_id, proposal_id, opinion

            results = await asyncio.gather(*[
                _collect_one(agent_id, proposer_id, proposal_text, proposal_id)
                for agent_id, proposer_id, proposal_text, proposal_id in jobs
            ])
            for agent_id, proposer_id, _proposal_id, opinion in results:
                opinion_payload = opinion.model_dump()
                state.setdefault("proposal_opinions", []).append(opinion_payload)
                self._emit_tool_call(task.id, "record_proposal_opinion", agent_id, f"proposal={_proposal_id}", opinion_payload, "native_agno", True)
                self._emit(
                    task.id,
                    "proposal_opinion_recorded",
                    f"{self.agents[agent_id].name} recorded an opinion on {proposer_id}'s proposal.",
                    actor=agent_id,
                    payload=opinion_payload,
                )
            return

        for agent_id in team.member_ids:
            agent = self.agents[agent_id]
            for proposer_id, proposal_text in proposals.items():
                if proposer_id == agent_id:
                    continue
                proposal_id = proposal_id_map.get(proposer_id) or f"prop-{proposer_id}"
                stance = self._deterministic_opinion_stance(agent, proposer_id)
                opinion_text = self._deterministic_opinion_text(agent, proposer_id, proposal_text)
                opinion_payload = {
                    "agent_id": agent_id,
                    "proposal_id": proposal_id,
                    "opinion": opinion_text,
                    "stance": stance,
                    "confidence": 0.5,
                    "phase": "proposal_review",
                }
                state.setdefault("proposal_opinions", []).append(opinion_payload)
                self._emit_tool_call(
                    task.id,
                    "record_proposal_opinion",
                    agent_id,
                    "deterministic",
                    opinion_payload,
                    "deterministic_no_key",
                    True,
                )
                self._emit(
                    task.id,
                    "proposal_opinion_recorded",
                    f"{agent.name} recorded an opinion on {proposer_id}'s proposal.",
                    actor=agent_id,
                    payload=opinion_payload,
                )

    def _deterministic_opinion_stance(self, agent: SocietyAgent, proposer_id: str) -> str:
        """Return a deterministic opinion stance based on agent profile."""

        if agent.profile.risk_tolerance == "low":
            return "uncertain"
        if agent.profile.risk_tolerance == "high":
            return "support"
        return "neutral"

    def _deterministic_opinion_text(self, agent: SocietyAgent, proposer_id: str, proposal_text: str) -> str:
        """Return a deterministic opinion text based on agent role."""

        role = agent.role.lower()
        snippet = str(proposal_text)[:120]
        if "research" in role:
            return f"I need more evidence before I can support this. Current proposal: {snippet}"
        if "review" in role or "critic" in role:
            return f"I see potential risks in this approach that need validation: {snippet}"
        if "architect" in role:
            return f"This approach needs clearer boundaries before I can endorse it: {snippet}"
        return f"I can work with this direction with caveats: {snippet}"

    async def _vote(self, task: TaskRun, team: Team, proposals: dict[str, str]) -> str | None:
        votes = []
        candidates = list(proposals.keys())

        if self.settings.llm_enabled:
            vote_results = await self._collect_votes_concurrent(task, team, candidates)
            for voter_id, decision in vote_results:
                choice = decision.choice
                if choice not in candidates:
                    raise GovernanceToolError(f"Vote choice '{choice}' is not a valid candidate: {candidates}")
                votes.append(choice)
                voter = self.agents[voter_id]
                self._emit(task.id, "vote_cast", f"{voter.name} voted for {self.agents[choice].name}.", actor=voter_id, payload={"choice": choice, "reason": decision.reason, "confidence": decision.confidence})
                self._record_position(
                    task.id,
                    AgentPosition(
                        agent_id=voter_id,
                        phase="vote",
                        stance="support",
                        target=choice,
                        reason=decision.reason,
                        confidence=decision.confidence,
                    ),
                )
                self._record_collaboration_action(
                    task.id,
                    CollaborationAction(
                        agent_id=voter_id,
                        action="coalition_joined",
                        target_agent_id=choice,
                        phase="vote",
                        reason=decision.reason,
                        confidence=decision.confidence,
                    ),
                )
                if self.settings.native_voting_enabled:
                    ballots = self._state(task.id).setdefault("ballots", [])
                    if not any(ballot.get("voter") == voter_id for ballot in ballots):
                        ballots.append({"voter": voter_id, "choice": choice, "reason": decision.reason, "confidence": decision.confidence})
                    self._emit_tool_call(task.id, "cast_ballot", voter_id, f"candidates={','.join(candidates)}", decision.model_dump(), "native_agno", True)
                else:
                    ballots = self._state(task.id).setdefault("ballots", [])
                    if not any(ballot.get("voter") == voter_id for ballot in ballots):
                        ballots.append({"voter": voter_id, "choice": choice, "reason": decision.reason, "confidence": decision.confidence})
                    self._emit_tool_call(task.id, "cast_vote", voter_id, f"candidates={','.join(candidates)}", decision.model_dump(), "native_agno", True)
        else:
            for voter_id in self._voter_ids(team):
                proposal_texts = "".join(proposals[c] for c in candidates)
                seed = hash((task.prompt, voter_id, proposal_texts))
                choice = candidates[seed % len(candidates)]
                votes.append(choice)
                self._state(task.id)["ballots"].append({"voter": voter_id, "choice": choice, "reason": "hash fallback", "confidence": 1.0})
                self._emit(task.id, "vote_cast", f"{self.agents[voter_id].name} voted for {self.agents[choice].name}.", actor=voter_id, payload={"choice": choice, "reason": "hash fallback"})
                self._emit_tool_call(task.id, "cast_vote", voter_id, "deterministic", {"choice": choice, "reason": "hash fallback", "confidence": 1.0}, "deterministic_no_key", True)
                self._record_position(
                    task.id,
                    AgentPosition(
                        agent_id=voter_id,
                        phase="vote",
                        stance="support",
                        target=choice,
                        reason="hash fallback",
                        confidence=1.0,
                    ),
                )
                self._record_collaboration_action(
                    task.id,
                    CollaborationAction(
                        agent_id=voter_id,
                        action="coalition_joined",
                        target_agent_id=choice,
                        phase="vote",
                        reason="hash fallback vote support",
                        confidence=1.0,
                    ),
                )

        state = self._state(task.id)
        if self.settings.llm_enabled and self.settings.native_voting_enabled:
            tally_result = None
            if not self.settings.efficient_society_enabled:
                tally_result = await self._run_governance_tool(
                    task=task,
                    actor_identity=self.agents[team.leader_id or team.member_ids[0]],
                    tool_func=tally_ballots_tool,
                    tool_name="tally_ballots",
                    schema_class=TallyResult,
                    prompt="Tally all ballot and determine the winner. Call the tally_ballots tool.",
                )
            # The governance tool's state mutation is its authoritative output,
            # including an intentionally empty tally or a tie. Reconstruct the
            # tally from persisted ballots only when the tool returned a schema
            # result without applying its normal state side effect.
            tool_applied_tally = state.get("phase") == "voted" and isinstance(state.get("tally"), dict)
            persisted_ballots = [
                ballot for ballot in state.get("ballots", [])
                if isinstance(ballot, dict) and ballot.get("choice") in candidates
            ]
            if tool_applied_tally:
                raw_tally = state.get("tally", {})
            elif persisted_ballots:
                raw_tally = dict(Counter(str(ballot["choice"]) for ballot in persisted_ballots))
                if tally_result and tally_result.tally != raw_tally:
                    self._emit(
                        task.id,
                        "tally_reconciled_from_ballots",
                        "The model tally differed from persisted ballots; persisted ballots are authoritative.",
                        payload={"model_tally": tally_result.tally, "ballot_tally": raw_tally},
                    )
            else:
                raw_tally = state.get("tally")
                if not isinstance(raw_tally, dict) or not raw_tally:
                    raw_tally = tally_result.tally if tally_result else {}
            tally: dict[str, int] = {}
            if isinstance(raw_tally, dict):
                for _key, _value in raw_tally.items():
                    if _key in candidates and isinstance(_value, (int, float)) and not isinstance(_value, bool):
                        tally[_key] = int(_value)
            state["tally"] = tally
            if not tally:
                state["phase"] = "voted"
                state["winner_id"] = None
                self._emit(task.id, "ballots_tallied", "Votes were tallied but no valid winner emerged.", payload={"tally": tally, "winner": None})
                self._pause_for_vote_resolution(task, team, proposals, tally, candidates, reason="empty_tally")
                return None
            top_count = max(tally.values())
            top_candidates = [c for c, v in tally.items() if v == top_count]
            if len(top_candidates) > 1:
                winner = self._benchmark_equivalent_tie_winner(team, proposals, top_candidates)
                if winner is None:
                    state["phase"] = "voted"
                    state["winner_id"] = None
                    self._emit(task.id, "ballots_tallied", "Votes were tallied but the result is a tie.", payload={"tally": tally, "winner": None, "tied_candidates": top_candidates})
                    self._pause_for_vote_resolution(task, team, proposals, tally, candidates, reason="tie", tied_candidates=top_candidates)
                    return None
                self._emit(task.id, "equivalent_tie_resolved", "Equivalent benchmark proposals were resolved without changing the selected answer.", payload={"tally": tally, "winner": winner, "tied_candidates": top_candidates})
            else:
                winner = top_candidates[0]
            state["winner_id"] = winner
            state["phase"] = "voted"
        else:
            tally = dict(Counter(votes).most_common())
            state["phase"] = "voted"
            state["tally"] = tally
            if not tally:
                state["winner_id"] = None
                self._emit(task.id, "ballots_tallied", "Votes were tallied but no valid winner emerged.", payload={"tally": tally, "winner": None})
                self._pause_for_vote_resolution(task, team, proposals, tally, candidates, reason="empty_tally")
                return None
            top_count = max(tally.values())
            top_candidates = [c for c, v in tally.items() if v == top_count]
            if len(top_candidates) > 1:
                winner = self._benchmark_equivalent_tie_winner(team, proposals, top_candidates)
                if winner is None:
                    state["winner_id"] = None
                    self._emit(task.id, "ballots_tallied", "Votes were tallied but the result is a tie.", payload={"tally": tally, "winner": None, "tied_candidates": top_candidates})
                    self._pause_for_vote_resolution(task, team, proposals, tally, candidates, reason="tie", tied_candidates=top_candidates)
                    return None
                self._emit(task.id, "equivalent_tie_resolved", "Equivalent benchmark proposals were resolved without changing the selected answer.", payload={"tally": tally, "winner": winner, "tied_candidates": top_candidates})
            else:
                winner = top_candidates[0]
            state["winner_id"] = winner
        self._emit(task.id, "ballots_tallied", "Votes were tallied in shared session state.", payload={"tally": tally, "winner": winner})
        self._emit(task.id, "solution_selected", f"{self.agents[winner].name}'s proposal won the vote.", actor=winner)
        winner_proposal_id = None
        winner_entry = state.get("proposals", {}).get(winner)
        if isinstance(winner_entry, dict):
            winner_proposal_id = winner_entry.get("proposal_id")
        supporting_votes = [ballot.get("voter") for ballot in state.get("ballots", []) if ballot.get("choice") == winner]
        blocking_objections = [
            obj.get("objection", "")
            for obj in state.get("public_room", {}).get("objections", [])
            if isinstance(obj, dict) and obj.get("blocks_execution")
        ]
        brief = state.get("working_brief") if isinstance(state.get("working_brief"), dict) else {}
        dissent_carried = list(brief.get("unresolved_dissent", []))[:5]
        winner_rationale_payload = {
            "winner_agent_id": winner,
            "winning_proposal_id": winner_proposal_id,
            "why_won": f"Won majority vote with {len(supporting_votes)} supporting ballot(s).",
            "supporting_votes": supporting_votes,
            "critical_tradeoffs": list(brief.get("open_questions", []))[:3],
            "dissent_carried": dissent_carried,
            "blocking_objections_cleared": len(blocking_objections) == 0,
            "tally": tally,
        }
        self._emit(
            task.id,
            "winner_selected",
            f"{self.agents[winner].name}'s proposal was selected as the winning approach.",
            actor=winner,
            payload=winner_rationale_payload,
        )
        await self._emit_leader_synthesis(task, team, winner, winner_proposal_id, winner_rationale_payload)
        return winner

    async def _emit_leader_synthesis(
        self,
        task: TaskRun,
        team: Team,
        winner: str,
        winner_proposal_id: str | None,
        winner_rationale_payload: dict[str, Any],
    ) -> None:
        """Emit an explicit leader-synthesis truth artifact after winner selection.

        This is separate from plain ballot tallying. It expresses: winning
        proposal or winner, why it won, critical tradeoffs, carried
        dissent/caveats, and that this is the leader synthesis.
        """

        state = self._state(task.id)
        leader_id = team.leader_id or team.member_ids[0]
        leader = self.agents[leader_id]
        winner_entry = state.get("proposals", {}).get(winner)
        if isinstance(winner_entry, dict):
            winner_proposal_text = str(winner_entry.get("proposal", ""))
        else:
            winner_proposal_text = str(winner_entry or "")
        opinions = [o for o in state.get("proposal_opinions", []) if isinstance(o, dict)]
        carried_dissent: list[str] = []
        caveats: list[str] = []
        for opinion in opinions:
            stance = str(opinion.get("stance", ""))
            if stance in {"oppose", "uncertain"}:
                opinion_text = str(opinion.get("opinion", ""))
                if opinion_text:
                    carried_dissent.append(f"{opinion.get('agent_id')}: {opinion_text}")
        brief = state.get("working_brief") if isinstance(state.get("working_brief"), dict) else {}
        for item in list(brief.get("unresolved_dissent", []))[:3]:
            text = str(item)
            if text and text not in carried_dissent:
                carried_dissent.append(text)
        for item in list(brief.get("open_questions", []))[:3]:
            text = str(item)
            if text and text not in caveats:
                caveats.append(text)
        supporting_votes = winner_rationale_payload.get("supporting_votes", [])
        why_won = winner_rationale_payload.get("why_won", "")
        critical_tradeoffs = winner_rationale_payload.get("critical_tradeoffs", [])
        if self.settings.llm_enabled:
            try:
                synthesis = await self._run_governance_tool(
                    task=task,
                    actor_identity=leader,
                    tool_func=leader_synthesize_tool,
                    tool_name="leader_synthesize",
                    schema_class=LeaderSynthesisRecord,
                    prompt=(
                        f"Task: {task.prompt}\n"
                        f"Winner: {winner} ({self.agents[winner].name})\n"
                        f"Winning proposal id: {winner_proposal_id or 'unknown'}\n"
                        f"Winning proposal: {winner_proposal_text[:400]}\n"
                        f"Why it won: {why_won}\n"
                        f"Supporting voters: {', '.join(supporting_votes)}\n"
                        f"Critical tradeoffs: {', '.join(critical_tradeoffs) or 'none'}\n"
                        f"Carried dissent: {'; '.join(carried_dissent[:3]) or 'none'}\n"
                        "Produce a leader synthesis: a human-readable summary of the winning "
                        "proposal, why it won, the key tradeoffs, and any carried dissent or caveats. "
                        "Call leader_synthesize with the structured result."
                    ),
                )
                synthesis_payload = synthesis.model_dump()
            except GovernanceToolError:
                synthesis_payload = self._deterministic_leader_synthesis(
                    leader_id, winner, winner_proposal_id, winner_proposal_text,
                    why_won, critical_tradeoffs, carried_dissent, caveats,
                )
                state["leader_synthesis"] = synthesis_payload
        else:
            synthesis_payload = self._deterministic_leader_synthesis(
                leader_id, winner, winner_proposal_id, winner_proposal_text,
                why_won, critical_tradeoffs, carried_dissent, caveats,
            )
            state["leader_synthesis"] = synthesis_payload
            self._emit_tool_call(
                task.id,
                "leader_synthesize",
                leader_id,
                "deterministic",
                synthesis_payload,
                "deterministic_no_key",
                True,
            )
        synthesis_payload["synthesis_kind"] = "leader_synthesis"
        # The event log is the durable source, while session state lets later
        # stages validate the same real decision without reconstructing it.
        state["leader_synthesis"] = synthesis_payload
        self._emit(
            task.id,
            "leader_synthesis",
            f"{leader.name} synthesized the decision as leader.",
            actor=leader_id,
            payload=synthesis_payload,
        )

    def _deterministic_leader_synthesis(
        self,
        leader_id: str,
        winner: str,
        winner_proposal_id: str | None,
        winner_proposal_text: str,
        why_won: str,
        critical_tradeoffs: list[str],
        carried_dissent: list[str],
        caveats: list[str],
    ) -> dict[str, Any]:
        """Build a deterministic leader synthesis payload for no-key runs."""

        return {
            "winner_agent_id": winner,
            "winning_proposal_id": winner_proposal_id,
            "winning_proposal_summary": winner_proposal_text[:400],
            "why_won": why_won or "Won majority vote.",
            "critical_tradeoffs": list(critical_tradeoffs)[:5],
            "carried_dissent": list(carried_dissent)[:5],
            "caveats": list(caveats)[:5],
            "confidence": 0.7,
            "synthesis_kind": "leader_synthesis",
        }

    def _ensure_independent_validator(self, task: TaskRun, team: Team) -> SocietyAgent | None:
        """Return one scoped Test/Validation Engineer that cannot vote."""

        member_ids = list(getattr(team, "member_ids", []) or [])
        agents = getattr(self, "agents", {})
        if not member_ids or not agents:
            # Legacy replay summaries and narrow unit harnesses may not carry
            # an executable roster. Real task teams always do.
            return None
        for agent_id in member_ids:
            if agent_id not in agents:
                continue
            agent = self.agents[agent_id]
            if resolve_role_key(agent) == "test_validation_engineer":
                return agent
        leader_id = team.leader_id or self._voter_ids(team)[0]
        leader = self.agents[leader_id]
        state = self._state(task.id)
        prior_spawn_decision = copy.deepcopy(state.get("spawn_decision"))
        self._do_spawn(
            task,
            team,
            leader,
            "Independently verify acceptance evidence after the decision is complete.",
            "Test Validation Engineer",
        )
        state["validation_spawn"] = copy.deepcopy(state.get("spawn_decision"))
        if prior_spawn_decision is None:
            state.pop("spawn_decision", None)
        else:
            state["spawn_decision"] = prior_spawn_decision
        validator = next(
            self.agents[agent_id]
            for agent_id in reversed(team.member_ids)
            if resolve_role_key(self.agents[agent_id]) == "test_validation_engineer"
        )
        # Dynamic specialists are evidence reporters, never governance voters.
        team.voter_ids = [agent_id for agent_id in team.voter_ids if agent_id != validator.id]
        return validator

    async def _run_independent_validator(self, task: TaskRun, team: Team) -> None:
        """Execute a non-voting specialist review against recorded proof."""

        state = self._state(task.id)
        evidence = state.get("acceptance_evidence")
        if not isinstance(evidence, dict) or not evidence:
            # There is nothing factual to validate in legacy replay records or
            # narrow harnesses that replace acceptance evaluation.
            return
        composition_validation = self._fixed_composition_validation_evidence(task.id)
        if composition_validation is not None:
            # The fixed Test Engineer is already independent from Builder and
            # its local report is tied to the composed artifacts. Do not spawn
            # a second validator that can contradict this authoritative run
            # evidence. Missing cleanup or a failed report remain explicit
            # required acceptance failures populated below.
            state["independent_validation"] = {
                "passed": composition_validation["validation_passed"],
                "validator_id": composition_validation["validator_id"],
                "can_vote": False,
                "source": "fixed_test_engineer_local_validation",
                "cleanup_complete": composition_validation["cleanup_complete"],
                "missing_cleanup_assignment_ids": composition_validation["missing_cleanup_assignment_ids"],
            }
            self._emit(
                task.id,
                "independent_validation_completed",
                "Fixed Test Engineer validation accepted." if composition_validation["validation_passed"] else "Fixed Test Engineer validation found proof gaps.",
                actor=composition_validation["validator_id"],
                payload=state["independent_validation"],
            )
            return
        validator = self._ensure_independent_validator(task, team)
        if validator is None:
            return
        if self.settings.llm_enabled:
            prompt = (
                f"Task: {task.prompt}\n\nFinal deliverable:\n{task.final_answer or ''}\n\n"
                f"Acceptance evidence:\n{json.dumps(evidence, sort_keys=True, default=str)[:12000]}\n\n"
                "Act independently from the voting agents. Check only the supplied evidence. "
                "Call report_independent_validation exactly once. Mark passed=false when a claim "
                "lacks proof or contradicts the evidence."
            )
            try:
                report = await self._run_governance_tool(
                    task=task,
                    actor_identity=validator,
                    tool_func=report_independent_validation_tool,
                    tool_name="report_independent_validation",
                    schema_class=IndependentValidationReport,
                    prompt=prompt,
                )
            except GovernanceToolError as exc:
                report = IndependentValidationReport(
                    passed=False,
                    missing_evidence=["independent_validator_execution"],
                    recommendation=f"Retry the independent validation step: {str(exc)[:160]}",
                )
        else:
            failed = state.get("failed_checks", [])
            report = IndependentValidationReport(
                passed=not failed,
                checked_evidence_ids=[str(item.get("check", "")) for item in state.get("acceptance_checks", [])],
                missing_evidence=[str(item.get("check", "")) for item in failed],
                recommendation="Resolve recorded acceptance gaps." if failed else "Recorded checks passed.",
            )

        payload = report.model_dump()
        payload.update({"validator_id": validator.id, "can_vote": False})
        state["independent_validation"] = payload
        self._register_artifact(
            task.id, "independent_validation", validator.id, "validation",
            payload, confidence=0.9 if report.passed else 0.6,
            status="final" if report.passed else "failed",
        )
        self._emit(
            task.id,
            "independent_validation_completed",
            "Independent validation passed." if report.passed else "Independent validation found proof gaps.",
            actor=validator.id,
            payload=payload,
        )
        if not report.passed:
            failed_checks = state.setdefault("failed_checks", [])
            if not any(item.get("check") == "independent_validator" for item in failed_checks if isinstance(item, dict)):
                failed_checks.append({
                    "check": "independent_validator",
                    "reason": "; ".join(report.missing_evidence + report.contradictions)
                    or report.recommendation or "independent validation failed",
                })
            if isinstance(evidence, dict):
                evidence["terminal_status"] = "failed"
                required_failures = evidence.setdefault("required_failures", [])
                if "independent_validator" not in required_failures:
                    required_failures.append("independent_validator")
                optional_failures = evidence.setdefault("optional_failures", [])
            while "independent_validator" in optional_failures:
                optional_failures.remove("independent_validator")

    def _fixed_composition_validation_evidence(self, task_id: str) -> dict[str, Any] | None:
        """Return fixed Test Engineer validation and cleanup evidence, if selected.

        This bridge intentionally trusts only event-ledger records from a
        selected immutable Test Engineer. It never upgrades a failed report or
        infers sandbox cleanup from a completed work node.
        """

        state = self._state(task_id)
        selection = state.get("fixed_specialist_selection")
        assignments = selection.get("assignments") if isinstance(selection, dict) else None
        if not isinstance(assignments, list):
            return None
        expected_assignment_ids = {
            str(item.get("assignment_id"))
            for item in assignments
            if isinstance(item, dict) and item.get("assignment_id")
        }
        validator_assignment_ids = {
            str(item.get("assignment_id"))
            for item in assignments
            if isinstance(item, dict) and item.get("template_id") == "test_engineer" and item.get("assignment_id")
        }
        if not validator_assignment_ids:
            return None

        validation_events = [
            event for event in self._safe_list_events(task_id)
            if event.type == "local_independent_validation_reported"
            and isinstance(event.payload, dict)
            and str(event.payload.get("assignment_id")) in validator_assignment_ids
        ]
        latest_validation = validation_events[-1] if validation_events else None
        cleanup_events = [
            event for event in self._safe_list_events(task_id)
            if event.type == "composition_assignment_cleanup_completed"
            and isinstance(event.payload, dict)
            and str(event.payload.get("assignment_id")) in expected_assignment_ids
        ]
        successful_cleanup_assignment_ids = {
            str(event.payload["assignment_id"])
            for event in cleanup_events
            if event.payload.get("success") is True
            and isinstance(event.payload.get("results"), list)
            and bool(event.payload["results"])
            and all(
                isinstance(result, dict) and result.get("success") is True and result.get("closed") is True
                for result in event.payload["results"]
            )
        }
        missing_cleanup = sorted(expected_assignment_ids - successful_cleanup_assignment_ids)
        return {
            "validation_passed": bool(latest_validation and latest_validation.payload.get("passed") is True),
            "validation_event_ids": [event.id for event in validation_events],
            "cleanup_event_ids": [event.id for event in cleanup_events],
            "cleanup_complete": not missing_cleanup,
            "missing_cleanup_assignment_ids": missing_cleanup,
            "validator_id": latest_validation.actor if latest_validation and latest_validation.actor else "test_engineer",
        }

    async def _monitor(self, task: TaskRun, team: Team, winner: str, proposals: dict[str, str]) -> None:
        critic_id = next((aid for aid in team.member_ids if "risk analysis" in self.agents[aid].skills), team.member_ids[-1])
        critic = self.agents[critic_id]

        if not self.settings.llm_enabled:
            critique = {"reviewed": winner, "critique": "", "risks": [], "improvements": [], "unavailable": True}
            self._state(task.id)["critique"] = critique
            self._emit(
                task.id,
                "agent_contribution_unavailable",
                f"{critic.name}'s peer review was unavailable because model access is disabled.",
                actor=critic_id,
                payload={"phase": "monitor", "reason": "llm_disabled", "reviewed": winner},
            )
            return

        prompt = (
            f"Task: {task.prompt}\n"
            f"Winning candidate: {winner} ({self.agents[winner].name}, {self.agents[winner].role})\n"
            f"Provide a concise quality review: identify assumptions and improvements.\n"
            f"Call the peer_review tool with your critique."
        )

        report = await self._run_governance_tool(
            task=task,
            actor_identity=critic,
            tool_func=peer_review_tool,
            tool_name="peer_review",
            schema_class=CritiqueReport,
            prompt=prompt,
        )

        critique_payload = {
            "reviewed": winner,
            "critique": report.critique,
            "risks": report.risks,
            "improvements": report.improvements,
            "confidence": report.confidence,
        }
        self._state(task.id)["critique"] = critique_payload
        self._register_artifact(task.id, "critique", critic_id, "monitor", critique_payload, confidence=report.confidence, status="final")
        self._emit(task.id, "peer_monitor_report", f"{critic.name} reviewed the winning solution.", actor=critic_id, payload=critique_payload)

    def _record_eval_metric_direct(self, task_id: str, metric_name: str, value: float, context: str = "") -> None:
        """Append an evaluation metric without going through a tool call."""

        from datetime import datetime, timezone
        state = self._state(task_id)
        state.setdefault("evaluation_metrics", []).append({
            "metric_name": metric_name,
            "value": value,
            "context": context,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

    async def _record_evaluation_metrics(self, task: TaskRun, team: Team, winner: str) -> None:
        """Record evaluation metrics after monitoring, behind evaluation_metrics_enabled."""

        if not self.settings.evaluation_metrics_enabled:
            return
        state = self._state(task.id)
        critique = state.get("critique") or {}
        confidence = float(critique.get("confidence", 0.0)) if isinstance(critique, dict) else 0.0
        revision_count = len(state.get("revisions", {}))
        blocker_count = sum(1 for s in state.get("subtasks", []) if s.get("blockers"))
        proposals_count = len(state.get("proposals", {}))
        completeness = min(1.0, proposals_count / max(len(team.member_ids), 1))

        metrics_to_record = [
            ("confidence", confidence, "critique confidence"),
            ("completeness", completeness, "proposal coverage"),
            ("revision_count", float(revision_count), "revisions after debate"),
            ("blocker_severity", float(blocker_count), "blocked subtasks"),
            ("validation_pass", 1.0 if not state.get("failed_checks") else 0.0, "acceptance checks all passed"),
        ]

        if self.settings.llm_enabled and not self.settings.efficient_society_enabled:
            actor = self.agents[team.leader_id or team.member_ids[0]]
            for metric_name, value, context in metrics_to_record:
                try:
                    await self._run_governance_tool(
                        task=task,
                        actor_identity=actor,
                        tool_func=record_metric_tool,
                        tool_name="record_metric",
                        schema_class=MetricRecord,
                        prompt=(
                            f"Record the evaluation metric '{metric_name}' with value {value}. "
                            f"Context: {context}. Call the record_metric tool."
                        ),
                    )
                except GovernanceToolError:
                    self._record_eval_metric_direct(task.id, metric_name, value, context)
        else:
            for metric_name, value, context in metrics_to_record:
                self._record_eval_metric_direct(task.id, metric_name, value, context)

    def _social_trace_counts(self, state: dict[str, Any]) -> dict[str, int]:
        """Return compact social trace counts for durable lessons."""

        public_room = state.get("public_room", {})
        return {
            "positions": len(public_room.get("positions", [])),
            "objections": len(public_room.get("objections", [])),
            "endorsements": len(public_room.get("endorsements", [])),
            "mind_changes": len(public_room.get("mind_changes", [])),
            "private_notes": sum(
                len(agent_state.get("private_notes", []))
                for agent_state in state.get("private_agent_state", {}).values()
                if isinstance(agent_state, dict)
            ),
            "published_private_notes": len(public_room.get("published_private_notes", [])),
            "collaboration_actions": len(public_room.get("collaboration_actions", [])),
            "trust_updates": len(state.get("trust_updates", [])),
        }

    def _social_lessons_for_agent(self, state: dict[str, Any], agent_id: str, winner: str) -> list[dict[str, Any]]:
        """Extract reusable social lessons relevant to one agent."""

        public_room = state.get("public_room", {})
        lessons: list[dict[str, Any]] = []
        for objection in public_room.get("objections", []):
            if not isinstance(objection, dict):
                continue
            if objection.get("agent_id") == agent_id:
                lessons.append({
                    "signal": "raised_blocker",
                    "lesson": f"Raised a {objection.get('severity', 'medium')} objection: {objection.get('objection', '')}",
                    "next_time": f"Require this resolution before endorsing work: {objection.get('resolution_condition', '')}",
                })
        for change in public_room.get("mind_changes", []):
            if not isinstance(change, dict):
                continue
            if change.get("agent_id") == agent_id:
                lessons.append({
                    "signal": "changed_mind",
                    "lesson": f"Changed stance from {change.get('previous_stance')} to {change.get('new_stance')}.",
                    "next_time": f"Stay open to challenges from {change.get('trigger_agent_id') or 'teammates'} when the reason is concrete.",
                })
            if change.get("trigger_agent_id") == agent_id:
                lessons.append({
                    "signal": "persuaded_teammate",
                    "lesson": f"Prompted {change.get('agent_id')} to revise their stance.",
                    "next_time": "Challenge weak assumptions with a specific revision path.",
                })
        for action in public_room.get("collaboration_actions", []):
            if not isinstance(action, dict):
                continue
            if action.get("agent_id") == agent_id:
                lessons.append({
                    "signal": action.get("action", "collaboration_action"),
                    "lesson": action.get("reason", ""),
                    "next_time": self._next_time_for_action(str(action.get("action", ""))),
                })
            elif action.get("target_agent_id") == agent_id:
                lessons.append({
                    "signal": f"received_{action.get('action', 'collaboration_action')}",
                    "lesson": f"{action.get('agent_id')} routed work or support toward this agent: {action.get('reason', '')}",
                    "next_time": "If teammates route work here again, clarify ownership and the acceptance check early.",
                })
        for endorsement in public_room.get("endorsements", []):
            if not isinstance(endorsement, dict):
                continue
            if endorsement.get("agent_id") == agent_id:
                lessons.append({
                    "signal": "deferred_to_peer",
                    "lesson": f"Deferred to {endorsement.get('endorsed_agent_id')} for {endorsement.get('domain')}.",
                    "next_time": "Make the deferral explicit when another agent has a better fit for the decision.",
                })
            elif endorsement.get("endorsed_agent_id") == agent_id:
                lessons.append({
                    "signal": "trusted_by_peer",
                    "lesson": f"{endorsement.get('agent_id')} trusted this agent for {endorsement.get('domain')}.",
                    "next_time": "Carry that trusted domain forward, but still state uncertainty clearly.",
                })
        if agent_id == winner:
            coalition_size = sum(
                1
                for action in public_room.get("collaboration_actions", [])
                if isinstance(action, dict)
                and action.get("action") == "coalition_joined"
                and action.get("target_agent_id") == agent_id
            )
            if coalition_size:
                lessons.append({
                    "signal": "proposal_attracted_support",
                    "lesson": f"Won selection with {coalition_size} visible coalition signal(s).",
                    "next_time": "Repeat the proposal shape that made teammates willing to back it.",
                })
        compact = [lesson for lesson in lessons if lesson.get("lesson")][:6]
        return compact

    def _next_time_for_action(self, action: str) -> str:
        """Translate a collaboration action into a reusable behavioral rule."""

        if action == "help_requested":
            return "Ask for leader support early when ownership or scope could block delivery."
        if action == "ownership_deferred":
            return "Defer ownership when another role has better task fit or elected authority."
        if action == "coalition_joined":
            return "When backing a proposal, state the specific reason so support is inspectable."
        return "Keep the collaboration move explicit and tied to the task evidence."

    def _apply_personality_drift(
        self,
        task_id: str,
        agent: SocietyAgent,
        social_lessons: list[dict[str, Any]],
        deltas: dict[str, float],
    ) -> None:
        """Let profile behavior drift from observed collaboration outcomes."""

        signals = {str(lesson.get("signal", "")) for lesson in social_lessons}
        changes: list[str] = []
        if "raised_blocker" in signals and deltas.get("critique", 0.0) > 0:
            marker = "more assertive in review phases after valid blockers"
            if marker not in agent.profile.values:
                agent.profile.values.append(marker)
                changes.append(marker)
            style_marker = "more direct when a blocker was previously validated"
            if style_marker not in agent.profile.communication_style:
                agent.profile.communication_style = (
                    f"{agent.profile.communication_style}; {style_marker}"
                    if agent.profile.communication_style
                    else style_marker
                )
        if "changed_mind" in signals:
            marker = "offers mitigations when challenged"
            if marker not in agent.profile.values:
                agent.profile.values.append(marker)
                changes.append(marker)
        if "trusted_by_peer" in signals or "proposal_attracted_support" in signals:
            trusted_domain = "lead trusted domains when task fit is clear"
            if trusted_domain not in agent.profile.values:
                agent.profile.values.append(trusted_domain)
                changes.append(trusted_domain)
        if deltas.get("delivery", 0.0) < 0:
            blocker = "missed risk requires explicit next-run mitigation"
            if blocker not in agent.profile.default_blockers:
                agent.profile.default_blockers.append(blocker)
                changes.append(blocker)
        if not changes:
            return
        payload = {
            "agent_id": agent.id,
            "changes": changes,
            "values": agent.profile.values,
            "default_blockers": agent.profile.default_blockers,
            "communication_style": agent.profile.communication_style,
        }
        self._state(task_id).setdefault("personality_drift", []).append(payload)
        self._emit(
            task_id,
            "personality_drifted",
            f"{agent.name}'s collaboration profile drifted from experience.",
            actor=agent.id,
            payload=payload,
        )

    async def _learn(self, task: TaskRun, team: Team, winner: str) -> None:
        state = self._state(task.id)
        eval_metrics = state.get("evaluation_metrics", [])
        task_class = str(state.get("task_class") or "planning")
        social_counts = self._social_trace_counts(state)
        for agent_id in team.member_ids:
            agent = self.agents[agent_id]
            social_lessons = self._social_lessons_for_agent(state, agent_id, winner)
            lesson = {
                "category": "collaboration",
                "lesson": f"Collaborated on '{task.prompt[:80]}' with {len(team.member_ids)} agents.",
                "trigger": "task_complete",
                "applies_to": [agent.role],
                "social_trace": social_counts,
                "social_lessons": social_lessons,
                "confidence": 1.0 if agent_id == winner else 0.7,
            }
            if social_lessons:
                lesson["lesson"] = f"{agent.name} should reuse {len(social_lessons)} social behavior lesson(s) from this run."
            if agent_id == winner:
                lesson["category"] = "delivery"
                winner_entry = state.get("proposals", {}).get(winner, "")
                if isinstance(winner_entry, dict):
                    winner_text = str(winner_entry.get("proposal", ""))
                else:
                    winner_text = str(winner_entry)
                lesson["lesson"] = f"Winning approach for '{task.prompt[:60]}': {winner_text[:120]}"
                if social_lessons:
                    lesson["lesson"] += f" Social behavior to reuse: {social_lessons[0]['signal']}."
            lesson_json = json.dumps(lesson)
            self._emit_tool_call(
                task.id,
                "memory_write",
                agent_id,
                "deterministic" if not self.settings.llm_enabled else "structured",
                {"memory": lesson_json, "tags": ["local-dev" if not self.settings.llm_enabled else "llm-run"]},
                "deterministic_no_key" if not self.settings.llm_enabled else "native_agno",
                True,
            )
            agent.memory.append(lesson_json)
            agent.reputation += 0.15 if agent_id == winner else 0.05
            if self.settings.contextual_trust_enabled:
                deltas = self.reputation.apply_task_result(
                    agent_id,
                    winner,
                    agent.role,
                    evaluation_metrics=eval_metrics,
                    task_class=task_class,
                    social_updates=state.get("trust_updates", []),
                )
            else:
                deltas = self.reputation.apply_task_result(agent_id, winner, agent.role, evaluation_metrics=eval_metrics)
            self._apply_personality_drift(task.id, agent, social_lessons, deltas)
            for domain, delta in deltas.items():
                if abs(delta) < 0.001:
                    continue
                self._record_trust_update(
                    task.id,
                    TrustUpdate(
                        evaluator_id="society",
                        target_agent_id=agent_id,
                        domain=domain,
                        delta=round(delta, 3),
                        reason=(
                            f"Updated {domain} trust for {task_class}; "
                            f"{'won selected proposal' if agent_id == winner else 'participated in completed collaboration'}."
                        ),
                    ),
                )
        active_ids = [agent_id for agent_id in team.member_ids if agent_id in self.agents]
        state["reputations"] = self._reputation_snapshot(active_ids)
        self._emit(task.id, "reputation_updated", "Structured reputation scores were updated.", payload={
            "reputations": state["reputations"],
        })
        self._emit(task.id, "learning_recorded", "Agents updated memory and reputation from the collaboration.")

    def _dissolve(self, task: TaskRun, team: Team) -> None:
        team.status = "dissolved"
        for agent_id in list(team.member_ids):
            agent = self.agents.get(agent_id)
            if agent and agent.parent_id is not None:
                del self.agents[agent_id]
        self._emit(task.id, "team_dissolved", "The temporary team dissolved and members returned to the society pool.", payload={"team_id": team.id})

    def _compose_answer(self, task: TaskRun, team: Team, winner: str, proposals: dict[str, str]) -> str:
        leader = self.agents[team.leader_id or winner]
        selected = proposals[winner]
        state = self._state(task.id)
        completed = [item for item in state.get("subtasks", []) if item.get("status") == "completed"]
        blockers = [item for item in state.get("subtasks", []) if item.get("blockers")]
        delegation_summary = ""
        if completed or blockers:
            delegation_summary = (
                f"\n\nCompleted subtasks: {len(completed)}. "
                f"Blocked subtasks: {len(blockers)}."
            )
        brief = state.get("working_brief") if isinstance(state.get("working_brief"), dict) else {}
        unresolved = list(brief.get("unresolved_dissent", [])) if isinstance(brief, dict) else []
        open_questions = list(brief.get("open_questions", [])) if isinstance(brief, dict) else []
        dissent_summary = ""
        if unresolved or open_questions:
            dissent_summary = (
                "\n\nUnresolved dissent and assumptions: "
                + "; ".join(str(item) for item in [*unresolved[:3], *open_questions[:3]])
            )
        artifact_revisions = [
            item for item in state.get("shared_artifact_revisions", [])
            if isinstance(item, dict)
        ]
        artifact_summary = ""
        if artifact_revisions:
            artifact_summary = (
                "\n\nArtifact collaboration: "
                + "; ".join(
                    f"{item.get('editor_id')} changed {item.get('section')} after {item.get('reviewer_id')}'s critique"
                    for item in artifact_revisions[:3]
                )
            )
        answer = (
            f"Leader: {leader.name}. Selected solution: {selected}\n\n"
            "Execution plan: define success criteria, split work among specialists, "
            "challenge assumptions through review, vote on conflicts, and preserve the "
            "collaboration record for future tasks."
            f"{delegation_summary}"
            f"{dissent_summary}"
            f"{artifact_summary}"
        )
        winner_artifact = next(
            (
                item["id"]
                for item in reversed(state.get("artifacts", []))
                if item.get("type") in {"proposal", "revision"} and item.get("producer") == winner
            ),
            None,
        )
        supporting = [
            ArtifactReference(id=item["id"], type=item["type"], producer=item["producer"])
            for item in state.get("artifacts", [])
            if item.get("type") in {"proposal", "revision", "critique"}
        ]
        final_deliverable = FinalDeliverable(
            answer=answer,
            selected_artifact_id=winner_artifact,
            supporting_artifacts=supporting,
        )
        state["final_deliverable"] = final_deliverable.model_dump()
        self._register_artifact(
            task.id,
            "final_deliverable",
            leader.id,
            "complete",
            final_deliverable.model_dump(),
            depends_on=[ref.id for ref in supporting],
            confidence=1.0,
            status="final",
        )
        return answer

    def _populate_acceptance_checks(self, task_id: str, team: Team, prompt: str = "") -> None:
        """Derive a generic task-derived acceptance contract and evaluate it.

        Produces ``acceptance_evidence`` (the contract + evaluation) and emits
        ``acceptance_evidence_evaluated``.  Each requirement carries a stable
        id, a required flag, evidence types, evidence event ids, passed status,
        and missing-evidence descriptions.  The legacy ``acceptance_checks`` /
        ``failed_checks`` keys are still populated for backward compatibility.
        """

        state = self._state(task_id)
        task_events = self._safe_list_events(task_id)

        proposals = state.get("proposals", {})
        revisions = state.get("revisions", {})
        critique = state.get("critique") or {}
        final_deliverable = state.get("final_deliverable")
        artifacts = state.get("artifacts", [])
        subtasks = state.get("subtasks", [])

        evidence_event_ids_by_type: dict[str, list[str]] = {}
        for ev in task_events:
            evidence_event_ids_by_type.setdefault(ev.type, []).append(ev.id)

        def _eids(*types: str) -> list[str]:
            out: list[str] = []
            for t in types:
                out.extend(evidence_event_ids_by_type.get(t, []))
            return out

        requirements: list[dict[str, Any]] = []

        has_proposals = len(proposals) > 0
        requirements.append({
            "id": "proposals_exist",
            "required": True,
            "evidence_types": ["artifact_state"],
            "evidence_event_ids": _eids("agent_proposal_submitted", "leader_synthesis"),
            "passed": has_proposals,
            "missing_evidence": [] if has_proposals else ["no proposals recorded"],
        })

        has_winner = state.get("winner_id") is not None
        requirements.append({
            "id": "winner_selected",
            "required": True,
            "evidence_types": ["vote_state"],
            "evidence_event_ids": _eids("winner_selected", "solution_selected", "ballots_tallied"),
            "passed": has_winner,
            "missing_evidence": [] if has_winner else ["no winner_id in session state"],
        })

        has_critique = isinstance(critique, dict) and bool(critique.get("critique"))
        requirements.append({
            "id": "critique_exists",
            "required": False,
            "evidence_types": ["monitor_state"],
            "evidence_event_ids": _eids("peer_monitor_report", "artifact_section_critiqued"),
            "passed": has_critique,
            "missing_evidence": [] if has_critique else ["no critique recorded"],
        })

        has_final = final_deliverable is not None
        requirements.append({
            "id": "final_deliverable_exists",
            "required": True,
            "evidence_types": ["compose_state"],
            "evidence_event_ids": _eids("leader_synthesis"),
            "passed": has_final,
            "missing_evidence": [] if has_final else ["no final deliverable"],
        })

        final_artifacts = [a for a in artifacts if a.get("type") == "final_deliverable" and a.get("status") == "final"]
        has_final_artifact = len(final_artifacts) > 0
        requirements.append({
            "id": "final_artifact_registered",
            "required": True,
            "evidence_types": ["artifact_ledger"],
            "evidence_event_ids": _eids("artifact_registered"),
            "passed": has_final_artifact,
            "missing_evidence": [] if has_final_artifact else ["no final artifact in ledger"],
        })

        blocked = [s for s in subtasks if isinstance(s, dict) and s.get("blockers")]
        required_blocked = [s for s in blocked if s.get("blocking_if_missing")]
        no_blocked = len(blocked) == 0
        requirements.append({
            "id": "no_blocked_subtasks",
            "required": bool(required_blocked),
            "evidence_types": ["delegation_state"],
            "evidence_event_ids": _eids("delegation_assigned", "delegation_reported"),
            "passed": no_blocked,
            "missing_evidence": [] if no_blocked else [
                f"{len(blocked)} subtask(s) have blockers; "
                f"{len(required_blocked)} are marked blocking_if_missing"
            ],
        })

        composition_validation = self._fixed_composition_validation_evidence(task_id)
        if composition_validation is not None:
            validation_passed = composition_validation["validation_passed"]
            requirements.append({
                "id": "fixed_specialist_independent_validation",
                "required": True,
                "evidence_types": ["fixed_test_engineer"],
                "evidence_event_ids": composition_validation["validation_event_ids"],
                "passed": validation_passed,
                "missing_evidence": [] if validation_passed else ["no passed local independent validation from the selected Test Engineer"],
            })
            cleanup_complete = composition_validation["cleanup_complete"]
            requirements.append({
                "id": "fixed_specialist_sandbox_cleanup",
                "required": True,
                "evidence_types": ["composition_cleanup"],
                "evidence_event_ids": composition_validation["cleanup_event_ids"],
                "passed": cleanup_complete,
                "missing_evidence": [] if cleanup_complete else [
                    f"missing cleanup evidence for assignments: {composition_validation['missing_cleanup_assignment_ids']}"
                ],
            })

        proof = state.get("demo_proof") if isinstance(state.get("demo_proof"), dict) else {}
        proof_verified = bool(proof.get("verified"))
        requirements.append({
            "id": "demo_proof_verified",
            "required": False,
            "evidence_types": ["demo_proof"],
            "evidence_event_ids": _eids("demo_proof_verified"),
            "passed": proof_verified,
            "missing_evidence": [] if proof_verified else [", ".join(proof.get("missing_markers", [])) or "proof event missing"],
        })

        answer = str((final_deliverable or {}).get("answer", "")) if isinstance(final_deliverable, dict) else ""
        effective_prompt = prompt or str(state.get("prompt", ""))
        no_mock_compliant = not (_prompt_has_no_mock_constraint(effective_prompt) and _contains_mock_recommendation(answer))
        requirements.append({
            "id": "no_mock_constraint",
            "required": True,
            "evidence_types": ["final_deliverable"],
            "evidence_event_ids": _eids("leader_synthesis"),
            "passed": no_mock_compliant,
            "missing_evidence": [] if no_mock_compliant else ["final answer recommends mocked or simulated behavior"],
        })

        required_reqs = [r for r in requirements if r["required"]]
        optional_reqs = [r for r in requirements if not r["required"]]
        required_passed = all(r["passed"] for r in required_reqs)
        optional_failures = [r["id"] for r in optional_reqs if not r["passed"]]
        required_failures = [r["id"] for r in required_reqs if not r["passed"]]

        recoverable = False
        if required_failures:
            retryable_subtasks = [
                s for s in subtasks
                if isinstance(s, dict)
                and s.get("status") in {"failed", "pending", "assigned"}
                and not s.get("exhausted")
            ]
            recoverable = len(retryable_subtasks) > 0

        if required_passed and not optional_failures:
            terminal = "complete"
        elif required_passed and optional_failures:
            terminal = "complete_with_warnings"
        elif recoverable:
            terminal = "remediation"
        else:
            terminal = "failed"

        evaluation = {
            "requirements": requirements,
            "required_passed": required_passed,
            "required_failures": required_failures,
            "optional_failures": optional_failures,
            "recoverable": recoverable,
            "terminal_status": terminal,
        }

        state["acceptance_evidence"] = evaluation
        state["acceptance_checks"] = [
            {"check": r["id"], "passed": r["passed"], "source": r["evidence_types"][0] if r["evidence_types"] else "unknown"}
            for r in requirements
        ]
        state["failed_checks"] = [
            {"check": r["id"], "reason": r["missing_evidence"][0] if r["missing_evidence"] else "failed"}
            for r in requirements if not r["passed"]
        ]

        self._emit(
            task_id,
            "acceptance_evidence_evaluated",
            f"Acceptance evidence evaluated: {terminal}.",
            actor=team.leader_id,
            payload=evaluation,
        )

    def _apply_validation_gate(self, task: TaskRun, team: Team) -> str:
        """Validate the final deliverable and revise it when checks fail."""

        state = self._state(task.id)
        answer = task.final_answer or ""
        checks = state.get("acceptance_checks", [])
        failed = state.get("failed_checks", [])
        evidence = state.get("acceptance_evidence")
        passed = (
            evidence.get("required_passed") is True
            if isinstance(evidence, dict)
            else len(failed) == 0
        )
        required_failures = set(evidence.get("required_failures", [])) if isinstance(evidence, dict) else set()
        gate_failures = (
            [item for item in failed if isinstance(item, dict) and item.get("check") in required_failures]
            if isinstance(evidence, dict)
            else list(failed)
        )
        payload = {
            "passed": passed,
            "checks": checks,
            "failed_checks": gate_failures,
            "optional_failures": evidence.get("optional_failures", []) if isinstance(evidence, dict) else [],
        }
        self._emit(
            task.id,
            "validation_gate_completed",
            "Validation gate passed." if passed else "Validation gate found issues and revised the deliverable.",
            actor=team.leader_id,
            payload=payload,
        )
        if passed:
            verified_summary = self._verified_fixed_composition_outcome(task.id)
            if verified_summary is not None:
                optional_failures = evidence.get("optional_failures", []) if isinstance(evidence, dict) else []
                if optional_failures:
                    verified_summary += f" Optional demo-proof gaps remain: {', '.join(str(item) for item in optional_failures)}."
                state["final_answer"] = verified_summary
                final_deliverable = state.get("final_deliverable")
                if isinstance(final_deliverable, dict):
                    final_deliverable["answer"] = verified_summary
                    final_deliverable["validation_status"] = "verified_fixed_composition"
                return verified_summary
            return answer

        failed_summary = "; ".join(
            f"{item.get('check', 'unknown')}: {item.get('reason', 'failed')}"
            for item in failed
            if isinstance(item, dict)
        )
        revised = (
            f"{answer}\n\nValidation note: the society detected unresolved checks before completion "
            f"({failed_summary}). Treat this as a bounded deliverable with the listed caveats."
        )
        state["final_answer"] = revised
        final_deliverable = state.get("final_deliverable")
        if isinstance(final_deliverable, dict):
            final_deliverable["answer"] = revised
            final_deliverable["validation_status"] = "revised_with_caveats"
            final_deliverable["failed_checks"] = failed
        self._register_artifact(
            task.id,
            "validation_revision",
            team.leader_id or team.member_ids[0],
            "validation",
            {
                "answer": revised,
                "failed_checks": failed,
            },
            confidence=0.6,
            status="revised",
        )
        return revised

    def _verified_fixed_composition_outcome(self, task_id: str) -> str | None:
        """Render terminal truth from passed fixed-specialist runtime evidence.

        A pre-execution leader synthesis is deliberation, not a terminal
        runtime verdict. This replaces it only after the acceptance record
        explicitly proves the immutable Test Engineer validation and every
        selected sandbox cleanup passed.
        """

        state = self._state(task_id)
        evidence = state.get("acceptance_evidence")
        if not isinstance(evidence, dict) or evidence.get("required_passed") is not True:
            return None
        requirements = {
            item.get("id"): item
            for item in evidence.get("requirements", [])
            if isinstance(item, dict) and item.get("id")
        }
        required_ids = {"fixed_specialist_independent_validation", "fixed_specialist_sandbox_cleanup"}
        if not required_ids.issubset(requirements) or not all(requirements[item_id].get("passed") is True for item_id in required_ids):
            return None

        artifact_refs: list[str] = []
        for event in self._safe_list_events(task_id):
            if event.type != "agentbay_artifact_exported" or not isinstance(event.payload, dict):
                continue
            payload = event.payload
            reference = payload.get("workspace_relative_path") or payload.get("path")
            artifact_ref = payload.get("artifact_ref")
            if not reference and isinstance(artifact_ref, dict):
                reference = artifact_ref.get("storage_path") or artifact_ref.get("workspace_relative_path")
            if isinstance(reference, str) and reference.strip() and reference.strip() not in artifact_refs:
                artifact_refs.append(reference.strip())
        artifact_text = ", ".join(artifact_refs[:6]) if artifact_refs else "the exported artifact references recorded by the runtime"
        return (
            "Verified runtime outcome: the fixed Builder/Test Engineer execution passed independent validation, "
            f"exported {artifact_text}, and closed every selected execution environment successfully. "
            "Pre-validation deliberation remains in the event history and is not the terminal verdict."
        )

    def _record_demo_proof(self, task: TaskRun, team: Team) -> None:
        """Emit a factual checklist for the judge-facing Agent Society story.

        This is deliberately an observation, not a completion gate: a run may
        finish with a missing marker, and the UI must expose that gap instead
        of fabricating a successful society narrative.
        """

        state = self._state(task.id)
        bundles = state.get("tool_bundles")
        role_keys = sorted({
            str(bundle.get("role_key"))
            for bundle in bundles.values()
            if isinstance(bundle, dict) and bundle.get("role_key")
        }) if isinstance(bundles, dict) else []
        if len(role_keys) < 3:
            completed_agents = {
                str(subtask.get("agent_id"))
                for subtask in state.get("subtasks", [])
                if isinstance(subtask, dict)
                and subtask.get("agent_id") in self.agents
                and subtask.get("status") == "completed"
                and subtask.get("outcome_status") == "completed"
            }
            role_keys = sorted({self._role_key(self.agents[agent_id]) for agent_id in completed_agents})

        evidence_subtask_ids: list[str] = []
        for subtask in state.get("subtasks", []):
            if not isinstance(subtask, dict):
                continue
            refs = subtask.get("evidence_refs")
            if not isinstance(refs, list):
                continue
            real_refs = [
                str(reference).strip()
                for reference in refs
                # This exact sentinel is emitted only when reporting itself
                # failed. A legitimate source may still contain the word
                # "fallback" in its title or URL.
                if str(reference).strip() and str(reference).strip().casefold() != "fallback work product"
            ]
            if real_refs and subtask.get("id"):
                evidence_subtask_ids.append(str(subtask["id"]))

        synthesis = state.get("leader_synthesis")
        synthesis = synthesis if isinstance(synthesis, dict) else {}
        carried_dissent = synthesis.get("carried_dissent")
        carried_dissent = carried_dissent if isinstance(carried_dissent, list) else []
        brief = state.get("working_brief")
        if isinstance(brief, dict):
            brief_dissent = brief.get("unresolved_dissent", [])
            if isinstance(brief_dissent, list):
                for item in brief_dissent:
                    text = str(item).strip()
                    if text and text not in carried_dissent:
                        carried_dissent.append(text)
        for event in reversed(self._safe_list_events(task.id)):
            if event.type != "winner_selected":
                continue
            winner_dissent = event.payload.get("dissent_carried") if isinstance(event.payload, dict) else None
            if isinstance(winner_dissent, list):
                for item in winner_dissent:
                    text = str(item).strip()
                    if text and text not in carried_dissent:
                        carried_dissent.append(text)
            break
        final_deliverable = state.get("final_deliverable")
        final_deliverable = final_deliverable if isinstance(final_deliverable, dict) else {}

        markers = {
            "distinct_competencies": len(role_keys) >= 3,
            "evidence_producing_delegation": bool(evidence_subtask_ids),
            "leader_decision": bool(synthesis.get("winning_proposal_summary")),
            # A hackathon proof run must demonstrate a real disagreement that
            # survived into the leader decision; unanimous runs remain honest
            # but do not satisfy this particular showcase marker.
            "carried_dissent": bool(carried_dissent),
            "final_artifact": bool(final_deliverable.get("selected_artifact_id")),
        }
        payload = {
            "verified": all(markers.values()),
            "markers": markers,
            "competency_roles": role_keys,
            "evidence_subtask_ids": evidence_subtask_ids,
            "leader_id": team.leader_id,
            "carried_dissent_count": len(carried_dissent),
            "final_artifact_id": final_deliverable.get("selected_artifact_id"),
            "missing_markers": [name for name, present in markers.items() if not present],
        }
        state["demo_proof"] = payload
        self._emit(
            task.id,
            "demo_proof_verified",
            "Agent Society proof chain verified." if payload["verified"] else "Agent Society proof chain has visible gaps.",
            actor=team.leader_id,
            payload=payload,
        )

    def _record_task_metrics(self, task: TaskRun, start_time: float) -> None:
        """Compute, store, and emit V3 metrics for a completed task."""

        state = self._state(task.id)
        state["phase"] = "complete"
        state["final_answer"] = task.final_answer or ""
        metrics = compute_task_metrics(state, start_time)
        self.metrics.record(metrics)
        self._emit(task.id, "task_metrics", "Structured evaluation metrics were recorded.", payload=metrics.model_dump())

    def _emit(self, task_id: str, event_type: str, message: str, actor: str | None = None, payload: dict | None = None) -> None:
        self.events.append(SocietyEvent(task_id=task_id, type=event_type, message=message, actor=actor, payload=payload or {}))
