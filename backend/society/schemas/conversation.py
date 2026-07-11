from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class GoalDiscussionStatement(BaseModel):
    round: int = Field(ge=1, description="Discussion round number")
    agent_id: str = Field(description="Agent contributing this view")
    responds_to: str | None = Field(default=None, description="Agent id this contribution responds to")
    stance: Literal["builds_on", "challenges", "clarifies", "blocks"] = Field(
        default="clarifies",
        description="How this contribution relates to the prior conversation",
    )
    interpretation: str = Field(description="What this agent thinks the user wants")
    unique_contribution: str = Field(default="", description="The non-repeated value this agent adds to the conversation")
    success_criteria: list[str] = Field(default_factory=list, description="Concrete conditions for a successful run")
    concerns: list[str] = Field(default_factory=list, description="Ambiguities, risks, or missing inputs")
    suggested_scope: str = Field(default="", description="Smallest useful scope to execute")
    question_for_next: str | None = Field(default=None, description="Question or challenge for the next agent")
    spoken_turn: str = Field(default="", description="Short meeting-room phrasing of this contribution")


class ConversationTurn(BaseModel):
    """One readable meeting-room transcript turn."""

    round: int = Field(ge=1, description="Discussion round number")
    turn_index: int = Field(ge=1, description="Turn index within the round")
    agent_id: str = Field(description="Agent speaking")
    responds_to: str | None = Field(default=None, description="Agent id this turn responds to")
    stance: Literal["builds_on", "challenges", "clarifies", "blocks"] = Field(default="clarifies")
    says: str = Field(description="Short natural-language statement for the transcript")
    quote_from_prior: str | None = Field(default=None, description="Prior point being referenced")
    question_for_next: str | None = Field(default=None, description="Question or challenge for the next speaker")


class TargetedQuestionExchange(BaseModel):
    """A bounded question-answer handoff between two named agents."""

    round: int = Field(ge=1, description="Discussion round where the question was raised")
    question_id: str = Field(description="Stable id for this exchange within the task")
    asker_id: str = Field(description="Agent who asked the question")
    target_agent_id: str = Field(description="Agent expected to answer")
    question: str = Field(description="Concrete question asked before readiness")
    answer: str = Field(description="Short answer from the target agent")
    resolved: bool = Field(default=True, description="Whether the answer is enough to proceed")
    assumption: str | None = Field(default=None, description="Assumption carried forward if not fully resolved")


class ReadinessBallot(BaseModel):
    attempt: int = Field(ge=1, description="Readiness attempt number")
    agent_id: str = Field(description="Agent casting this ballot")
    ready: bool = Field(description="Whether the agent is ready to proceed")
    critical_blocker: bool = Field(default=False, description="Whether a critical blocker exists")
    reason: str = Field(default="", description="Why the agent is or is not ready")
    required_clarification: str | None = Field(default=None, description="Clarification needed before proceeding")


class ReadinessTally(BaseModel):
    attempt: int = Field(ge=1, description="Readiness attempt number")
    ready_count: int = Field(ge=0, description="Number of agents voting ready")
    not_ready_count: int = Field(ge=0, description="Number of agents voting not ready")
    total: int = Field(ge=0, description="Total ballots cast")
    passed: bool = Field(description="Whether readiness passed the policy threshold")
    blockers: list[str] = Field(default_factory=list, description="Critical blockers preventing readiness")


class WorkingBrief(BaseModel):
    summary: str = Field(description="Shared understanding of the task")
    agreed_scope: str = Field(default="", description="What the team will do now")
    success_criteria: list[str] = Field(default_factory=list, description="Agreed success criteria")
    constraints: list[str] = Field(default_factory=list, description="Known constraints")
    open_questions: list[str] = Field(default_factory=list, description="Unresolved questions")
    blocked_items: list[str] = Field(default_factory=list, description="Items still blocked")
    unresolved_dissent: list[str] = Field(default_factory=list, description="Dissent or objections carried into execution")
    proceeding_with_dissent: bool = Field(default=False, description="Whether execution may proceed despite non-blocking dissent")
    assumptions: list[str] = Field(default_factory=list, description="Assumptions accepted so execution can proceed")
    question_answers: list[TargetedQuestionExchange] = Field(default_factory=list, description="Targeted pre-readiness Q&A exchanges")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="Team confidence in the brief")


class MeetingRecap(BaseModel):
    """Polished collaboration replay summary for the UI."""

    summary: str = Field(description="Short narrative of what happened in the meeting")
    influenced_by: list[str] = Field(default_factory=list, description="Notable influence or deferral moments")
    plan_changes: list[str] = Field(default_factory=list, description="How the plan changed during discussion")
    unresolved_dissent: list[str] = Field(default_factory=list, description="Remaining dissent visible to the user")
    saved_lessons: list[str] = Field(default_factory=list, description="Lessons or assumptions carried forward")
