export const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

export async function responseError(response: Response): Promise<Error> {
  const fallback = `Request failed with status ${response.status}.`;
  const text = await response.text();
  if (!text) return new Error(fallback);
  try {
    const parsed = JSON.parse(text);
    const detail = parsed?.detail;
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) => typeof item?.msg === "string" ? item.msg : null)
        .filter(Boolean);
      if (messages.length > 0) return new Error(messages.join(" "));
    }
    if (typeof detail === "string") return new Error(detail);
  } catch {
    // Fall through to the plain text body when the backend did not return JSON.
  }
  return new Error(text || fallback);
}

export type Agent = {
  id: string;
  name: string;
  role: string;
  skills: string[];
  profile: {
    values: string[];
    communication_style: string;
    risk_tolerance: "low" | "medium" | "high";
    decision_bias: string;
    default_blockers: string[];
    defers_to: Record<string, string[]>;
    failure_mode: string;
  };
  memory: string[];
  reputation: number;
  parent_id?: string | null;
};

export type AgentMemory = {
  task_id?: string | null;
  memory?: string | null;
  tags: string[];
  created_at?: string | null;
  mode?: string | null;
};

export type SocietyEvent = {
  id: string;
  task_id: string;
  type: string;
  message: string;
  actor?: string | null;
  payload: Record<string, unknown>;
  created_at: string;
};

export type TaskRun = {
  id: string;
  prompt: string;
  status: "queued" | "running" | "waiting_for_user" | "complete" | "failed";
  team_id?: string | null;
  final_answer?: string | null;
  created_at?: string;
  updated_at?: string;
  event_count?: number;
};

export type Health = {
  status: string;
  provider: string;
  llm_enabled: boolean;
  model: string;
};

export type EvidenceStatus = "complete" | "partial" | "missing";

export type TimelineCounts = {
  total: number;
  by_type: Record<string, number>;
};

export type AgentStance = {
  agent_id: string;
  stance: string;
  phase?: string | null;
  reason?: string | null;
  confidence?: number | null;
  source_event_id: string;
};

export type BlockerRecord = {
  source: string;
  message: string;
  agent_id?: string | null;
  required_action?: string | null;
  source_event_id: string;
};

export type ArtifactProjection = {
  id?: string | null;
  type: string;
  producer?: string | null;
  phase?: string | null;
  status?: string | null;
  content: Record<string, unknown>;
  source_event_id: string;
};

export type TeamProjection = {
  id?: string | null;
  member_ids: string[];
  leader_id?: string | null;
  status?: string | null;
};

export type ParticipantProjection = {
  agent_id: string;
  role?: string | null;
  is_leader: boolean;
  current_stance: string;
  current_action?: string | null;
  last_contribution_type?: string | null;
  last_contribution_summary?: string | null;
  status_source?: string | null;
};

export type ToolUsageProjection = {
  agent_id: string;
  tool_name: string;
  phase?: string | null;
  why_used?: string | null;
  result_summary?: string | null;
  success: boolean;
  source_event_id: string;
};

export type DelegationProjection = {
  subtask_id: string;
  assigned_by?: string | null;
  agent_id: string;
  objective: string;
  why_assigned?: string | null;
  done_criteria: string[];
  blockers: string[];
  status: string;
  result_summary?: string | null;
};

export type FailureProjection = {
  phase: string;
  blocking_reason: string;
  missing_inputs: string[];
  missing_evidence: string[];
  system_error?: string | null;
  recoverable: boolean;
};

export type WinnerRationaleProjection = {
  winner_agent_id: string;
  winning_proposal_id?: string | null;
  why_won: string;
  supporting_votes: string[];
  critical_tradeoffs: string[];
  dissent_carried: string[];
};

export type RunCockpit = {
  task_id: string;
  prompt?: string | null;
  status: string;
  current_phase: string;
  team?: TeamProjection | null;
  participants: ParticipantProjection[];
  leader_rationale?: string | null;
  latest_agent_stances: AgentStance[];
  gates: Array<Record<string, unknown>>;
  blockers: BlockerRecord[];
  artifacts: ArtifactProjection[];
  delegation_summary: DelegationProjection[];
  tool_usage_summary: ToolUsageProjection[];
  trace_status: string;
  generated_at?: string | null;
  stale_reason?: string | null;
  failure?: FailureProjection | null;
  final_answer?: string | null;
  timeline: TimelineCounts;
  evidence_status: EvidenceStatus;
  missing_sources: string[];
};

export type ProposalProjection = {
  proposal_id?: string | null;
  agent_id: string;
  proposal: string;
  rationale?: string | null;
  supersedes_proposal_id?: string | null;
  created_from_phase?: string | null;
  source_event_id: string;
};

export type ProposalOpinionProjection = {
  proposal_id: string;
  agent_id: string;
  stance: string;
  opinion: string;
  confidence?: number | null;
  phase?: string | null;
  source_event_id: string;
};

