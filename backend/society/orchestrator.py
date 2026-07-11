from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Type, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from config import Settings, get_settings
from .agents import build_agno_agent, fallback_contribution, role_tool_context, role_tool_instructions
from .context7_research import collect_context7_evidence, format_context7_evidence
from .db import get_agno_db
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
from .schemas.evaluation import TaskMetrics, MetricRecord
from .schemas.artifacts import ArtifactRecord, ArtifactReference, FinalDeliverable
from .schemas.debate import ChallengeRecord, ProposalOpinionRecord, ProposalRecord, RevisionRecord
from .schemas.delegation import SubtaskAssignment, SubtaskReport
from .schemas.voting import TallyResult
from .schemas.conversation import (
    ConversationTurn,
    GoalDiscussionStatement,
    MeetingRecap,
    ReadinessBallot,
    ReadinessTally,
    TargetedQuestionExchange,
    WorkingBrief,
)
from .schemas.coordination import CoordinationSubtaskHint, TeamCoordinationBrief
from .schemas.social import AgentPosition, CollaborationAction, EndorsementRecord, MindChangeRecord, ObjectionRecord, PrivateNote, TrustUpdate
from .session import initial_session_state
from .team import build_society_team
from .tools.capabilities import (
    decompose_task_tool,
    implementation_plan_tool,
    memory_lookup_tool,
    memory_write_tool,
    risk_assessment_tool,
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
from .tools.evaluation import record_metric_tool
from .tools.conversation import submit_goal_discussion_tool, cast_readiness_vote_tool
from .tools.social import publish_private_note_tool, record_private_note_tool, state_position_tool

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


def _extract_tool_result(
    response: Any,
    tool_name: str,
    schema_class: Type[T],
) -> T:
    """Extract and validate a native tool result from an Agno response.

    Governance in LLM-enabled mode is only valid when Agno exposes an explicit
    tool result. A model response that merely contains JSON in prose is rejected.
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
        raise ValueError(f"No tool result found for '{tool_name}' in agent response")

    parsed = _parse_json_response(raw_json)
    if parsed is None:
        raise ValueError(f"Could not parse tool result JSON for '{tool_name}': {raw_json[:200]}")

    try:
        return schema_class.model_validate(parsed)
    except ValidationError as exc:
        raise ValueError(f"Tool result validation failed for '{tool_name}': {exc}") from exc


def _fallback_goal_discussion(task: TaskRun, actor: SocietyAgent, prompt: str, reason: str) -> GoalDiscussionStatement:
    """Create a typed meeting contribution when a non-critical discussion tool call fails.

    This keeps the visible run moving while preserving that the model/tool path
    had a recoverable shape error. It is intentionally limited to the
    pre-execution conversation because later governance decisions still require
    explicit tool output.
    """

    return GoalDiscussionStatement(
        round=1,
        agent_id=actor.id,
        stance="clarifies",
        interpretation=task.prompt[:240],
        unique_contribution=f"{actor.name} could not provide a structured tool response, so the society is carrying a cautious fallback note.",
        success_criteria=[],
        concerns=[f"Recovered from missing tool result: {reason[:160]}"],
        suggested_scope="Continue with the mission brief and preserve the tool failure as a caveat.",
        question_for_next=None,
        spoken_turn=f"I hit a tool-format issue, so I am keeping my contribution conservative: continue from the brief and treat my missing structured output as a caveat.",
    )


def _fallback_readiness_ballot(actor: SocietyAgent, reason: str) -> ReadinessBallot:
    """Return a cautious ready vote when the readiness tool transport fails."""

    return ReadinessBallot(
        attempt=1,
        agent_id=actor.id,
        ready=True,
        critical_blocker=False,
        reason=f"Recovered from readiness tool issue; no critical blocker was produced. {reason[:120]}",
    )


def _fallback_agent_position(actor: SocietyAgent, phase: str, reason: str) -> AgentPosition:
    """Return a visible neutral/support stance for non-critical social tracing."""

    return AgentPosition(
        agent_id=actor.id,
        phase=phase,
        stance="support",
        reason=f"Recovered from social tool issue; carrying the brief forward with caveat. {reason[:120]}",
        confidence=0.5,
        conditions=[],
    )


def _fallback_private_note(actor: SocietyAgent, phase: str, reason: str) -> PrivateNote:
    """Return a private note that preserves a recoverable social-tool issue."""

    return PrivateNote(
        agent_id=actor.id,
        phase=phase,
        note=f"Recovered from private-note tool issue: {reason[:180]}",
        may_publish=False,
    )


class GovernanceToolError(Exception):
    """Raised when a native governance tool call fails in LLM-enabled mode."""


class SocietyOrchestrator:
    """Coordinates the full Agent Society lifecycle for each submitted task."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.events = EventStore(Path(__file__).resolve().parent / "data" / "events.jsonl")
        self.tasks: dict[str, TaskRun] = {}
        self.teams: dict[str, Team] = {}
        self.session_states: dict[str, dict[str, Any]] = {}
        self.agno_teams: dict[str, Any] = {}
        self.workflows: dict[str, Any] = {}
        self.metrics = MetricsCollector()
        self.reputation = ReputationStore()
        self.agno_db = get_agno_db(self.settings)
        self.agents: dict[str, SocietyAgent] = self._seed_agents()
        self._load_reputation_from_events()

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
            await self._spawn_child_agent(task, team)
            proposals = await self._negotiate(task, team)
            await self._delegate_subtasks(task, team)
            await self._collect_proposal_opinions(task, team, proposals)
            winner = await self._vote(task, team, proposals)
            await self._monitor(task, team, winner, proposals)
            task.final_answer = self._compose_answer(task, team, winner, proposals)
            self._populate_acceptance_checks(task.id, team)
            task.final_answer = self._apply_validation_gate(task, team)
            await self._record_evaluation_metrics(task, team, winner)
            await self._learn(task, team, winner)
            self._dissolve(task, team)
            task.status = "complete"
            task.updated_at = now_iso()
            self._record_task_metrics(task, start_time)
            self._emit(task.id, "task_complete", "The society produced a final answer.", payload={"answer": task.final_answer})
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

        clarification = {
            "status": "answered",
            "answer": answer,
            "answered_at": now_iso(),
            "resume_phase": state.get("resume_phase") or "post_readiness",
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
        await self._execute_after_readiness(task, team, time.time())
        return task

    async def resume_with_clarification(self, task_id: str, answer: str) -> TaskRun:
        """Inject a user clarification and resume from the paused phase."""

        task = self.apply_user_clarification(task_id, answer)
        await self.continue_after_clarification(task_id)
        return task

    def _form_team(self, task: TaskRun) -> Team:
        member_ids = list(self.agents.keys())[:4]
        team = Team(task_id=task.id, member_ids=member_ids, status="active")
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
        return team

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
            response = await asyncio.wait_for(
                agno_team.arun(
                    prompt,
                    session_id=task.id,
                    session_state=self._state(task.id),
                    add_session_state_to_context=True,
                ),
                timeout=max(self.settings.llm_timeout_seconds, 120),
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
            await self._run_targeted_question_exchange(task, team)
            if self.settings.readiness_voting_enabled:
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
                    "Call submit_goal_discussion with your exact agent_id."
                ),
            )
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

    async def _run_readiness_vote(self, task: TaskRun, team: Team, attempt: int) -> bool:
        """Collect readiness ballots from each team member and tally."""

        state = self._state(task.id)
        ready_count = 0
        not_ready_count = 0
        blockers: list[str] = []
        for agent_id in team.member_ids:
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
            ballot = await self._run_governance_tool(
                task=task,
                actor_identity=agent,
                tool_func=cast_readiness_vote_tool,
                tool_name="cast_readiness_vote",
                schema_class=ReadinessBallot,
                prompt=(
                    f"Task: {task.prompt}\n"
                    f"Your exact agent_id is {agent_id}.\n"
                    f"Readiness attempt: {attempt}.\n"
                    f"Your latest discussion statement: {latest_statement}\n"
                    "Vote whether the society is ready to execute. "
                    "Set critical_blocker=true only if execution would be misleading without user clarification. "
                    "Call cast_readiness_vote with your exact agent_id."
                ),
            )
            if ballot.ready:
                ready_count += 1
            else:
                not_ready_count += 1
            if ballot.critical_blocker:
                blockers.append(f"{agent_id}: {ballot.reason}")
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
        total = ready_count + not_ready_count
        passed = ready_count > total / 2 and not blockers
        tally = ReadinessTally(
            attempt=attempt,
            ready_count=ready_count,
            not_ready_count=not_ready_count,
            total=total,
            passed=passed,
            blockers=blockers,
        )
        state["readiness_tally"] = tally.model_dump()
        state["ready_to_proceed"] = passed if passed else state.get("ready_to_proceed")
        self._emit(task.id, "readiness_vote_tallied", f"Readiness vote tallied for attempt {attempt}.", payload=tally.model_dump())
        return passed

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

        try:
            response = await asyncio.wait_for(
                agno_agent.arun(prompt),
                timeout=self.settings.llm_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            if schema_class is GoalDiscussionStatement:
                result = _fallback_goal_discussion(task, actor_identity, prompt, str(exc))
                self._state(task.id).setdefault("goal_discussions", []).append(result.model_dump())
                self._emit_tool_call(task.id, tool_name, actor_identity.id, prompt[:200], result.model_dump(), "native_agno", False)
                return result
            if schema_class is ReadinessBallot:
                result = _fallback_readiness_ballot(actor_identity, str(exc))
                self._emit_tool_call(task.id, tool_name, actor_identity.id, prompt[:200], result.model_dump(), "native_agno", False)
                return result
            if schema_class is AgentPosition:
                result = _fallback_agent_position(actor_identity, self._state(task.id).get("phase", "unknown"), str(exc))
                self._emit_tool_call(task.id, tool_name, actor_identity.id, prompt[:200], result.model_dump(), "native_agno", False)
                return result
            if schema_class is PrivateNote:
                result = _fallback_private_note(actor_identity, self._state(task.id).get("phase", "unknown"), str(exc))
                self._emit_tool_call(task.id, tool_name, actor_identity.id, prompt[:200], result.model_dump(), "native_agno", False)
                return result
            self._emit_tool_call(task.id, tool_name, actor_identity.id, prompt[:200], {}, "native_agno", False)
            raise GovernanceToolError(f"Timeout calling {tool_name}: {exc}") from exc
        except Exception as exc:
            self._emit_tool_call(task.id, tool_name, actor_identity.id, prompt[:200], {}, "native_agno", False)
            raise GovernanceToolError(f"Agent run failed for {tool_name}: {exc}") from exc

        try:
            result = _extract_tool_result(response, tool_name, schema_class)
        except ValueError as exc:
            retry_prompt = (
                f"{prompt}\n\n"
                f"Your previous response did not call the required `{tool_name}` tool. "
                f"Retry now and call `{tool_name}` exactly once. "
                "Do not explain, summarize, or answer in prose outside the tool call."
            )
            try:
                retry_response = await asyncio.wait_for(
                    agno_agent.arun(retry_prompt),
                    timeout=self.settings.llm_timeout_seconds,
                )
                result = _extract_tool_result(retry_response, tool_name, schema_class)
            except (asyncio.TimeoutError, ValueError) as retry_exc:
                if schema_class is GoalDiscussionStatement:
                    result = _fallback_goal_discussion(task, actor_identity, retry_prompt, str(retry_exc))
                    self._state(task.id).setdefault("goal_discussions", []).append(result.model_dump())
                    self._emit_tool_call(
                        task.id,
                        tool_name,
                        actor_identity.id,
                        retry_prompt[:200],
                        {"recovered": True, "reason": str(retry_exc)[:300], **result.model_dump()},
                        "native_agno",
                        False,
                    )
                    return result
                if schema_class is ReadinessBallot:
                    result = _fallback_readiness_ballot(actor_identity, str(retry_exc))
                    self._emit_tool_call(task.id, tool_name, actor_identity.id, retry_prompt[:200], result.model_dump(), "native_agno", False)
                    return result
                if schema_class is AgentPosition:
                    result = _fallback_agent_position(actor_identity, self._state(task.id).get("phase", "unknown"), str(retry_exc))
                    self._emit_tool_call(task.id, tool_name, actor_identity.id, retry_prompt[:200], result.model_dump(), "native_agno", False)
                    return result
                if schema_class is PrivateNote:
                    result = _fallback_private_note(actor_identity, self._state(task.id).get("phase", "unknown"), str(retry_exc))
                    self._emit_tool_call(task.id, tool_name, actor_identity.id, retry_prompt[:200], result.model_dump(), "native_agno", False)
                    return result
                self._emit_tool_call(task.id, tool_name, actor_identity.id, retry_prompt[:200], {}, "native_agno", False)
                raise GovernanceToolError(
                    f"Could not extract valid tool result for {tool_name} after retry: {retry_exc}"
                ) from retry_exc
            except Exception as retry_exc:
                self._emit_tool_call(task.id, tool_name, actor_identity.id, retry_prompt[:200], {}, "native_agno", False)
                raise GovernanceToolError(f"Agent retry failed for {tool_name}: {retry_exc}") from retry_exc

        result_dict = result.model_dump()
        self._emit_tool_call(task.id, tool_name, actor_identity.id, prompt[:200], result_dict, "native_agno", True)
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
        if not self.settings.llm_enabled:
            task_class = str(state.get("task_class") or "planning")
            leader_id = max(team.member_ids, key=lambda aid: self._leadership_score(aid, task_class) + len(self.agents[aid].skills) * 0.05)
            team.leader_id = leader_id
            state = self._state(task.id)
            state["phase"] = "leader_elected"
            state["leader_id"] = leader_id
            state["reputations"] = self._reputation_snapshot(team.member_ids)
            state["metrics"]["governance_rounds"] += 1
            reason = f"deterministic contextual trust score for {task_class}"
            self._emit(task.id, "leader_elected", f"{self.agents[leader_id].name} was elected task leader.", actor=leader_id, payload={"reason": reason, "task_class": task_class, "leadership_score": self._leadership_score(leader_id, task_class)})
            self._emit_tool_call(task.id, "elect_leader", leader_id, "deterministic", {"leader_id": leader_id, "reason": reason, "confidence": 1.0}, "deterministic_no_key", True)
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
        state["child_agents"].append(child_record)
        state["spawn_decision"] = {"spawn": True, "reason": reason, "child_id": child.id, "specialist_role": specialist_role}
        self._emit(task.id, "child_agent_spawned", "The leader spawned a specialist child agent.", actor=leader.id, payload={"child": child.model_dump(), "reason": reason})

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
        research_markers = ("agno", "mcp", "context7", "sdk", "library", "framework")
        needs_research_evidence = any(marker in task.prompt.lower() for marker in research_markers)
        has_researcher_subtask = any(
            subtask.get("agent_id") == "researcher"
            and subtask.get("status") == "planned"
            for subtask in planned
        )
        if needs_research_evidence and "researcher" in team.member_ids and not has_researcher_subtask:
            researcher_subtask = {
                "id": f"subtask-{uuid4().hex[:10]}",
                "agent_id": "researcher",
                "subtask": (
                    "Collect Context7-backed evidence for the technical documentation claims, "
                    "identify the exact Agno API pattern, and list unsupported assumptions before implementation work proceeds."
                ),
                "status": "planned",
                "source": "required_research_evidence",
            }
            subtasks.append(researcher_subtask)
            planned.insert(0, researcher_subtask)
            self._emit(
                task.id,
                "research_subtask_required",
                "A researcher evidence subtask was inserted before implementation work.",
                actor="researcher",
                payload=researcher_subtask,
            )
        leader_id = team.leader_id or team.member_ids[0]
        processed: list[dict[str, Any]] = []
        for index, subtask in enumerate(planned, start=1):
            subtask.setdefault("id", f"subtask-{uuid4().hex[:10]}")
            subtask.setdefault("deadline_step", index)
            agent_id = subtask["agent_id"]
            if agent_id not in team.member_ids:
                continue
            agent = self.agents[agent_id]
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
            try:
                work_result = await self._ask_agent(agent, work_prompt, task_id=task.id)
            except (asyncio.TimeoutError, Exception) as exc:
                work_result = (
                    f"Recovered fallback work product for {agent.name}: {subtask.get('subtask', '')}. "
                    f"The agent work-product call failed with {type(exc).__name__}; continue with this subtask as partial evidence."
                )
                self._emit_tool_call(
                    task.id,
                    "agent_work_product",
                    agent_id,
                    work_prompt[:200],
                    {"result": work_result, "recovered": True, "error": f"{type(exc).__name__}: {str(exc)[:200]}"},
                    "native_agno",
                    False,
                )

            report_prompt = (
                f"Task: {task.prompt}\n"
                f"Report the result for subtask {subtask['id']}: {subtask.get('subtask', '')}\n"
                f"Work product to report:\n{work_result[:3000]}\n"
                "Call report_subtask with the exact subtask_id and your agent_id. "
                "Include result_summary (one-line), evidence_refs (list of evidence sources used), "
                "outcome_status (completed, partial, blocked, or failed), "
                "outcome_summary (one-line outcome for cockpit), and provenance."
            )
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
                    status="completed",
                    agent_id=agent_id,
                    result=work_result,
                    result_summary=work_result[:240],
                    blockers=[],
                    evidence_refs=["fallback work product"],
                    outcome_status="partial",
                    outcome_summary=f"Recovered from report tool issue: {str(exc)[:160]}",
                    provenance="agent_report_fallback",
                )
                self._emit_tool_call(task.id, "report_subtask", agent_id, report_prompt[:200], report.model_dump(), "native_agno", False)
            update_fields = report.model_dump()
            if update_fields.get("status") == "not_found":
                update_fields["status"] = "blocked"
                update_fields["blockers"] = [
                    "report_subtask did not match an assigned subtask in session state."
                ]
            subtask.update(update_fields)
            resolved_outcome_status = report.outcome_status or subtask.get("status", "completed")
            resolved_result_summary = report.result_summary or report.result[:240]
            resolved_outcome_summary = report.outcome_summary or resolved_result_summary
            resolved_provenance = report.provenance or "agent_report"
            resolved_evidence_refs = report.evidence_refs or subtask.get("evidence_refs", [])
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
        changes = revision.get("changes") or []
        change_summary = "; ".join(str(item) for item in changes[:2]) if isinstance(changes, list) else str(changes)
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

    async def _ask_agent(self, identity: SocietyAgent, prompt: str, context: str = "", task_id: str | None = None) -> str:
        if not self.settings.llm_enabled:
            return fallback_contribution(identity, prompt)
        evidence_context = ""
        if (
            identity.id == "researcher"
            and task_id is not None
            and self.settings.context7_mcp_enabled
            and any(marker in f"{prompt}\n{context}".lower() for marker in ("agno", "mcp", "context7", "sdk", "library", "framework"))
        ):
            try:
                evidence = await collect_context7_evidence(
                    self.settings,
                    f"{prompt}\n\n{context}"[:4000],
                )
            except Exception as exc:
                evidence = {
                    "success": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "tool_calls": [],
                }
            self._state(task_id).setdefault("research_evidence", []).append(evidence)
            self._emit(
                task_id,
                "research_evidence_collected",
                "Researcher collected Context7 evidence before answering.",
                actor=identity.id,
                payload=evidence,
            )
            evidence_context = format_context7_evidence(evidence)
        full_prompt = f"{prompt}\n\n{context}" if context else prompt
        if evidence_context:
            full_prompt = f"{full_prompt}\n\n{evidence_context}\n\nUse only this evidence for documentation-specific claims. Label anything else as inference."
        async with role_tool_context(identity, self.settings) as role_tools:
            agno_agent = build_agno_agent(
                identity,
                self.settings,
                tools=role_tools or None,
                extra_instructions=role_tool_instructions(identity, self.settings),
                session_id=task_id,
                session_state=self._state(task_id) if task_id is not None else None,
            )
            response = await asyncio.wait_for(
                agno_agent.arun(full_prompt),
                timeout=self.settings.llm_timeout_seconds,
            )
        return str(getattr(response, "content", response))

    async def _collect_proposal_opinions(self, task: TaskRun, team: Team, proposals: dict[str, str]) -> None:
        """Collect public opinions about each proposal before voting.

        Emits proposal_opinion_recorded events with proposal_id, agent_id,
        stance, opinion, confidence, and phase. If no meaningful opinion is
        available, uses an explicit neutral stance rather than fabricating.
        """

        state = self._state(task.id)
        proposal_id_map = state.get("proposal_id_map", {})
        for agent_id in team.member_ids:
            agent = self.agents[agent_id]
            for proposer_id, proposal_text in proposals.items():
                if proposer_id == agent_id:
                    continue
                proposal_id = proposal_id_map.get(proposer_id) or f"prop-{proposer_id}"
                if self.settings.llm_enabled and self.settings.native_debate_enabled:
                    opinion = await self._run_governance_tool(
                        task=task,
                        actor_identity=agent,
                        tool_func=record_proposal_opinion_tool,
                        tool_name="record_proposal_opinion",
                        schema_class=ProposalOpinionRecord,
                        prompt=(
                            f"Task: {task.prompt}\n"
                            f"Your exact agent_id is {agent_id}.\n"
                            f"Proposal from {proposer_id} (id: {proposal_id}):\n{proposal_text}\n"
                            "State your honest opinion of this proposal. "
                            "Use stance: support, oppose, uncertain, or neutral. "
                            "If you have no meaningful view, use neutral. "
                            "Call record_proposal_opinion with your exact agent_id and the proposal_id."
                        ),
                    )
                    opinion_payload = opinion.model_dump()
                else:
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

    async def _vote(self, task: TaskRun, team: Team, proposals: dict[str, str]) -> str:
        votes = []
        candidates = list(proposals.keys())

        for voter_id in team.member_ids:
            if self.settings.llm_enabled and self.settings.native_voting_enabled:
                voter = self.agents[voter_id]
                summary = "\n".join(f"- {cid}: {self.agents[cid].name}, role={self.agents[cid].role}" for cid in candidates)
                prompt = (
                    f"Task: {task.prompt}\n"
                    f"Your exact voter_id is {voter_id}.\n"
                    f"Candidates:\n{summary}\n"
                    f"Vote for the best proposal. The choice must be one of: {', '.join(candidates)}.\n"
                    "Call the cast_ballot tool with your exact voter_id and decision."
                )
                decision = await self._run_governance_tool(
                    task=task,
                    actor_identity=voter,
                    tool_func=cast_ballot_tool,
                    tool_name="cast_ballot",
                    schema_class=VoteDecision,
                    prompt=prompt,
                )
                choice = decision.choice
                if choice not in candidates:
                    raise GovernanceToolError(f"Vote choice '{choice}' is not a valid candidate: {candidates}")
                votes.append(choice)
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
                continue

            if not self.settings.llm_enabled:
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
                continue

            summary = "\n".join(f"- {cid}: {self.agents[cid].name}, role={self.agents[cid].role}" for cid in candidates)
            voter = self.agents[voter_id]
            self._state(task.id)["current_actor"] = voter_id
            prompt = (
                f"Task: {task.prompt}\n"
                f"Candidates:\n{summary}\n"
                f"Vote for the best proposal. The choice must be one of: {', '.join(candidates)}.\n"
                f"Call the cast_vote tool with your decision."
            )

            decision = await self._run_governance_tool(
                task=task,
                actor_identity=voter,
                tool_func=cast_vote_tool,
                tool_name="cast_vote",
                schema_class=VoteDecision,
                prompt=prompt,
            )

            choice = decision.choice
            if choice not in candidates:
                raise GovernanceToolError(f"Vote choice '{choice}' is not a valid candidate: {candidates}")

            votes.append(choice)
            ballots = self._state(task.id).setdefault("ballots", [])
            if not any(ballot.get("voter") == voter_id for ballot in ballots):
                ballots.append({"voter": voter_id, "choice": choice, "reason": decision.reason, "confidence": decision.confidence})
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

        state = self._state(task.id)
        if self.settings.llm_enabled and self.settings.native_voting_enabled:
            await self._run_governance_tool(
                task=task,
                actor_identity=self.agents[team.leader_id or team.member_ids[0]],
                tool_func=tally_ballots_tool,
                tool_name="tally_ballots",
                schema_class=TallyResult,
                prompt="Tally all ballots and determine the winner. Call the tally_ballots tool.",
            )
            tally = state["tally"]
            winner = state["winner_id"]
        else:
            tally = dict(Counter(votes).most_common())
            winner = next(iter(tally))
            state["phase"] = "voted"
            state["tally"] = tally
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

    async def _monitor(self, task: TaskRun, team: Team, winner: str, proposals: dict[str, str]) -> None:
        critic_id = next((aid for aid in team.member_ids if "risk analysis" in self.agents[aid].skills), team.member_ids[-1])
        critic = self.agents[critic_id]

        if not self.settings.llm_enabled:
            critique = {"reviewed": winner, "critique": "deterministic fallback", "risks": [], "improvements": [], "confidence": 1.0}
            self._state(task.id)["critique"] = critique
            self._register_artifact(task.id, "critique", critic_id, "monitor", critique, confidence=1.0, status="final")
            self._emit(task.id, "peer_monitor_report", f"{critic.name} checked the winning solution for unsupported assumptions.", actor=critic_id, payload={"reviewed": winner, "critique": "deterministic fallback"})
            self._emit_tool_call(task.id, "peer_review", critic_id, "deterministic", {"critique": "deterministic fallback", "risks": [], "improvements": [], "confidence": 1.0}, "deterministic_no_key", True)
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

        if self.settings.llm_enabled:
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

    def _populate_acceptance_checks(self, task_id: str, team: Team) -> None:
        """Derive acceptance_checks and failed_checks from artifact/final-deliverable state."""

        state = self._state(task_id)
        checks: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        proposals = state.get("proposals", {})
        revisions = state.get("revisions", {})
        critique = state.get("critique") or {}
        final_deliverable = state.get("final_deliverable")
        artifacts = state.get("artifacts", [])

        has_proposals = len(proposals) > 0
        checks.append({"check": "proposals_exist", "passed": has_proposals, "source": "artifact_state"})
        if not has_proposals:
            failed.append({"check": "proposals_exist", "reason": "no proposals recorded"})

        has_winner = state.get("winner_id") is not None
        checks.append({"check": "winner_selected", "passed": has_winner, "source": "vote_state"})
        if not has_winner:
            failed.append({"check": "winner_selected", "reason": "no winner_id in session state"})

        has_critique = isinstance(critique, dict) and bool(critique.get("critique"))
        checks.append({"check": "critique_exists", "passed": has_critique, "source": "monitor_state"})
        if not has_critique:
            failed.append({"check": "critique_exists", "reason": "no critique recorded"})

        has_final = final_deliverable is not None
        checks.append({"check": "final_deliverable_exists", "passed": has_final, "source": "compose_state"})
        if not has_final:
            failed.append({"check": "final_deliverable_exists", "reason": "no final deliverable"})

        final_artifacts = [a for a in artifacts if a.get("type") == "final_deliverable" and a.get("status") == "final"]
        has_final_artifact = len(final_artifacts) > 0
        checks.append({"check": "final_artifact_registered", "passed": has_final_artifact, "source": "artifact_ledger"})
        if not has_final_artifact:
            failed.append({"check": "final_artifact_registered", "reason": "no final artifact in ledger"})

        revision_count = len(revisions)
        subtasks = state.get("subtasks", [])
        blocked = [s for s in subtasks if s.get("blockers")]
        if blocked:
            checks.append({"check": "no_blocked_subtasks", "passed": False, "source": "delegation_state"})
            failed.append({"check": "no_blocked_subtasks", "reason": f"{len(blocked)} subtask(s) have blockers"})
        else:
            checks.append({"check": "no_blocked_subtasks", "passed": True, "source": "delegation_state"})

        state["acceptance_checks"] = checks
        state["failed_checks"] = failed

    def _apply_validation_gate(self, task: TaskRun, team: Team) -> str:
        """Validate the final deliverable and revise it when checks fail."""

        state = self._state(task.id)
        answer = task.final_answer or ""
        checks = state.get("acceptance_checks", [])
        failed = state.get("failed_checks", [])
        passed = len(failed) == 0
        payload = {
            "passed": passed,
            "checks": checks,
            "failed_checks": failed,
        }
        self._emit(
            task.id,
            "validation_gate_completed",
            "Validation gate passed." if passed else "Validation gate found issues and revised the deliverable.",
            actor=team.leader_id,
            payload=payload,
        )
        if passed:
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