export type CritiqueProjection = {
  critic_id?: string | null;
  target?: string | null;
  critique: string;
  risks: string[];
  improvements: string[];
  source_event_id: string;
};

export type RevisionProjection = {
  agent_id: string;
  revised_proposal: string;
  changes: string[];
  source_event_id: string;
};

export type BallotProjection = {
  voter_id: string;
  choice: string;
  reason?: string | null;
  confidence?: number | null;
  source_event_id: string;
};

export type DecisionReview = {
  task_id: string;
  current_phase: string;
  proposals: ProposalProjection[];
  proposal_opinions: ProposalOpinionProjection[];
  critiques: CritiqueProjection[];
  revisions: RevisionProjection[];
  ballots: BallotProjection[];
  selected_winner?: string | null;
  selected_proposal_id?: string | null;
  winner_rationale?: WinnerRationaleProjection | null;
  blocking_objections: string[];
  non_blocking_dissent: string[];
  unresolved_dissent: string[];
  supporting_artifacts: ArtifactProjection[];
  evidence_status: EvidenceStatus;
  missing_sources: string[];
};

export type RunRecap = {
  task_id: string;
  status: string;
  duration_seconds?: number | null;
  turns: number;
  events: number;
  plan_changes: string[];
  mind_changes: Array<Record<string, unknown>>;
  dissents: string[];
  saved_lessons: string[];
  trust_reputation_changes: Array<Record<string, unknown>>;
  social_deltas: Array<Record<string, unknown>>;
  delegation_outcomes: DelegationProjection[];
  completion_outcome: string;
  failure?: FailureProjection | null;
  metrics?: Record<string, unknown> | null;
  final_answer?: string | null;
  evidence_status: EvidenceStatus;
  missing_sources: string[];
};

export type AgentDossier = {
  agent: Agent;
  task_id?: string | null;
  this_run_summary: Record<string, unknown>;
  this_run_timeline: Array<Record<string, unknown>>;
  run_lessons: string[];
  published_notes: string[];
  run_tool_usage: ToolUsageProjection[];
  run_delegation: DelegationProjection[];
  reputation: number;
  trust: Array<Record<string, unknown>>;
  behavioral_tendencies: string[];
  recent_stances: AgentStance[];
  evidence_status: EvidenceStatus;
  missing_sources: string[];
};

export async function getHealth(): Promise<Health> {
  const response = await fetch(`${API_BASE}/health`);
  if (!response.ok) throw await responseError(response);
  return response.json();
}

export async function createTask(prompt: string): Promise<TaskRun> {
  const response = await fetch(`${API_BASE}/tasks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt })
  });
  if (!response.ok) throw await responseError(response);
  return response.json();
}

export async function submitClarification(taskId: string, answer: string): Promise<TaskRun> {
  const response = await fetch(`${API_BASE}/tasks/${taskId}/clarifications`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ answer })
  });
  if (!response.ok) throw await responseError(response);
  return response.json();
}

export async function listAgents(): Promise<Agent[]> {
  const response = await fetch(`${API_BASE}/agents`);
  if (!response.ok) throw await responseError(response);
  return response.json();
}

export async function getTask(taskId: string): Promise<TaskRun> {
  const response = await fetch(`${API_BASE}/tasks/${taskId}`);
  if (!response.ok) throw await responseError(response);
  return response.json();
}

export async function listTasks(): Promise<TaskRun[]> {
  const response = await fetch(`${API_BASE}/tasks`);
  if (!response.ok) throw await responseError(response);
  return response.json();
}

export async function listTaskEvents(taskId: string): Promise<SocietyEvent[]> {
  const response = await fetch(`${API_BASE}/tasks/${taskId}/events`);
  if (!response.ok) throw await responseError(response);
  return response.json();
}

export async function listAgentMemory(agentId: string): Promise<AgentMemory[]> {
  const response = await fetch(`${API_BASE}/agents/${agentId}/memory`);
  if (!response.ok) throw await responseError(response);
  return response.json();
}

export async function getRunCockpit(taskId: string): Promise<RunCockpit> {
  const response = await fetch(`${API_BASE}/tasks/${taskId}/cockpit`);
  if (!response.ok) throw await responseError(response);
  return response.json();
}

export async function getDecisionReview(taskId: string): Promise<DecisionReview> {
  const response = await fetch(`${API_BASE}/tasks/${taskId}/review`);
  if (!response.ok) throw await responseError(response);
  return response.json();
}

export async function getRunRecap(taskId: string): Promise<RunRecap> {
  const response = await fetch(`${API_BASE}/tasks/${taskId}/recap`);
  if (!response.ok) throw await responseError(response);
  return response.json();
}

export async function getAgentDossier(agentId: string, taskId?: string | null): Promise<AgentDossier> {
  const params = taskId ? `?task_id=${encodeURIComponent(taskId)}` : "";
  const response = await fetch(`${API_BASE}/agents/${agentId}/dossier${params}`);
  if (!response.ok) throw await responseError(response);
  return response.json();
}
