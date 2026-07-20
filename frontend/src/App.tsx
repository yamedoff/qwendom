import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  BrainCircuit,
  CheckCircle2,
  ClipboardCheck,
  Clock3,
  Crown,
  FileText,
  GitBranch,
  ListChecks,
  MessagesSquare,
  Network,
  Route,
  Send,
  ShieldCheck,
  Sparkles,
  Target,
  Users,
  Vote,
  Wrench
} from "lucide-react";
import {
  API_BASE,
  Agent,
  AgentMemory,
  AgentDossier,
  DecisionReview,
  Health,
  RunCockpit,
  RunRecap,
  SocietyEvent,
  TaskArtifact,
  TaskRun,
  createTask,
  getAgentDossier,
  getDecisionReview,
  getHealth,
  getRunCockpit,
  getRunRecap,
  getTask,
  listTaskArtifacts,
  listAgentMemory,
  listAgents,
  listTaskEvents,
  listTasks,
} from "./api";
import "./core.css";
import "./styles.css";

type PageKey = "intake" | "live" | "review" | "recap" | "artifacts" | "dossier" | "benchmark" | "architecture";

const PAGE_KEYS: PageKey[] = ["intake", "live", "review", "recap", "artifacts", "dossier", "benchmark", "architecture"];
const PAGE_ALIASES: Record<string, PageKey> = {
  dossiers: "dossier",
};
const MIN_PROMPT_LENGTH = 8;

function validateMissionPrompt(value: string): string | null {
  if (value.trim().length >= MIN_PROMPT_LENGTH) return null;
  return "Add a short mission before convening the society.";
}

/**
 * Backend treats both "complete" and "complete_with_warnings" as terminal
 * completion. The latter finished successfully but emitted non-fatal warnings.
 * Before this helper the UI only recognised "complete", so a warnings-bearing
 * completion was misclassified as failed/partial and left polling/SSE active.
 */
const COMPLETED_STATUSES: ReadonlySet<string> = new Set(["complete", "complete_with_warnings"]);
const isCompletedStatus = (status: string | null | undefined): boolean =>
  status != null && COMPLETED_STATUSES.has(status);
const TERMINAL_STATUSES: ReadonlySet<string> = new Set([...COMPLETED_STATUSES, "failed", "interrupted", "remediation"]);
const isTerminalStatus = (status: string | null | undefined): boolean =>
  status != null && TERMINAL_STATUSES.has(status);
const TERMINAL_EVENT_TYPES: ReadonlySet<string> = new Set(["task_complete", "task_failed", "task_interrupted", "task_remediation"]);

function dialRunState(status: string | null | undefined): string {
  if (status === "running") return "live";
  if (status === "queued") return "convening";
  if (status === "waiting_for_user" || status === "interrupted") return "paused";
  if (status === "remediation") return "remediation";
  if (status === "failed") return "stopped";
  if (isCompletedStatus(status)) return "complete";
  return "inactive";
}

function runErrorMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  if (/fetch|failed to fetch|network/i.test(message)) {
    return "Backend is offline or unreachable. Start the backend, then try the run again. No local mock run was created.";
  }
  return `The backend rejected the run: ${message}`;
}

function useHashPage(): [PageKey, (key: PageKey) => void] {
  const read = (): PageKey => {
    const h = window.location.hash.replace("#", "");
    if ((PAGE_KEYS as string[]).includes(h)) return h as PageKey;
    return PAGE_ALIASES[h] ?? "intake";
  };
  const [page, setPageState] = useState<PageKey>(read);
  useEffect(() => {
    const onHash = () => setPageState(read());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const setPage = useCallback((key: PageKey) => {
    window.location.hash = key;
    setPageState(key);
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  }, []);
  return [page, setPage];
}

const roleToolBundles: Record<string, { primary: string; support: string[] }> = {
  architect: { primary: "decompose_task", support: ["state_position", "endorse_agent", "record_private_note"] },
  researcher: { primary: "memory_lookup", support: ["record_private_note", "publish_private_note", "state_position"] },
  builder: { primary: "implementation_plan", support: ["state_position", "change_mind", "record_private_note"] },
  critic: { primary: "risk_assessment", support: ["register_objection", "evaluate_peer", "publish_private_note"] }
};

const roleKeyOf = (agent: Agent): keyof typeof roleToolBundles => {
  const text = `${agent.id} ${agent.role} ${agent.skills.join(" ")}`.toLowerCase();
  if (text.includes("research") || text.includes("evidence") || text.includes("memory")) return "researcher";
  if (text.includes("risk") || text.includes("critic") || text.includes("review") || text.includes("validation")) return "critic";
  if (text.includes("architect") || text.includes("system") || text.includes("decompose")) return "architect";
  return "builder";
};

const eventIcon: Record<string, ReactNode> = {
  leader_elected: <Crown size={18} />,
  leader_election_started: <Crown size={18} />,
  vote_cast: <Vote size={18} />,
  ballots_tallied: <Vote size={18} />,
  solution_selected: <Vote size={18} />,
  child_agent_spawned: <GitBranch size={18} />,
  no_spawn: <GitBranch size={18} />,
  proposal_challenged: <GitBranch size={18} />,
  proposal_revised: <GitBranch size={18} />,
  debate_round_completed: <GitBranch size={18} />,
  tool_call: <Wrench size={18} />,
  workflow_checkpoint: <Clock3 size={18} />,
  workflow_completed: <Clock3 size={18} />,
  agno_team_ran: <BrainCircuit size={18} />,
  peer_monitor_report: <ShieldCheck size={18} />,
  learning_recorded: <BrainCircuit size={18} />,
  reputation_updated: <ShieldCheck size={18} />,
  task_metrics: <Wrench size={18} />,
  validation_gate_completed: <ClipboardCheck size={18} />,
  task_complete: <Sparkles size={18} />,
  goal_discussion_started: <MessagesSquare size={18} />,
  conversation_turn: <MessagesSquare size={18} />,
  agent_goal_opinion: <MessagesSquare size={18} />,
  targeted_question_answered: <MessagesSquare size={18} />,
  readiness_vote_cast: <ClipboardCheck size={18} />,
  readiness_vote_tallied: <ClipboardCheck size={18} />,
  working_brief_finalized: <FileText size={18} />,
  subtasks_assigned_from_brief: <ListChecks size={18} />,
  agent_position_stated: <Target size={18} />,
  agent_objection_registered: <ShieldCheck size={18} />,
  agent_endorsed_peer: <Users size={18} />,
  agent_changed_mind: <Route size={18} />,
  private_note_published: <FileText size={18} />,
  agent_tool_bundle_selected: <Wrench size={18} />,
  agent_help_requested: <Users size={18} />,
  agent_deferred_ownership: <Route size={18} />,
  agent_joined_coalition: <Users size={18} />,
  trust_updated: <ShieldCheck size={18} />,
  meeting_recap: <FileText size={18} />,
  artifact_section_critiqued: <FileText size={18} />,
  shared_artifact_revised: <FileText size={18} />,
  personality_drifted: <BrainCircuit size={18} />,
  failure_recovery_attempted: <ShieldCheck size={18} />,
  user_clarification_requested: <MessagesSquare size={18} />,
  user_clarification_answered: <MessagesSquare size={18} />,
  society_resumed: <Route size={18} />
};

type PhaseKey =
  | "setup"
  | "goal_discussion"
  | "readiness_vote"
  | "working_brief"
  | "leader_election"
  | "subtask_assignment"
  | "work_review"
  | "final_answer";

const phaseOrder: { key: PhaseKey; label: string; icon: ReactNode }[] = [
  { key: "setup", label: "Setup / Team Formation", icon: <Users size={16} /> },
  { key: "goal_discussion", label: "Goal Discussion", icon: <MessagesSquare size={16} /> },
  { key: "readiness_vote", label: "Readiness Vote", icon: <ClipboardCheck size={16} /> },
  { key: "working_brief", label: "Working Brief", icon: <FileText size={16} /> },
  { key: "leader_election", label: "Leader Election", icon: <Crown size={16} /> },
  { key: "subtask_assignment", label: "Subtask Assignment", icon: <ListChecks size={16} /> },
  { key: "work_review", label: "Work / Review", icon: <Wrench size={16} /> },
  { key: "final_answer", label: "Final Answer", icon: <Sparkles size={16} /> }
];

const societyWorkflow: { phase: PhaseKey; title: string; outcome: string; icon: ReactNode }[] = [
  { phase: "setup", title: "Constitute", outcome: "Create a temporary task society with durable identities.", icon: <Users size={17} /> },
  { phase: "goal_discussion", title: "Align", outcome: "Each role interprets the goal and surfaces risks or ambiguity.", icon: <MessagesSquare size={17} /> },
  { phase: "readiness_vote", title: "Gate", outcome: "Agents vote whether the society has enough clarity to execute.", icon: <ClipboardCheck size={17} /> },
  { phase: "working_brief", title: "Brief", outcome: "Freeze scope, success criteria, constraints, and open questions.", icon: <FileText size={17} /> },
  { phase: "leader_election", title: "Lead", outcome: "Elect the coordinator for this task, not a permanent ruler.", icon: <Crown size={17} /> },
  { phase: "subtask_assignment", title: "Divide", outcome: "Turn the brief into owned subtasks with done criteria.", icon: <ListChecks size={17} /> },
  { phase: "work_review", title: "Negotiate", outcome: "Propose, challenge, revise, vote, and monitor the selected path.", icon: <GitBranch size={17} /> },
  { phase: "final_answer", title: "Synthesize", outcome: "Return a traceable answer and update memory/reputation.", icon: <Sparkles size={17} /> }
];

const phaseOf = (type: string, payload: Record<string, unknown> = {}): PhaseKey => {
  if (type === "tool_call") {
    const toolName = asString(payload.tool_name);
    if (toolName === "submit_goal_discussion") return "goal_discussion";
    if (toolName === "cast_readiness_vote") return "readiness_vote";
    if (toolName === "elect_leader" || toolName === "decide_spawn") return "leader_election";
    if (toolName === "assign_subtask" || toolName === "report_subtask") return "subtask_assignment";
  }
  if (type === "task_failed") {
    const phase = asString(payload.phase);
    if (phase === "forming" || phase === "formed") return "setup";
    if (phase === "pre_execution_conversation") return "goal_discussion";
    if (phase === "leader_elected") return "leader_election";
    if (phase === "debating") return "work_review";
  }
  switch (type) {
    case "agent_position_stated": {
      const socialPhase = asString(payload.phase);
      if (socialPhase === "vote") return "work_review";
      if (socialPhase === "readiness_vote") return "readiness_vote";
      if (socialPhase === "working_brief") return "working_brief";
      if (socialPhase === "leader_election") return "leader_election";
      if (socialPhase === "final_answer" || socialPhase === "learning") return "final_answer";
      return "goal_discussion";
    }
    case "agent_objection_registered": {
      const blocks = asBoolean(payload.blocks_execution);
      const target = asString(payload.target);
      if (blocks || target === null) return "readiness_vote";
      return "work_review";
    }
    case "goal_discussion_started":
    case "conversation_turn":
    case "agent_goal_opinion":
    case "targeted_question_answered":
      return "goal_discussion";
    case "readiness_vote_cast":
    case "readiness_vote_tallied":
    case "user_clarification_requested":
    case "user_clarification_answered":
    case "society_resumed":
      return "readiness_vote";
    case "working_brief_finalized":
    case "meeting_recap":
    case "private_note_published":
      return "working_brief";
    case "leader_election_started":
    case "leader_elected":
    case "agent_endorsed_peer":
      return "leader_election";
    case "subtasks_assigned_from_brief":
      return "subtask_assignment";
    case "specialist_selection_proposed":
    case "specialist_selection_rejected":
    case "specialist_selection_accepted":
    case "specialist_invocation_approved":
    case "specialist_invocation_started":
    case "composition_assignment_materialized":
    case "work_node_started":
    case "work_node_blocked":
    case "work_node_failed":
    case "work_node_retry_scheduled":
    case "work_node_canceled":
    case "work_node_completed":
    case "composition_assignment_cleanup_completed":
    case "composition_assignment_cleanup_warning":
    case "artifact_validated":
      return "work_review";
    case "agent_tool_bundle_selected":
    case "artifact_section_critiqued":
    case "shared_artifact_revised":
      return "work_review";
    case "agent_help_requested":
      return "subtask_assignment";
    case "agent_deferred_ownership":
      return "leader_election";
    case "agent_joined_coalition":
      return "work_review";
    case "agent_changed_mind":
      return "work_review";
    case "task_complete":
    case "task_failed":
    case "team_dissolved":
    case "trust_updated":
    case "personality_drifted":
    case "failure_recovery_attempted":
      return "final_answer";
    case "task_received":
    case "team_formed":
    case "error":
      return "setup";
    default:
      return "work_review";
  }
};

const phaseConfig = (phase: PhaseKey) =>
  phaseOrder.find((item) => item.key === phase) ?? phaseOrder[phaseOrder.length - 1];

const internalTimelineEvents = new Set([
  "workflow_checkpoint",
  "workflow_completed",
  "task_metrics",
  "reputation_updated",
  "learning_recorded",
  "team_dissolved"
]);

const isTimelineEvent = (event: SocietyEvent) => !internalTimelineEvents.has(event.type);

interface PhaseGroup {
  phase: PhaseKey;
  events: SocietyEvent[];
}

const mergeEvents = (current: SocietyEvent[], incoming: SocietyEvent[]): SocietyEvent[] => {
  const byId = new Map<string, SocietyEvent>();
  for (const e of current) byId.set(e.id, e);
  for (const e of incoming) byId.set(e.id, e);
  return [...byId.values()].sort((a, b) => (Date.parse(a.created_at) || 0) - (Date.parse(b.created_at) || 0));
};

const asString = (value: unknown): string | null =>
  typeof value === "string" && value.length > 0 ? value : null;
const asStringArray = (value: unknown): string[] =>
  Array.isArray(value) ? value.map((item) => String(item)).filter((item) => item.length > 0) : [];
const asNumber = (value: unknown): number | null => (typeof value === "number" && Number.isFinite(value) ? value : null);
const asBoolean = (value: unknown): boolean | null => (typeof value === "boolean" ? value : null);
const asRecord = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;

/** Prefer the public roster identity wherever an event only carries an internal ID. */
function displayIdentity(id: string | null | undefined, agents: Agent[], fallback = "Society member"): string {
  if (!id) return fallback;
  const agent = agents.find((item) => item.id === id);
  return agent ? `${agent.name} — ${agent.role}` : fallback;
}

/** Turn small projection records into readable evidence without inventing a result. */
function readableProjection(item: Record<string, unknown>): string {
  const subject = asString(item.agent_name) ?? asString(item.actor_name) ?? asString(item.agent_id) ?? "A society member";
  const change = asString(item.summary) ?? asString(item.reason) ?? asString(item.change) ?? asString(item.message);
  const before = asString(item.before) ?? asString(item.previous_value);
  const after = asString(item.after) ?? asString(item.new_value) ?? asString(item.value);
  if (change) return `${subject}: ${change}`;
  if (before && after) return `${subject}: ${before} → ${after}`;
  return "A recorded change is available in the event evidence.";
}

const formatDeferrals = (defersTo: Record<string, string[]>): string[] =>
  Object.entries(defersTo).map(([agentId, domains]) => `${agentId}: ${domains.join(", ")}`);

type Stance = "aligned" | "dissent" | "blocking" | "changed" | "neutral";

const stanceColors: Record<Stance, string> = {
  aligned: "#4E9A6F",
  dissent: "#C07C33",
  blocking: "#CE5F4E",
  changed: "#64809A",
  neutral: "#9A8E7C"
};

function StringList({ items, className }: { items: string[]; className?: string }) {
  if (items.length === 0) return null;
  return (
    <ul className={className}>
      {items.map((item, index) => (
        <li key={index}>{item}</li>
      ))}
    </ul>
  );
}

function ParsedMemory({ memory }: { memory?: string | null }) {
  if (!memory) return null;
  try {
    const parsed = JSON.parse(memory) as Record<string, unknown>;
    const lesson = asString(parsed.lesson) ?? memory;
    const category = asString(parsed.category);
    const socialLessons = Array.isArray(parsed.social_lessons)
      ? parsed.social_lessons
          .map((item) => asRecord(item))
          .filter((item): item is Record<string, unknown> => item !== null)
      : [];
    const socialTrace = asRecord(parsed.social_trace);
    return (
      <div className="memoryParsed">
        {category && <span className="badge">{category}</span>}
        <p>{lesson}</p>
        {socialLessons.length > 0 && (
          <div className="memoryLessons">
            {socialLessons.slice(0, 3).map((item, index) => (
              <div className="memoryLesson" key={index}>
                {asString(item.signal) && <strong>{asString(item.signal)?.replaceAll("_", " ")}</strong>}
                {asString(item.lesson) && <p>{asString(item.lesson)}</p>}
                {asString(item.next_time) && <em>{asString(item.next_time)}</em>}
              </div>
            ))}
          </div>
        )}
        {socialTrace && (
          <div className="capabilities">
            {Object.entries(socialTrace)
              .filter(([, value]) => typeof value === "number" && value > 0)
              .slice(0, 5)
              .map(([key, value]) => <span key={key}>{key}: {String(value)}</span>)}
          </div>
        )}
      </div>
    );
  } catch {
    return <p>{memory}</p>;
  }
}

function Explainer({
  id,
  title,
  text,
  openId,
  setOpenId,
  children
}: {
  id: string;
  title: string;
  text: string;
  openId: string | null;
  setOpenId: (id: string | null) => void;
  children: ReactNode;
}) {
  const isOpen = openId === id;
  return (
    <div className={`has-exp ${isOpen ? "exp-open" : ""}`}>
      {children}
      <button
        type="button"
        className="exp-btn"
        aria-label={isOpen ? "Hide explanation" : "Show explanation"}
        aria-expanded={isOpen}
        aria-controls={`${id}-explainer`}
        onClick={(event) => {
          event.stopPropagation();
          setOpenId(isOpen ? null : id);
        }}
      >
        i
      </button>
      <aside className="exp-pop" id={`${id}-explainer`} role="note" aria-live="polite" aria-hidden={!isOpen}>
        <strong>{title}</strong>
        <p>{text}</p>
      </aside>
    </div>
  );
}

function PhaseStrip({
  currentPhase,
  completedPhases
}: {
  currentPhase: PhaseKey | null;
  completedPhases: Set<PhaseKey>;
}) {
  return (
    <div className="phaseStrip" aria-label="Society phase progress">
      {societyWorkflow.map((stage, index) => {
        const isCurrent = currentPhase === stage.phase;
        const isComplete = completedPhases.has(stage.phase) && !isCurrent;
        return (
          <span
            className={`phaseDot ${isCurrent ? "cur" : ""} ${isComplete ? "passed" : ""}`}
            key={stage.phase}
            style={{ "--phase-index": index } as React.CSSProperties}
            title={stage.title}
            aria-label={stage.title}
            aria-current={isCurrent ? "step" : undefined}
          >
            {isCurrent ? stage.title : index + 1}
          </span>
        );
      })}
    </div>
  );
}

function StanceDial({ stance, strength, tick }: { stance: Stance; strength: number; tick: number | null }) {
  const radius = 21;
  const circumference = 2 * Math.PI * radius;
  const normalizedStrength = Math.max(0, Math.min(1, strength));
  const normalizedTick = tick === null ? null : Math.max(0, Math.min(1, tick));
  const tickAngle = normalizedTick === null ? 0 : (normalizedTick * 360 - 90) * Math.PI / 180;
  const tickStart = normalizedTick === null ? null : {
    x: 24 + Math.cos(tickAngle) * 16.5,
    y: 24 + Math.sin(tickAngle) * 16.5
  };
  const tickEnd = normalizedTick === null ? null : {
    x: 24 + Math.cos(tickAngle) * 25.5,
    y: 24 + Math.sin(tickAngle) * 25.5
  };

  return (
    <svg className="dial-svg" viewBox="0 0 48 48" aria-label={`${stance} stance at ${Math.round(normalizedStrength * 100)} percent strength`}>
      <circle className="dial-track" cx="24" cy="24" r={radius} aria-hidden="true" />
      <circle
        className="dial-arc"
        cx="24"
        cy="24"
        r={radius}
        stroke={stanceColors[stance]}
        strokeDasharray={`${normalizedStrength * circumference} ${circumference}`}
        aria-hidden="true"
      />
      {tickStart && tickEnd && (
        <line
          className="dial-tick"
          x1={tickStart.x}
          y1={tickStart.y}
          x2={tickEnd.x}
          y2={tickEnd.y}
          aria-hidden="true"
        />
      )}
    </svg>
  );
}

const stanceForAgent = (agent: Agent, cockpit?: RunCockpit | null): Stance => {
  const projected = cockpit?.latest_agent_stances.find((stance) => stance.agent_id === agent.id)?.stance.toLowerCase();
  if (projected === "block" || projected === "blocking") return "blocking";
  if (projected === "oppose" || projected === "dissent" || projected === "challenges") return "dissent";
  if (projected === "changed" || projected === "changed_mind") return "changed";
  if (projected === "support" || projected === "aligned" || projected === "builds_on") return "aligned";
  return "neutral";
};

const officeCodes = ["st", "bl", "cr", "rk", "ar"] as const;
type OfficeCode = typeof officeCodes[number];

const officeForIndex = (index: number): OfficeCode => officeCodes[index % officeCodes.length];

function Rail({ status, health, currentPage, onNavigate, onTour }: { status: string; health: Health | null; currentPage: PageKey; onNavigate: (key: PageKey) => void; onTour: () => void }) {
  const navItems: { key: PageKey; label: string }[] = [
    { key: "intake", label: "Intake" },
    { key: "live", label: "Live run" },
    { key: "review", label: "Review" },
    { key: "recap", label: "Recap" },
    { key: "artifacts", label: "Artifacts" },
    { key: "dossier", label: "Dossier" },
    { key: "benchmark", label: "Benchmark" },
    { key: "architecture", label: "Architecture" }
  ];
  return (
    <>
      <aside className="rail">
        <div className="glyph"></div>
        <div className="wordmark">Quendom</div>
        <div className="wordsub">WORKING SOCIETY</div>
        <nav className="railnav" aria-label="Screens">
          {navItems.map(({ key, label }) => (
            <a
              data-nav={key}
              aria-current={key === currentPage ? "page" : undefined}
              href={`#${key}`}
              key={key}
              onClick={(e) => { e.preventDefault(); onNavigate(key); }}
            >
              {label}
            </a>
          ))}
        </nav>
        <div className="rail-bottom">
          <span className="rail-live"><span className="pulse"></span>{status.toUpperCase()}</span><br />
          {health ? `${health.provider} · ${health.llm_enabled ? health.model : "fallback"}` : "LOCAL SESSION"}<br />
          <button className="tourbtn" id="tour" type="button" onClick={onTour}>▸&nbsp;&nbsp;TOUR</button>
        </div>
      </aside>
      <div className="mobilebar">
        <div><div className="wordmark">Quendom</div></div>
        <span className="rail-live"><span className="pulse"></span>{status.toUpperCase()}</span>
      </div>
    </>
  );
}

function DotStrip({ currentPhaseIndex, cockpit }: { currentPhaseIndex: number; cockpit?: RunCockpit | null }) {
  const total = 48;
  const current = Math.max(0, Math.min(total - 1, Math.round(((currentPhaseIndex + 1) / societyWorkflow.length) * total) - 1));
  const gates: Record<number, "passed" | "open"> = {};
  if (cockpit?.gates.length) {
    cockpit.gates.forEach((gate, index) => {
      gates[Math.min(total - 1, Math.max(0, Math.round(((index + 1) / cockpit.gates.length) * total) - 1))] = "passed";
    });
  }
  return (
    <div
      className="dotstrip"
      data-explain-title="Gate-aware phase strip"
      data-explain-text="48 dots of elapsed work, darkening copper as the run progresses. Ringed dots are governance gates."
    >
      <div className="dots" aria-label="Run progress">
        {Array.from({ length: total }, (_, index) => {
          const gate = gates[index];
          const style = !gate && index <= current
            ? { background: `rgb(${Math.round(242 + (122 - 242) * (index / Math.max(1, current)))},${Math.round(224 + (62 - 224) * (index / Math.max(1, current)))},${Math.round(198 + (198 - 198) * (index / Math.max(1, current)))})` }
            : undefined;
          return <i className={`${gate ? `gate ${gate}` : ""} ${index === current ? "cur" : ""}`} style={style} key={index} />;
        })}
      </div>
      <div className="strip-badge" style={{ left: `${((current + 0.5) / total) * 100}%` }}>{societyWorkflow[Math.max(0, currentPhaseIndex)]?.title ?? "Intake"}</div>
      <div className="strip-labels"><span>{cockpit?.current_phase ?? "INTAKE"}</span><span className="g">{cockpit?.gates.length ? `${cockpit.gates.length} GATE EVENT(S)` : "NO GATE EVIDENCE"}</span><span>{cockpit?.evidence_status ?? "WAITING"}</span></div>
    </div>
  );
}

function OfficeDial({ agent, index, cockpit, runStatus }: { agent: Agent; index: number; cockpit?: RunCockpit | null; runStatus?: TaskRun["status"] }) {
  const stance = stanceForAgent(agent, cockpit);
  const office = officeForIndex(index);
  const isLeader = cockpit?.participants.some((p) => p.agent_id === agent.id && p.is_leader) ?? false;
  return (
    <div className="dial" tabIndex={0}>
      <div className="dial-ring">
        <StanceDial
          stance={stance}
          strength={Math.min(1, Math.max(0.22, agent.reputation / 3))}
          tick={agent.profile.risk_tolerance === "low" ? 0.2 : agent.profile.risk_tolerance === "medium" ? 0.55 : 0.82}
        />
        <span className={`orb ${office}`}></span>
      </div>
      <div className="name">{agent.name}</div>
      <div className="dial-role">{agent.role}</div>
      {isLeader && <div className="lead">LEAD</div>}
      <span className={`chip ${stance}`}>{stance.toUpperCase()}</span><span className="since">{dialRunState(runStatus)}</span>
      <div className="dial-hist">
        <div className="dh-label">STANCE HISTORY</div>
        <div className="dh-row"><span>{agent.profile.communication_style}</span><span className="t">now</span></div>
        <div className="dh-row"><span>{agent.profile.risk_tolerance} risk</span><span className="t">profile</span></div>
      </div>
    </div>
  );
}

type LedgerTurnKind = "support" | "propose" | "challenge" | "block" | "revision" | "direct";

function typedTurnClass(event: SocietyEvent): LedgerTurnKind {
  if (event.type === "agent_objection_registered") return asBoolean(event.payload.blocks_execution) ? "block" : "challenge";
  if (event.type === "agent_changed_mind") return "revision";
  if (event.type === "agent_position_stated") {
    const stance = asString(event.payload.stance);
    if (stance?.includes("challenge")) return "challenge";
    if (stance?.includes("block")) return "block";
    if (stance?.includes("oppose")) return "challenge";
    if (stance?.includes("support") || stance?.includes("aligned") || stance?.includes("builds_on")) return "support";
    return "propose";
  }
  if (event.type === "conversation_turn") return "direct";
  return "propose";
}

function humanEventLabel(type: string): string {
  const labels: Record<string, string> = {
    agent_position_stated: "POSITION",
    agent_objection_registered: "BLOCKER",
    agent_changed_mind: "REVISED VIEW",
    targeted_question_answered: "EVIDENCE RETURNED",
    subtasks_assigned_from_brief: "WORK DELEGATED",
    delegation_assigned: "WORK DELEGATED",
    leader_elected: "LEADER CHOSEN",
    solution_selected: "DECISION",
    shared_artifact_revised: "ARTIFACT REVISED",
    meeting_recap: "HANDOFF"
  };
  return labels[type] ?? type.replaceAll("_", " ").toUpperCase();
}

/** Only expose a canonical constraint ID; actor IDs are implementation detail. */
function blockerLabel(value: string | null): string | null {
  return value && /^C-[A-Za-z0-9_-]+$/.test(value) ? value : null;
}

/** Keep event freshness readable without exposing unstable raw second counts. */
function elapsedLabel(seconds: number): string {
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function TypedTurn({ event, index, agents }: { event: SocietyEvent; index: number; agents: Agent[] }) {
  const turnType = typedTurnClass(event);
  const actor = event.actor ?? asString(event.payload.agent_id) ?? asString(event.payload.actor) ?? `office-${index + 1}`;
  const agent = agents.find((item) => item.id === actor);
  const stance = asString(event.payload.stance)?.toLowerCase();
  const office = officeForIndex(index);
  const reason = asString(event.payload.reason) ?? asString(event.payload.critique) ?? asString(event.payload.says);
  const delegateTargets = asRecord(event.payload.delegates) ?? asRecord(event.payload.delegation);
  const constraintId = blockerLabel(asString(event.payload.constraint_id));
  const auditSummary = turnType === "block"
    ? "Blocker record · execution pauses until its condition is resolved"
    : turnType === "revision"
      ? "Revision record · the agent changed its position"
      : turnType === "challenge"
        ? "Challenge record · the agent questioned the current assumption"
        : turnType === "support"
          ? "Support record · the agent backed the current path"
          : "Decision update · the agent proposed a path";
  return (
    <div className={`turn ${turnType}`}>
      <div className="turn-top">
        <span className={`orb ${office}`}></span>
        <span className="name">{agent?.name ?? actor}</span>
        <span className="role">{agent?.role ?? humanEventLabel(event.type)}</span>
        <span className={`ttag ${turnType}`}>{turnType.toUpperCase()}</span>
        <span className="time">{new Date(event.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
      </div>
      <div className="tx">
        {event.type === "agent_position_stated" && stance
          ? `${agent?.name ?? actor} ${stance === "oppose" ? "challenges" : stance === "block" ? "blocks" : stance === "support" ? "supports" : "frames"} the current path.`
          : event.type === "agent_objection_registered"
            ? `${agent?.name ?? actor} blocks the current path until the condition below is met.`
            : event.message}
      </div>
      {turnType === "direct" && delegateTargets && (
        <div className="t-delegate">
          {(delegateTargets
            ? Object.entries(delegateTargets).slice(0, 4)
            : agents.slice(1, 4).map((a, i) => [officeForIndex(i + 1), `${a.name} · task`])
          ).map(([target, desc], i) => (
            <div className="dr" key={i}>
              <span className={`orb ${target}`}></span>
              <span><b>{asString(desc)?.split(" · ")[0] ?? target}</b>{desc ? ` · ${String(desc).split(" · ").slice(1).join(" · ")}` : ""}</span>
              <span>t-{String(i + 4).padStart(2, "0")}</span>
            </div>
          ))}
        </div>
      )}
      {(reason || turnType === "block" || turnType === "revision") && (
        <details className="ledger-audit-row">
          <summary>{auditSummary}</summary>
          {reason && turnType !== "direct" && <div className="t-anchor">↳ {reason}</div>}
          {turnType === "block" && (
            <>
          <div className="t-struct t-violation"><span className="lbl">GOVERNANCE GATE</span>{constraintId && <span className="cid">{constraintId}</span>}<span className="t-struct-copy">{asString(event.payload.target) ?? "Execution path blocked until risk is resolved."}</span></div>
          <div className="t-struct t-lift"><span className="lbl">CONDITION TO LIFT</span>{asString(event.payload.resolution_condition) ?? asString(event.payload.condition) ?? "A responsible office must revise the proposal and satisfy the blocker."}</div>
            </>
          )}
          {turnType === "revision" && (
            <div className="t-struct t-flip"><span className="lbl">WHAT CHANGED</span><span className="from">{asString(event.payload.previous_position) ?? "Previous stance"}</span><span className="arr">→</span><span className="to">{asString(event.payload.new_position) ?? "Updated stance"}</span></div>
          )}
        </details>
      )}
    </div>
  );
}

function LivingBriefCard({ brief, prompt }: { brief: Record<string, unknown> | null; prompt: string }) {
  const scope = asString(brief?.agreed_scope) ?? asString(brief?.summary) ?? prompt;
  const constraints = asStringArray(brief?.constraints);
  const success = asStringArray(brief?.success_criteria);
  return (
    <div className="card" data-explain-title="Living brief" data-explain-text="The shared objective with per-line version blame. Copper blame marks lines edited this session.">
      <div className="card-head">
        <span className="card-title"><span className="ic">≡</span>WORKING BRIEF</span>
        <span className="right"><span className="fresh"></span><span className="ver">{brief ? "live" : "draft"}</span></span>
      </div>
      <div className="card-body">
        <p className="brief-obj">{scope}</p>
        {constraints.length > 0 && (
          <>
            <div className="kv">CONSTRAINTS</div>
            {constraints.map((item, index) => (
              <div className="b-line" key={index}>
                <span className="cid">C-{index + 1}</span>
                <span className="bt">{item}</span>
                <span className="blame"><b>t-{String(index + 2).padStart(2, "0")}</b></span>
              </div>
            ))}
          </>
        )}
        {success.length > 0 && (
          <>
            <div className="kv">SUCCESS CRITERIA</div>
            {success.map((item, index) => (
              <div className="b-line" key={index}>
                <span className="box"></span>
                <span className="bt">{item}</span>
                <span className="blame"><b>t-{String(index + 5).padStart(2, "0")}</b></span>
              </div>
            ))}
          </>
        )}
        {constraints.length === 0 && success.length === 0 && (
          <p className="hint">Constraints and criteria will appear as the brief is finalized.</p>
        )}
      </div>
    </div>
  );
}

function delegationStatus(item: Record<string, unknown>, _index: number): { status: string; label: string; dep: string | null; depClass: string } {
  const status = asString(item.status);
  const dependsOn = asString(item.depends_on) ?? asString(item.waits_on);
  const holding = asString(item.holding) ?? asString(item.blocks);
  const feeds = asString(item.feeds) ?? asString(item.output_to);
  if (status === "done" || status === "complete") return { status: "done", label: "DONE", dep: feeds ? `→ ${feeds}` : null, depClass: "dep" };
  if (status === "gating" || holding) return { status: "gating", label: `GATING ${holding ? holding.charAt(0).toUpperCase() : ""}`, dep: holding ? `⊘ holding → ${holding}` : null, depClass: "dep holds" };
  if (dependsOn) return { status: "progress", label: "IN PROGRESS", dep: `⧗ waiting on → ${dependsOn}`, depClass: "dep waits" };
  if (feeds) return { status: "progress", label: "IN PROGRESS", dep: `→ ${feeds}`, depClass: "dep" };
  return { status: "unknown", label: "UNKNOWN", dep: null, depClass: "" };
}

function DelegationCard({ assignments }: { assignments: Record<string, unknown> | null }) {
  const items = Array.isArray(assignments?.assignments)
    ? assignments.assignments
    : Array.isArray(assignments?.subtasks)
      ? assignments.subtasks
      : [];
  return (
    <div className="card" data-explain-title="Ownership tokens" data-explain-text="Who owns what, with dependency hints: each subtask shows what it is waiting on or holding up, so a gate reads as a chain — not a label. The decision memo waits on the constraint audit, which is holding Proposal B.">
      <div className="card-head">
        <span className="card-title"><span className="ic">⊙</span>DELEGATION</span>
        <span className="right"><span className="ver">{items.length} subtasks</span></span>
      </div>
      <div style={{ paddingTop: 12 }}>
        {items.length === 0 && (
          <div className="empty-state">No delegation events have been emitted for this run.</div>
        )}
        {items.map((item: unknown, index: number) => {
          const record = (item ?? {}) as Record<string, unknown>;
          const owner = asString(record.owner) ?? asString(record.agent_id) ?? `office-${index + 1}`;
          const objective = asString(record.objective) ?? asString(record.task) ?? `Subtask ${index + 1}`;
          const officeCode = officeForIndex(index);
          const { label, dep, depClass } = delegationStatus(record, index);
          return (
            <div className="drow" key={index}>
              <span className={`orb ${officeCode}`}></span>
              <span className="task">
                <span className="t">{objective}</span>
                <span className="o">{owner}</span>
                {dep && <span className={depClass}>{dep}</span>}
              </span>
              <span className={`pillstat ${label === "DONE" ? "done" : label.startsWith("GATING") ? "gating" : "progress"}`}>{label}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function artifactStanding(index: number, total: number): { standing: string; className: string; crit: number | null; blk: number | null } {
  if (total <= 1) return { standing: "DRAFTING", className: "drafting", crit: null, blk: null };
  if (index === 0) return { standing: "LEADING", className: "leading", crit: 2, blk: null };
  if (index === total - 1) return { standing: "DRAFTING", className: "drafting", crit: null, blk: null };
  return { standing: "CHALLENGED", className: "challenged", crit: 1, blk: 1 };
}

function ArtifactsCard({ events }: { events: SocietyEvent[] }) {
  const artifactEvents = events.filter((event) =>
    ["artifact_section_critiqued", "shared_artifact_revised", "working_brief_finalized", "meeting_recap"].includes(event.type)
  );
  const artifacts = artifactEvents.length > 0
    ? artifactEvents.slice(-3).map((event, index) => ({
        id: asString(event.payload.artifact_id) ?? `artifact-${index}`,
        name: asString(event.payload.section) ?? event.type.replaceAll("_", " "),
        type: event.type === "working_brief_finalized" ? "brief" : event.type === "meeting_recap" ? "recap" : "proposal",
        revision: index,
      }))
    : [];
  return (
    <div className="card" data-explain-title="Artifact passports" data-explain-text="One glanceable unit per artifact, reused in the feed, this tray, and review: type icon, revision sparkline, open-critique counter, blocks, and owner. Standing (leading / challenged / drafting) is the society's current read.">
      <div className="card-head">
        <span className="card-title"><span className="ic">▤</span>ARTIFACTS</span>
        <span className="right"><span className="ver">{artifacts.length} in play</span></span>
      </div>
      <div style={{ paddingTop: 12 }}>
        {artifacts.length === 0 && <div className="empty-state">No artifact events have been emitted for this run.</div>}
        {artifacts.map((artifact, index) => {
          const { standing, className, crit, blk } = artifactStanding(index, artifacts.length);
          return (
            <div className="a-item" key={artifact.id}>
              <div className="passport flat">
                <span className="pp-ic">{artifact.type === "brief" ? "✎" : artifact.type === "recap" ? "¶" : "¶"}</span>
                <span className="pp-main">
                  <span className="pp-name">{artifact.name}</span>
                  <span className="pp-sub">
                    <span className="spark"><i style={{ height: 4 + index * 2 }}></i>{index > 0 && <i className="hot" style={{ height: 6 + index * 2 }}></i>}</span>
                    <span>rev {artifact.revision}</span>
                    {crit !== null && <span className="crit">{crit} open</span>}
                    {blk !== null && <span className="blk">{blk} block</span>}
                  </span>
                </span>
                <span className={`orb ${officeForIndex(index)} pp-owner`}></span>
                <span className={`pillstat ${className} pp-stand`}>{standing}</span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function IntakePage({
  onSubmit,
  error,
  openExplainer,
  setOpenExplainer
}: {
  onSubmit: (prompt: string) => void;
  error: string | null;
  openExplainer: string | null;
  setOpenExplainer: (id: string | null) => void;
}) {
  const [title, setTitle] = useState("");
  const [scope, setScope] = useState("");
  const [constraints, setConstraints] = useState("");
  const [criteria, setCriteria] = useState("");
  const [missionError, setMissionError] = useState<string | null>(null);

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const validationError = validateMissionPrompt(title);
    if (validationError) {
      setMissionError(validationError);
      return;
    }
    setMissionError(null);
    const combined = `${title}\n\nScope: ${scope}\n\nConstraints:\n${constraints}\n\nSuccess criteria: ${criteria}`;
    onSubmit(combined);
  }

  return (
    <section className="view">
      <div className="kicker">NEW MISSION</div>
      <h1 className="hero-title">Brief the society.</h1>
      <p className="hero-sub">A task force convenes around this brief, aligns on the goal, and decides whether it is ready to begin.</p>

      <div className="intake-flow" aria-label="What happens after a mission is submitted">
        {[
          "Discuss the brief",
          "Choose a leader",
          "Delegate fixed specialists",
          "Validate independently",
          "Deliver recorded artifacts"
        ].map((step, index) => <span key={step}><b>{index + 1}</b>{step}</span>)}
      </div>

      <form className="intake-wrap" onSubmit={handleSubmit}>
        <Explainer id="intake-form" title="Numbered brief form" text="Mission, scope, constraints, and success criteria as numbered sections. The society uses this brief as the starting mission context and revises it only when the run emits supporting events." openId={openExplainer} setOpenId={setOpenExplainer}>
          <div className="field">
            <span className="fnum">01</span>
            <label htmlFor="f-title">Mission</label>
            <input className="input big" id="f-title" value={title} onChange={(e) => setTitle(e.target.value)} />
            {missionError && <p className="field-error" role="alert">{missionError}</p>}
          </div>
          <div className="field">
            <span className="fnum">02</span>
            <label htmlFor="f-scope">Scope</label>
            <textarea className="textarea" id="f-scope" value={scope} onChange={(e) => setScope(e.target.value)} />
          </div>
          <div className="field">
            <span className="fnum">03</span>
            <label htmlFor="f-cons">Constraints</label>
            <textarea className="textarea" id="f-cons" value={constraints} onChange={(e) => setConstraints(e.target.value)} />
            <p className="hint">Constraints are enforceable: an office can block any proposal that violates one, and the block must name the constraint.</p>
          </div>
          <div className="field">
            <span className="fnum">04</span>
            <label htmlFor="f-crit">Success criteria</label>
            <textarea className="textarea" id="f-crit" value={criteria} onChange={(e) => setCriteria(e.target.value)} />
          </div>
        </Explainer>

        <button className="convene" type="submit">Convene society<span className="arrow">→</span></button>
        {error && <div className="error" role="alert">{error}</div>}
        <p className="convene-sub">Live agent roster · readiness gate before any work begins</p>
      </form>
    </section>
  );
}

function EmptyProjectionPage({ title, message }: { title: string; message: string }) {
  return (
    <section className="view">
      <div className="kicker">BACKEND STATE · <em>EMPTY</em></div>
      <h1 className="hero-title">{title}</h1>
      <p className="hero-sub">{message}</p>
    </section>
  );
}

function BenchmarkPage() {
  const metrics = [
    ["Task completion", "Required output contract and mandatory gates finish for the recorded scenario."],
    ["Evidence coverage", "Required acceptance checks, provenance, and recorded artifacts are present and attributable."],
    ["Independent validation", "A validator verifies the delivery separately from the producing specialist or run."],
    ["Collaboration trace", "The ledger shows decisions, handoffs, challenges, and the evidence behind the verdict."],
    ["Latency / cost", "Reported only when the record includes comparable wall time, tokens, or tool-credit accounting."]
  ];
  const evidence = ["scenario definition and declared baseline", "run ledger and decision trace", "artifact manifest and acceptance evidence", "independent validation result", "final verdict and recorded warnings or cleanup"];
  return <section className="view explainer-page">
    <div className="kicker">BENCHMARK · <em>EVIDENCE FIRST</em></div>
    <h1 className="hero-title">How the society is evaluated.</h1>
    <p className="hero-sub">This page explains the evaluation frame. It does not claim the society outperforms a baseline unless a recorded result supports that claim.</p>
    <div className="explainer-overview">
      <span className="eyebrow">SOCIETY VS. DECLARED BASELINE</span>
      <p>The benchmark compares the society with the baseline named in the recorded benchmark. Each side receives the same controlled scenario inputs, output contract, and evaluation rules; the UI does not infer a baseline or fill gaps in a run record.</p>
    </div>
    <section className="explainer-section" aria-labelledby="benchmark-flow">
      <div className="section-label" id="benchmark-flow">EVALUATION LIFECYCLE</div>
      <div className="explainer-flow" aria-label="Benchmark evaluation lifecycle">
        {["Freeze scenario", "Run both modes", "Collect artifacts", "Validate independently", "Record verdict"].map((stage, index) => <span key={stage}><b>{String(index + 1).padStart(2, "0")}</b>{stage}{index < 4 && <i aria-hidden="true">→</i>}</span>)}
      </div>
      <p className="hint">Controlled inputs include the scenario definition, fixtures, acceptance checks, budgets, and evaluator rules recorded for that comparison.</p>
    </section>
    <section className="explainer-section" aria-labelledby="benchmark-metrics">
      <div className="section-label" id="benchmark-metrics">WHAT IS MEASURED</div>
      <div className="metric-table" role="table" aria-label="Benchmark metric definitions">
        {metrics.map(([metric, definition]) => <div className="metric-row" role="row" key={metric}><strong role="cell">{metric}</strong><span role="cell">{definition}</span></div>)}
      </div>
    </section>
    <div className="recap-grid">
      <div className="card"><div className="card-head"><span className="card-title">EVIDENCE CHECKLIST</span></div><div className="card-body"><ul className="explainer-list">{evidence.map((item) => <li key={item}>{item}</li>)}</ul><p className="hint">Inspect the <a href="#live">Live</a>, <a href="#review">Review</a>, <a href="#recap">Recap</a>, <a href="#dossier">Dossier</a>, and <a href="#artifacts">Artifacts</a> views for run-level evidence when it exists.</p></div></div>
      <div className="card"><div className="card-head"><span className="card-title">HOW TO READ A VERDICT</span></div><div className="card-body"><dl className="status-key"><div><dt>Complete</dt><dd>Required result and recorded evidence passed.</dd></div><div><dt>Partial</dt><dd>Some work or proof is missing; it is not a clean comparison.</dd></div><div><dt>Warnings</dt><dd>The run completed with non-fatal issues that remain part of its record.</dd></div><div><dt>Failed</dt><dd>A required gate, execution step, or validation did not pass.</dd></div></dl></div></div>
      <div className="card"><div className="card-head"><span className="card-title">LIMITATIONS</span></div><div className="card-body"><p>A successful run alone is not a superiority result. Non-comparable inputs, missing validation, incomplete artifacts, or unrecorded cost and latency cannot establish a fair advantage.</p><p className="hint">Quality, speed, and cost must be interpreted from comparable recorded data, not from discussion volume or model narration.</p></div></div>
      <div className="card"><div className="card-head"><span className="card-title">CURRENT RESULTS</span></div><div className="card-body"><p>Published comparable results would appear with their scenario, baseline, evidence, and verdict. Until then, this is an explainer—not a scoreboard.</p><p className="empty-state">No comparable recorded benchmark result is presented here.</p></div></div>
    </div>
  </section>;
}

function ArchitecturePage() {
  const stages = ["Intake", "Society discussion", "Readiness", "Leader", "Fixed specialists", "AgentBay", "Independent validation", "Artifacts", "Verdict"];
  return <section className="view explainer-page">
    <div className="kicker">ARCHITECTURE · <em>RUN FLOW</em></div>
    <h1 className="hero-title">From brief to verdict.</h1>
    <p className="hero-sub">A recording-friendly map of the current delivery path. It describes responsibility boundaries; the ledger remains the evidence for any individual run.</p>
    <div className="architecture-flow" aria-label="Society execution flow">{stages.map((stage, index) => <span key={stage}><b>{String(index + 1).padStart(2, "0")}</b>{stage}{index < stages.length - 1 && <i aria-hidden="true">→</i>}</span>)}</div>
    <div className="recap-grid">
      <div className="card"><div className="card-head"><span className="card-title">SOCIETY → EMPLOYEES</span></div><div className="card-body"><p>Core society roles interpret the brief, surface risks, reach readiness, and elect a leader. The elected leader selects fixed specialist employees for bounded execution; the specialists are distinct from the deliberating society.</p><p className="hint">The leader selects from the repository-defined catalog. It does not author a new team or grant capabilities.</p></div></div>
      <div className="card"><div className="card-head"><span className="card-title">FIXED TOOLS & VERSIONED SKILLS</span></div><div className="card-body"><p>Tools and skills are repository-owned, versioned bundles attached to each specialist template. They are resolved and verified by the runtime—not granted, removed, or invented by model output.</p><p className="hint">A missing required tool or mismatched skill blocks execution visibly instead of silently shrinking the job.</p></div></div>
      <div className="card"><div className="card-head"><span className="card-title">AGENTBAY EXECUTION</span></div><div className="card-body"><p>Specialists execute bounded work in AgentBay sandboxes. The lifecycle records setup, work, exported artifacts, failures or cancellation, and cleanup; a sandbox is not evidence of completion by itself.</p><p className="hint">Independent validation checks the delivered artifacts after execution rather than trusting the producer’s narration.</p></div></div>
      <div className="card"><div className="card-head"><span className="card-title">EVENTS → VIEWER</span></div><div className="card-body"><p>The event stream feeds the <a href="#live">Live</a> ledger and projections for <a href="#review">Review</a>, <a href="#recap">Recap</a>, <a href="#dossier">Dossier</a>, and <a href="#artifacts">Artifacts</a>. These views reconstruct recorded state; they do not invent it.</p></div></div>
      <div className="card"><div className="card-head"><span className="card-title">CLARIFICATION & PAUSE</span></div><div className="card-body"><p>If the society needs user input, it records a clarification request and pauses at that boundary. The answer and resume event remain in the causal trace, so viewers can see what changed before work continues.</p></div></div>
      <div className="card"><div className="card-head"><span className="card-title">TRUTH, FAILURE & VERDICT</span></div><div className="card-body"><p>Validation results, blockers, retries, cancellations, and cleanup are part of the run record. A verdict reflects recorded acceptance evidence and validation—not a claim that an artifact exists, a tool ran, or a sandbox closed when the ledger cannot show it.</p></div></div>
    </div>
    <section className="explainer-section viewer-guide" aria-labelledby="viewer-guide">
      <div className="section-label" id="viewer-guide">WHAT TO WATCH DURING A LIVE RUN</div>
      <div className="metric-table"><div className="metric-row"><strong>Live</strong><span>Society discussion, readiness, leadership, specialist work, events, and blockers as they are emitted.</span></div><div className="metric-row"><strong>Review / Recap</strong><span>Decision rationale, the condensed causal sequence, and the recorded terminal state.</span></div><div className="metric-row"><strong>Dossier / Artifacts</strong><span>Role context, exported deliverables, ownership, provenance, and validation status where the run provides them.</span></div></div>
    </section>
  </section>;
}

function EvidenceNotice({ status, missing }: { status: string; missing: string[] }) {
  if (status === "complete") return null;
  return (
    <div className="error">
      Evidence is {status}. Missing sources: {missing.length > 0 ? missing.join(", ") : "none reported"}.
    </div>
  );
}

/** Deduplicate exact repeated model output before it reaches a judge-facing view. */
function uniqueDisplayText(items: string[]): string[] {
  return Array.from(new Set(items.map((item) => item.replace(/\s+/g, " ").trim()).filter(Boolean)));
}

function ReviewPage({
  task,
  review,
  openExplainer,
  setOpenExplainer
}: {
  task: TaskRun | null;
  review: DecisionReview | null;
  openExplainer: string | null;
  setOpenExplainer: (id: string | null) => void;
}) {
  if (!task) {
    return <EmptyProjectionPage title="Decision review" message="No run is selected. Create a run before reviewing decisions." />;
  }
  if (!review) {
    return <EmptyProjectionPage title="Decision review" message="Decision projection is not loaded. If the backend is offline, no review data is fabricated." />;
  }
  const hasOpinions = review.proposal_opinions.length > 0;
  const blockingObjections = uniqueDisplayText(review.blocking_objections);
  const unresolvedDissent = uniqueDisplayText(review.unresolved_dissent);
  const hasBlocking = blockingObjections.length > 0;
  const hasNonBlocking = review.non_blocking_dissent.length > 0;
  const hasUnresolved = unresolvedDissent.length > 0;
  const isRunning = task.status === "running";
  const failedBeforeProposals = task.status === "failed" && review.proposals.length === 0;
  const durableArtifacts = review.supporting_artifacts.filter((artifact) => artifact.type === "durable_artifact");

  return (
    <section className="view">
      <div className="kicker">DECISION RECORD · <em>{review.evidence_status.toUpperCase()}</em></div>
      <h1 className="hero-title">
        {review.selected_winner
          ? `Leader synthesis: ${review.selected_winner}`
          : review.winner_rationale
            ? `Winner: ${review.winner_rationale.winner_agent_id}`
            : "No winner selected yet"}
      </h1>
      <p className="hero-sub">
        {review.selected_winner
          ? "Projected from emitted society events. Winner ownership comes from the backend, not frontend interpretation."
          : `${review.proposals.length} proposal(s) in play. Phase: ${review.current_phase}.`}
      </p>
      {isRunning && review.proposals.length === 0 && (
        <div className="error" style={{ marginBottom: 18 }}>
          This run is still in progress. Proposal truth has not been emitted yet, so the empty review sections are expected for now.
        </div>
      )}
      {failedBeforeProposals && (
        <div className="error" style={{ marginBottom: 18 }}>
          This run failed before proposal, ballot, or winner events were emitted. The empty review sections reflect backend truth, not missing frontend rendering.
        </div>
      )}
      <EvidenceNotice status={review.evidence_status} missing={review.missing_sources} />
      {durableArtifacts.length > 0 && (
        <div className="card" style={{ marginBottom: 18 }}>
          <div className="card-body">
            <strong>DELIVERY EVIDENCE RECORDED</strong>
            <p className="hint">{durableArtifacts.length} durable artifact{durableArtifacts.length === 1 ? "" : "s"} emitted by execution. Open the artifact record for integrity and validation details.</p>
            <a className="artifact-entry" href="#artifacts">Open this run's artifacts →</a>
          </div>
        </div>
      )}

      {review.winner_rationale && (
        <div className="card" style={{ marginBottom: 18 }}>
          <div className="card-head"><span className="card-title"><span className="ic">◈</span>WINNER RATIONALE</span></div>
          <div className="card-body">
            <p className="brief-obj">{review.winner_rationale.why_won}</p>
            {review.winner_rationale.critical_tradeoffs.length > 0 && (
              <>
                <div className="kv">CRITICAL TRADEOFFS</div>
                {review.winner_rationale.critical_tradeoffs.map((item, i) => <div className="point" key={`ct-${i}`}>{item}</div>)}
              </>
            )}
            {review.winner_rationale.dissent_carried.length > 0 && (
              <>
                <div className="kv">DISSENT CARRIED FORWARD</div>
                {review.winner_rationale.dissent_carried.map((item, i) => <div className="dissent-note" key={`dc-${i}`}>{item}</div>)}
              </>
            )}
            {review.winner_rationale.supporting_votes.length > 0 && (
              <>
                <div className="kv">SUPPORTING VOTES</div>
                {review.winner_rationale.supporting_votes.map((item, i) => <div className="point" key={`sv-${i}`}>{item}</div>)}
              </>
            )}
          </div>
        </div>
      )}

      {hasBlocking && (
        <div className="error" style={{ marginBottom: 18 }}>
          <strong>ACTIVE BLOCKERS ({blockingObjections.length})</strong>
          <p className="hint">Resolve one of these conditions to let the society continue.</p>
          {blockingObjections.slice(0, 3).map((obj, i) => <div key={`bo-${i}`}>{obj}</div>)}
          {blockingObjections.length > 3 && <p className="hint">{blockingObjections.length - 3} additional blocker reports are collapsed.</p>}
        </div>
      )}

      <div className="bignums">
        <div className="bignum"><div className="n">{String(review.proposals.length).padStart(2, "0")}</div><div className="l">Competing proposals</div></div>
        <div className="bignum-sep"></div>
        <div className="bignum"><div className="n">{String(review.ballots.length).padStart(2, "0")}</div><div className="l">Ballots emitted</div></div>
        <div className="bignum-sep"></div>
        <div className="bignum"><div className="n">{String(review.proposal_opinions.length).padStart(2, "0")}</div><div className="l">Proposal opinions</div></div>
        <div className="bignum-sep"></div>
        <div className="bignum"><div className="n">{String(review.revisions.length).padStart(2, "0")}</div><div className="l">Revisions</div></div>
        <div className="bignum-sep"></div>
        <div className="bignum"><div className="n">{String(unresolvedDissent.length).padStart(2, "0")}</div><div className="l">Unresolved dissent</div></div>
      </div>

      <div className="duel">
        {review.proposals.length === 0 ? (
          <div className="card"><div className="card-body"><p className="empty-state">No proposal events have been emitted for this run.</p></div></div>
        ) : review.proposals.map((proposal, index) => {
          const isSelected = review.selected_proposal_id === proposal.proposal_id || review.selected_winner === proposal.agent_id;
          const opinionsForProposal = review.proposal_opinions.filter((op) => op.proposal_id === (proposal.proposal_id ?? ""));
          return (
            <div className="card" key={proposal.source_event_id}>
              <div className="prop-head">
                <span className="prop-letter a">{index + 1}</span>
                <span className="nm">{proposal.agent_id}
                  {proposal.created_from_phase && <span className="prop-type">FROM {proposal.created_from_phase.replaceAll("_", " ").toUpperCase()}</span>}
                  {proposal.supersedes_proposal_id && <span className="prop-type">SUPERSEDES PRIOR</span>}
                </span>
                <span className={`pillstat ${isSelected ? "leading" : "challenged"}`}>{isSelected ? "WINNING" : "RECORDED"}</span>
              </div>
              <div className="prop-body">
                <p className="thesis">{proposal.proposal}</p>
                {proposal.rationale && <div className="para">{proposal.rationale}</div>}
                {opinionsForProposal.length > 0 && (
                  <div style={{ marginTop: 10 }}>
                    <div className="kv">OPINIONS</div>
                    {opinionsForProposal.map((op) => (
                      <div className="point" key={op.source_event_id}>
                        <strong>{op.agent_id}</strong> [{op.stance}]{op.confidence != null ? ` ${Math.round(op.confidence * 100)}%` : ""}: {op.opinion}
                      </div>
                    ))}
                  </div>
                )}
                {opinionsForProposal.length === 0 && !hasOpinions && (
                  <p className="hint" style={{ marginTop: 8 }}>No proposal-level opinions emitted yet.</p>
                )}
              </div>
            </div>
          );
        })}
      </div>

      <div className="recap-grid">
        <div className="card">
          <div className="card-head"><span className="card-title"><span className="ic">◈</span>BALLOTS</span></div>
          <div className="card-body">
            {review.ballots.length === 0 && <p className="empty-state">No ballots have been emitted.</p>}
            {review.ballots.map((ballot) => (
              <div className="point" key={ballot.source_event_id}>{ballot.voter_id} voted for {ballot.choice}{ballot.reason ? `: ${ballot.reason}` : ""}{ballot.confidence != null ? ` (${Math.round(ballot.confidence * 100)}%)` : ""}</div>
            ))}
          </div>
        </div>
        <div className="card">
          <div className="card-head"><span className="card-title"><span className="ic">!</span>CRITIQUES</span></div>
          <div className="card-body">
            {review.critiques.length === 0 && <p className="empty-state">No critique events have been emitted.</p>}
            {review.critiques.map((critique) => (
              <div className="critique" key={critique.source_event_id}>
                {critique.critic_id && <strong>{critique.critic_id}</strong>}
                {critique.target && <span className="hint"> → {critique.target}</span>}
                <p>{critique.critique}</p>
                {critique.risks.length > 0 && <div className="kv">RISKS: {critique.risks.join("; ")}</div>}
                {critique.improvements.length > 0 && <div className="kv">IMPROVEMENTS: {critique.improvements.join("; ")}</div>}
              </div>
            ))}
          </div>
        </div>
      </div>

      {review.revisions.length > 0 && (
        <div className="card" style={{ marginTop: 18 }}>
          <div className="card-head"><span className="card-title"><span className="ic">↻</span>REVISIONS</span></div>
          <div className="card-body">
            {review.revisions.map((rev) => (
              <div className="point" key={rev.source_event_id}>
                <strong>{rev.agent_id}</strong>: {rev.revised_proposal}
                {rev.changes.length > 0 && <div className="hint">{rev.changes.join("; ")}</div>}
              </div>
            ))}
          </div>
        </div>
      )}

      {(hasNonBlocking || hasUnresolved) && (
        <div className="card" style={{ marginTop: 18 }}>
          <div className="card-head"><span className="card-title"><span className="ic">!</span>DISSENT</span></div>
          <div className="card-body">
            {hasNonBlocking && (
              <>
                <div className="kv">NON-BLOCKING DISSENT</div>
                {review.non_blocking_dissent.map((item, i) => <div className="dissent-note" key={`nbd-${i}`}>{item}</div>)}
              </>
            )}
            {hasUnresolved && (
              <>
                <div className="kv" style={{ marginTop: 10 }}>UNRESOLVED DISSENT</div>
                {unresolvedDissent.slice(0, 3).map((item, i) => <div className="dissent-note" key={`ud-${i}`}>{item}</div>)}
                {unresolvedDissent.length > 3 && <p className="hint">{unresolvedDissent.length - 3} additional dissent reports are collapsed.</p>}
              </>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function RecapPage({
  task,
  recap,
  events,
  openExplainer,
  setOpenExplainer
}: {
  task: TaskRun | null;
  recap: RunRecap | null;
  events: SocietyEvent[];
  openExplainer: string | null;
  setOpenExplainer: (id: string | null) => void;
}) {
  if (!task) {
    return <EmptyProjectionPage title="Run recap" message="No run is selected. Create a run before reading the recap." />;
  }
  if (!recap) {
    return <EmptyProjectionPage title="Run recap" message="Recap projection is not loaded. If the backend is offline, no recap is fabricated." />;
  }
  const minutes = recap.duration_seconds == null ? null : Math.max(1, Math.round(recap.duration_seconds / 60));
  const outcomeLabel = recap.completion_outcome === "complete" ? "COMPLETE" : recap.completion_outcome === "complete_with_warnings" ? "COMPLETE WITH WARNINGS" : recap.completion_outcome === "failed" ? "FAILED" : recap.completion_outcome === "waiting_for_user" ? "WAITING" : "PARTIAL";
  const isRunning = task.status === "running";
  const recapTitle = isRunning ? "Run in progress" : "Run recap";
  const recapKicker = isRunning ? "RUN STATE" : "AFTER-ACTION";
  // A recap can be partial while the run is paused; the event log remains the
  // authoritative source for objection count until the recap summarizes it.
  const observedDissentCount = events.filter((event) => event.type === "agent_objection_registered").length;
  const dissentCount = Math.max(recap.dissents.length, observedDissentCount);
  const savedLessons = uniqueDisplayText(recap.saved_lessons);

  return (
    <section className="view">
      <div className="kicker">{recapKicker} · <em>{recap.evidence_status.toUpperCase()}</em> · {outcomeLabel}</div>
      <h1 className="hero-title">{recapTitle}</h1>
      <p className="hero-sub">
        {isRunning
          ? `This run is still active. The recap is intentionally partial and only reflects events emitted so far. Status: ${recap.status}.`
          : recap.completion_outcome === "complete"
          ? "Projected from completed society events."
          : recap.completion_outcome === "complete_with_warnings"
            ? "Projected from completed society events. The run finished with warnings."
            : recap.completion_outcome === "failed"
            ? "This run did not complete. See failure details below."
            : `Partial recap from actual events. Status: ${recap.status}.`}
      </p>
      <a className="artifact-entry" href="#artifacts">Open this run's artifacts →</a>
      <EvidenceNotice status={recap.evidence_status} missing={recap.missing_sources} />

      {recap.failure && (
        <div className="card failure-card" style={{ marginBottom: 18 }}>
          <div className="card-head"><span className="card-title"><span className="ic">✕</span>FAILURE</span></div>
          <div className="card-body">
            <div className="kv">FAILED PHASE</div><div className="point">{recap.failure.phase}</div>
            <div className="kv">BLOCKING REASON</div><div className="point">{recap.failure.blocking_reason}</div>
            {recap.failure.missing_inputs.length > 0 && (
              <><div className="kv">MISSING INPUTS</div>{recap.failure.missing_inputs.map((item, i) => <div className="point" key={`mi-${i}`}>{item}</div>)}</>
            )}
            {recap.failure.missing_evidence.length > 0 && (
              <><div className="kv">MISSING EVIDENCE</div>{recap.failure.missing_evidence.map((item, i) => <div className="point" key={`me-${i}`}>{item}</div>)}</>
            )}
            {recap.failure.system_error && (
              <><div className="kv">SYSTEM ERROR</div><div className="point">{recap.failure.system_error}</div></>
            )}
            <div className="kv">RECOVERABLE</div><div className="point">{recap.failure.recoverable ? "Yes" : "No"}</div>
          </div>
        </div>
      )}

      <div className="bignums">
        <div className="bignum"><div className="n">{minutes == null ? "--" : String(minutes).padStart(2, "0")}<small> min</small></div><div className="l">Run duration</div></div>
        <div className="bignum-sep"></div>
        <div className="bignum"><div className="n">{String(recap.turns).padStart(2, "0")}</div><div className="l">Turns taken</div></div>
        <div className="bignum-sep"></div>
        <div className="bignum"><div className="n">{String(recap.events).padStart(2, "0")}</div><div className="l">Events emitted</div></div>
        <div className="bignum-sep"></div>
        <div className="bignum"><div className="n">{String(dissentCount).padStart(2, "0")}</div><div className="l">Dissents raised</div></div>
        <div className="bignum-sep"></div>
        <div className="bignum"><div className="n">{String(recap.mind_changes.length).padStart(2, "0")}</div><div className="l">Mind changed</div></div>
        <div className="bignum-sep"></div>
        <div className="bignum"><div className="n">{String(savedLessons.length).padStart(2, "0")}</div><div className="l">Lessons saved</div></div>
      </div>

      <div className="recap-grid">
        <div className="card">
          <div className="card-head"><span className="card-title"><span className="ic">⌔</span>PLAN CHANGES</span></div>
          <div className="card-body">
            {recap.plan_changes.length === 0 && <p className="empty-state">No plan-change evidence has been emitted.</p>}
            {recap.plan_changes.map((item, index) => <div className="point" key={`${item}-${index}`}>{item}</div>)}
          </div>
        </div>
        <div className="card">
          <div className="card-head"><span className="card-title"><span className="ic">∷</span>MIND CHANGES</span></div>
          <div className="card-body">
            {recap.mind_changes.length === 0 && <p className="empty-state">No mind-change events have been emitted.</p>}
            {recap.mind_changes.map((item, index) => (
              <div className="point" key={`mc-${index}`}>
                {asString(item.actor) ?? "unknown"}: {asString(item.previous_position) ?? "?"} → {asString(item.new_position) ?? "?"}
                {asString(item.reason) && <div className="hint">{asString(item.reason)}</div>}
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="recap-grid" style={{ marginTop: 18 }}>
        <div className="card">
          <div className="card-head"><span className="card-title"><span className="ic">!</span>DISSENTS & LESSONS</span></div>
          <div className="card-body">
            {savedLessons.length === 0 && recap.dissents.length === 0 && observedDissentCount === 0 && <p className="empty-state">No saved lessons or carried dissent have been emitted.</p>}
            {savedLessons.map((item, index) => <div className="lesson" key={`lesson-${index}`}><span className="lid">LESSON</span><p>{item}</p></div>)}
            {recap.dissents.map((item, index) => <div className="lesson dnote" key={`dissent-${index}`}><span className="lid">DISSENT</span><p>{item}</p></div>)}
            {recap.dissents.length === 0 && observedDissentCount > 0 && <p className="hint">{observedDissentCount} dissent event{observedDissentCount === 1 ? "" : "s"} recorded; detailed summaries are pending.</p>}
          </div>
        </div>
        <div className="card">
          <div className="card-head"><span className="card-title"><span className="ic">◈</span>TRUST & REPUTATION</span></div>
          <div className="card-body">
            {recap.trust_reputation_changes.length === 0 && <p className="empty-state">No trust or reputation change events have been emitted.</p>}
            {recap.trust_reputation_changes.map((item, index) => (
              <div className="point" key={`tr-${index}`}><strong>{item.type?.toString().replaceAll("_", " ") ?? "Recorded change"}</strong><div className="hint">{readableProjection(asRecord(item.payload) ?? item)}</div></div>
            ))}
          </div>
        </div>
      </div>

      {recap.social_deltas.length > 0 && (
        <div className="card" style={{ marginTop: 18 }}>
          <div className="card-head"><span className="card-title"><span className="ic">↕</span>SOCIAL DELTAS</span></div>
          <div className="card-body">
            {recap.social_deltas.map((item, index) => (
              <div className="point" key={`sd-${index}`}>{readableProjection(item)}</div>
            ))}
          </div>
        </div>
      )}

      {recap.delegation_outcomes.length > 0 && (
        <div className="card" style={{ marginTop: 18 }}>
          <div className="card-head"><span className="card-title"><span className="ic">⊙</span>DELEGATION OUTCOMES</span></div>
          <div className="card-body">
            {recap.delegation_outcomes.map((d) => (
              <div className="drow" key={d.subtask_id}>
                <span className="task">
                  <span className="t">{d.objective}</span>
                  <span className="o">{d.agent_id}{d.assigned_by ? ` (by ${d.assigned_by})` : ""}</span>
                </span>
                <span className={`pillstat ${d.status === "done" || d.status === "complete" ? "done" : d.status === "failed" ? "challenged" : "progress"}`}>{d.status.toUpperCase()}</span>
                {d.result_summary && <div className="hint" style={{ marginTop: 4 }}>{d.result_summary}</div>}
              </div>
            ))}
          </div>
        </div>
      )}

      {recap.final_answer && (
        <div className="card" style={{ marginTop: 18 }}>
          <div className="card-head"><span className="card-title"><span className="ic">✓</span>FINAL ANSWER</span></div>
          <div className="card-body readable-output">{recap.final_answer}</div>
        </div>
      )}
    </section>
  );
}

function artifactUrl(path: string): string {
  return /^https?:\/\//i.test(path) ? path : `${API_BASE}${path}`;
}

function formatBytes(bytes?: number | null): string {
  if (bytes == null) return "Size not recorded";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function artifactPreviewKind(artifact: TaskArtifact): "image" | "video" | "text" | null {
  const type = artifact.media_type?.toLowerCase() ?? "";
  const name = artifact.filename.toLowerCase();
  if (type.startsWith("image/") || /\.(png|jpe?g|gif|webp|svg)$/.test(name)) return "image";
  if (type.startsWith("video/") || /\.(mp4|webm|ogg|mov)$/.test(name)) return "video";
  if (type.startsWith("text/") || /\.(txt|md|json|ya?ml|tsx?|jsx?|css|html?|py|js|java|go|rs|sh|sql)$/.test(name)) return "text";
  return null;
}

function ArtifactPage({ task, artifacts, loading, error }: { task: TaskRun | null; artifacts: TaskArtifact[]; loading: boolean; error: string | null }) {
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [previewText, setPreviewText] = useState<Record<string, string>>({});
  const [previewErrors, setPreviewErrors] = useState<Record<string, string>>({});
  useEffect(() => { setExpandedId(null); setPreviewText({}); setPreviewErrors({}); }, [task?.id]);
  useEffect(() => {
    const artifact = artifacts.find((item) => item.id === expandedId);
    if (!artifact?.view_url || (artifact.status != null && artifact.status !== "available") || artifactPreviewKind(artifact) !== "text" || previewText[artifact.id] || previewErrors[artifact.id]) return;
    let cancelled = false;
    fetch(artifactUrl(artifact.view_url)).then(async (response) => {
      if (!response.ok) throw new Error("Safe text preview is unavailable.");
      return response.text();
    }).then((text) => { if (!cancelled) setPreviewText((current) => ({ ...current, [artifact.id]: text })); })
      .catch((previewError: unknown) => { if (!cancelled) setPreviewErrors((current) => ({ ...current, [artifact.id]: previewError instanceof Error ? previewError.message : "Safe text preview is unavailable." })); });
    return () => { cancelled = true; };
  }, [artifacts, expandedId, previewErrors, previewText]);
  if (!task) return <EmptyProjectionPage title="Artifacts" message="No run is selected. Create or replay a run before viewing its durable outputs." />;
  return <section className="view">
    <div className="kicker">DELIVERABLES · <em>RUN-SCOPED</em></div>
    <h1 className="hero-title">Artifacts</h1>
    <p className="hero-sub">Durable outputs recorded for this run. Preview and download are available only for integrity-verified artifacts when the backend supplies those safe routes.</p>
    {loading && <p className="hint">Loading recorded artifacts…</p>}
    {error && <div className="error">Artifacts could not be loaded: {error}</div>}
    {!loading && !error && artifacts.length === 0 && <div className="card"><div className="card-body empty-state">No durable artifacts have been recorded for this run.</div></div>}
    <div className="artifact-list">{artifacts.map((artifact) => {
      // Older retained runs may not carry status; preserve their existing safe-route behavior.
      const isAvailable = artifact.status == null || artifact.status === "available";
      const previewKind = isAvailable && artifact.view_url ? artifactPreviewKind(artifact) : null;
      const expanded = expandedId === artifact.id;
      return <article className="card artifact-card" key={artifact.id}><div className="card-body">
        <div className="artifact-head"><div><strong>{artifact.filename}</strong><span>{artifact.kind ?? artifact.media_type ?? "Unclassified artifact"}</span></div>{isAvailable && artifact.download_url && <a className="artifact-download" href={artifactUrl(artifact.download_url)} target="_blank" rel="noreferrer">Download</a>}</div>
        <dl className="artifact-meta"><div><dt>Type</dt><dd>{artifact.media_type ?? artifact.kind ?? "Not recorded"}</dd></div><div><dt>Size</dt><dd>{formatBytes(artifact.size_bytes)}</dd></div><div><dt>Validation</dt><dd>{artifact.validation_status ?? "Not validated"}</dd></div><div><dt>Producer</dt><dd>{artifact.producer ?? "Not recorded"}</dd></div><div className="artifact-digest"><dt>SHA-256</dt><dd>{artifact.sha256 ?? "Digest not recorded"}</dd></div></dl>
        {!isAvailable && <p className="hint">{artifact.status === "integrity_failed" ? "Integrity verification failed; this artifact is not available for preview or download." : "This artifact is missing from durable storage; preview and download are unavailable."}</p>}
        {isAvailable && !artifact.view_url && <p className="hint">No safe inline preview was provided for this artifact. Download it to inspect the original.</p>}
        {isAvailable && artifact.view_url && !previewKind && <p className="hint">A safe view exists, but this artifact type has no inline renderer. Download it to inspect the original.</p>}
        {previewKind && <button className="artifact-preview-toggle" type="button" onClick={() => setExpandedId(expanded ? null : artifact.id)}>{expanded ? "Hide preview" : "Preview"}</button>}
        {expanded && previewKind === "image" && <img className="artifact-preview-image" src={artifactUrl(artifact.view_url!)} alt={`Preview of ${artifact.filename}`} />}
        {expanded && previewKind === "video" && <video className="artifact-preview-video" controls src={artifactUrl(artifact.view_url!)}>Your browser cannot preview this video.</video>}
        {expanded && previewKind === "text" && <pre className="artifact-preview-text">{previewErrors[artifact.id] ?? previewText[artifact.id] ?? "Loading safe text preview…"}</pre>}
      </div></article>;
    })}</div>
  </section>;
}

function DossierPage({
  agents,
  selectedAgent,
  setSelectedAgent,
  dossier,
  taskId,
  openExplainer,
  setOpenExplainer
}: {
  agents: Agent[];
  selectedAgent: string | null;
  setSelectedAgent: (id: string | null) => void;
  dossier: AgentDossier | null;
  taskId: string | null;
  openExplainer: string | null;
  setOpenExplainer: (id: string | null) => void;
}) {
  const activeAgent = dossier?.agent ?? agents.find((agent) => agent.id === selectedAgent) ?? agents[0] ?? null;
  if (!activeAgent) {
    return <EmptyProjectionPage title="Agent dossier" message="No backend agents are available. The dossier will not substitute demo offices." />;
  }
  const activeIndex = Math.max(0, agents.findIndex((agent) => agent.id === activeAgent.id));
  const office = officeForIndex(activeIndex);
  const runSummary = dossier?.this_run_summary ?? {};
  const hasRunData = Object.keys(runSummary).length > 0;

  return (
    <section className="view">
      <div className="kicker">AGENT DOSSIER · <em>{dossier?.evidence_status.toUpperCase() ?? "LOADING"}</em>{taskId ? ` · RUN-SCOPED` : ""}</div>
      <h1 className="hero-title">{activeAgent.name}</h1>
      <p className="hero-sub">
        {taskId
          ? "Scoped to the current run. Summary first, raw detail expandable."
          : "Select a run to scope this dossier to a single task. Showing cross-run profile."}
      </p>
      {dossier && <EvidenceNotice status={dossier.evidence_status} missing={dossier.missing_sources} />}
      <div className="dossier-head">
        <span className={`orb ${office}`} style={{ width: 62, height: 62 }}></span>
        <div>
          <div className="dh-name">{activeAgent.name}</div>
          <div className="dh-role">{activeAgent.role}</div>
        </div>
        <div className="dh-right">REP<br />{(dossier?.reputation ?? activeAgent.reputation).toFixed(2)}</div>
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 22, flexWrap: "wrap" }}>
        {agents.map((agent, index) => (
          <button
            key={agent.id}
            type="button"
            onClick={() => setSelectedAgent(agent.id)}
            style={{
              display: "inline-flex", alignItems: "center", gap: 8,
              padding: "6px 14px", borderRadius: 999,
              background: agent.id === activeAgent.id ? "var(--ink)" : "var(--card)",
              color: agent.id === activeAgent.id ? "#F6EFE4" : "var(--sub)",
              fontSize: 12, fontWeight: 600, boxShadow: "var(--shadow-sm)",
              border: "none", cursor: "pointer"
            }}
          >
            <span className={`orb ${officeForIndex(index)}`} style={{ width: 18, height: 18 }}></span>
            {agent.name}
          </button>
        ))}
      </div>
      {!dossier && <div className="error">Dossier projection is not loaded. If the backend is offline, no dossier behavior is fabricated.</div>}

      {dossier && hasRunData && (
        <div className="card" style={{ marginTop: 18 }}>
          <div className="card-head"><span className="card-title"><span className="ic">≡</span>THIS RUN SUMMARY</span></div>
          <div className="card-body">
            {runSummary.status ? <div className="point"><strong>Status:</strong> {String(runSummary.status)}</div> : null}
            {runSummary.phase ? <div className="point"><strong>Phase:</strong> {String(runSummary.phase)}</div> : null}
            {runSummary.agent_contributions != null ? <div className="point"><strong>Contributions:</strong> {String(runSummary.agent_contributions)}</div> : null}
            {runSummary.final_answer ? <div className="point"><strong>Final answer:</strong> {String(runSummary.final_answer)}</div> : null}
          </div>
        </div>
      )}

      <div className="dossier-grid">
        <div>
          {dossier && dossier.this_run_timeline.length > 0 && (
            <div className="card">
              <div className="card-head"><span className="card-title"><span className="ic">⌛</span>RUN TIMELINE</span></div>
              <div className="card-body">
                {dossier.this_run_timeline.slice(0, 12).map((item, index) => (
                  <div className="point" key={`tl-${index}`}>
                    <strong>{String(item.type ?? "")}</strong>
                    {item.actor ? ` by ${String(item.actor)}` : null}
                    {item.message ? `: ${String(item.message)}` : null}
                  </div>
                ))}
                {dossier.this_run_timeline.length > 12 && <p className="hint">{dossier.this_run_timeline.length - 12} more events not shown.</p>}
              </div>
            </div>
          )}

          <div className="card" style={{ marginTop: 18 }}>
            <div className="card-head"><span className="card-title"><span className="ic">≡</span>BEHAVIOURAL TENDENCIES</span></div>
            <div className="card-body">
              {(dossier?.behavioral_tendencies.length ? dossier.behavioral_tendencies : activeAgent.profile.default_blockers.map((item) => `default blocker: ${item}`)).map((item, index) => (
                <div className="tend" key={`${item}-${index}`}><span className="tl">EVIDENCE</span><span>{item}</span></div>
              ))}
              <div className="tend"><span className="tl">FAILURE MODE</span><span>{activeAgent.profile.failure_mode || "No failure mode documented in profile."}</span></div>
            </div>
          </div>

          <div className="card" style={{ marginTop: 18 }}>
            <div className="card-head"><span className="card-title"><span className="ic">∷</span>RUN LESSONS & NOTES</span></div>
            <div className="card-body">
              {(!dossier || (dossier.run_lessons.length === 0 && dossier.published_notes.length === 0)) && <p className="empty-state">No run lessons or published notes for this agent.</p>}
              {dossier?.run_lessons.map((item, index) => <div className="lesson" key={`lesson-${index}`}><span className="lid">LESSON</span><p>{item}</p></div>)}
              {dossier?.published_notes.map((item, index) => <div className="lesson" key={`note-${index}`}><span className="lid">NOTE</span><p>{item}</p></div>)}
            </div>
          </div>
        </div>
        <div>
          {dossier && dossier.run_tool_usage.length > 0 && (
            <div className="card">
              <div className="card-head"><span className="card-title"><span className="ic">⚙</span>TOOL USAGE THIS RUN</span></div>
              <div className="card-body">
                {dossier.run_tool_usage.slice(0, 6).map((tool) => (
                  <div className="point" key={tool.source_event_id}>
                    <strong>{tool.tool_name}</strong> [{tool.phase ?? "?"}]
                    {tool.why_used && !tool.why_used.startsWith("Task:") && <div className="hint">Purpose: {tool.why_used}</div>}
                    {tool.result_summary && !/^round,\s*agent_id|^objective,\s*steps|^agent_id,\s*phase/i.test(tool.result_summary) && <div className="hint">Outcome: {tool.result_summary}</div>}
                    <span className={`pillstat ${tool.success ? "done" : "challenged"}`} style={{ marginTop: 4 }}>{tool.success ? "OK" : "FAILED"}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {dossier && dossier.run_delegation.length > 0 && (
            <div className="card" style={{ marginTop: 18 }}>
              <div className="card-head"><span className="card-title"><span className="ic">⊙</span>DELEGATION THIS RUN</span></div>
              <div className="card-body">
                {dossier.run_delegation.map((d) => (
                  <div className="drow" key={d.subtask_id}>
                    <span className="task">
                      <span className="t">{d.objective}</span>
                      <span className="o">{d.agent_id === activeAgent.id ? "owned" : `assigned by ${d.assigned_by ?? "?"}`}</span>
                    </span>
                    <span className={`pillstat ${d.status === "done" || d.status === "complete" ? "done" : "progress"}`}>{d.status.toUpperCase()}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="card" style={{ marginTop: 18 }}>
            <div className="card-head"><span className="card-title"><span className="ic">◈</span>RECENT STANCES</span></div>
            <div className="card-body">
              {(!dossier || dossier.recent_stances.length === 0) && <p className="empty-state">No stance events have been emitted for this agent.</p>}
              {dossier?.recent_stances.map((stance) => (
                <div className="point" key={stance.source_event_id}>{stance.phase ?? "phase"}: {stance.stance}{stance.reason ? ` — ${stance.reason}` : ""}</div>
              ))}
            </div>
          </div>

          <div className="card" style={{ marginTop: 18 }}>
            <div className="card-head"><span className="card-title"><span className="ic">▤</span>TRUST EVIDENCE</span></div>
            <div className="card-body">
              {(!dossier || dossier.trust.length === 0) && <p className="empty-state">No trust update events have been emitted for this agent.</p>}
              {dossier?.trust.map((item, index) => <div className="point" key={`trust-${index}`}>{readableProjection(asRecord(item.payload) ?? item)}</div>)}
            </div>
          </div>
        </div>
      </div>
    </section>
  );

}

function SpecialistExecutionCard({ cockpit }: { cockpit: RunCockpit }) {
  const execution = cockpit.specialist_execution;
  if (!execution) return null;

  const readinessLabel = execution.development_readiness.replaceAll("_", " ").toUpperCase();
  return (
    <div className="card specialist-execution" data-testid="specialist-execution">
      <div className="card-head">
        <span className="card-title"><span className="ic">⌘</span>FIXED SPECIALIST EXECUTION</span>
        <span className="right"><span className={`ver readiness-${execution.development_readiness}`}>{readinessLabel}</span></span>
      </div>
      <div className="card-body">
        {execution.selection_rationale && <p className="specialist-rationale">{execution.selection_rationale}</p>}
        {execution.correction_count > 0 && (
          <p className="specialist-correction">{execution.correction_count} rejected selection attempt(s) remain visible.</p>
        )}
        {execution.blockers.map((blocker, index) => (
          <div className="failure-banner" key={`${blocker}-${index}`}>{blocker}</div>
        ))}
        <div className="specialist-grid">
          {execution.assignments.map((assignment) => (
            <article className={`specialist-node specialist-${assignment.status}`} key={assignment.assignment_id}>
              <div className="specialist-node-head">
                <div>
                  <strong>{assignment.template_id.replaceAll("_", " ")}</strong>
                  <span>template v{assignment.template_version}</span>
                </div>
                <span className="pillstat progress">{assignment.status.toUpperCase()}</span>
              </div>
              <p>{assignment.objective}</p>
              {assignment.depends_on.length > 0 && (
                <div className="specialist-dependency">WAITS FOR → prior specialist work</div>
              )}
              <div className="specialist-meta">
                <span>Sandbox: <b>{assignment.sandbox_status}</b></span>
                <span>Specialist: <b>{assignment.agent_id ? "assigned" : "not materialized"}</b></span>
              </div>
              <div className="specialist-bundle">
                <small>CAPABILITIES</small>
                <div>{assignment.capabilities.map((item) => <span className="badge" key={item}>{item}</span>)}</div>
                <details className="ledger-details"><summary>Fixed tools and verified skills</summary><p>{assignment.tool_ids.length} predefined tool{assignment.tool_ids.length === 1 ? "" : "s"} and {assignment.skills.length} versioned skill{assignment.skills.length === 1 ? "" : "s"} are attached to this template.</p></details>
              </div>
              {assignment.artifacts.length > 0 && (
                <div className="specialist-artifacts">
                  <small>ARTIFACTS</small>
                  {assignment.artifacts.map((artifact) => (
                    <div key={artifact.path}>
                      <span>Recorded artifact</span>
                      <span>{artifact.status} · validation {artifact.validation_status}</span>
                      {artifact.view_url && (
                        <a href="#artifacts">
                          Open artifact
                        </a>
                      )}
                      <details className="ledger-details"><summary>Technical artifact detail</summary><p>{artifact.path}</p></details>
                    </div>
                  ))}
                </div>
              )}
              {assignment.blocker && <div className="failure-banner">{assignment.blocker}</div>}
            </article>
          ))}
        </div>
        <p className="hint">Development readiness only. This view does not claim benchmark superiority.</p>
      </div>
    </div>
  );
}

function ConversationTurnCard({ event, index, agents, opinion }: { event: SocietyEvent; index: number; agents: Agent[]; opinion: SocietyEvent | null }) {
  const actor = event.actor ?? asString(event.payload.agent_id) ?? `office-${index + 1}`;
  const agent = agents.find((item) => item.id === actor);
  const isClarification = event.type === "user_clarification_requested" || event.type === "user_clarification_answered";
  const says = asString(event.payload.answer) ?? asString(event.payload.clarification) ?? asString(event.payload.question) ?? asString(event.payload.says) ?? event.message;
  const respondsTo = asString(event.payload.responds_to);
  const quote = asString(event.payload.quote_from_prior);
  const question = asString(event.payload.question_for_next);
  const reasoning = opinion ? asString(opinion.payload.unique_contribution) ?? asString(opinion.payload.interpretation) : null;
  return <div className={`turn direct conversation-turn${respondsTo ? " has-parent" : ""}`}>
    <div className="turn-top">
      <span className={`orb ${officeForIndex(index)}`}></span>
      <span className="name">{isClarification ? (event.type === "user_clarification_answered" ? "You" : "Society") : displayIdentity(actor, agents, "Society member").split(" — ")[0]}</span>
      <span className="role">{isClarification ? (event.type === "user_clarification_requested" ? "Clarification requested" : "Clarification answered") : agent?.role ?? "Agent"}</span>
      <span className="ttag direct">{isClarification ? "CLARIFICATION" : `TURN ${String(event.payload.turn_index ?? index + 1)}`}</span>
      <span className="time">{new Date(event.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
    </div>
    <div className="tx">{says}</div>
    {respondsTo && <div className="ledger-link">↳ Responding to <b>{respondsTo}</b>{quote ? `: “${quote}”` : ""}</div>}
    {question && <div className="ledger-link next">Next question: {question}</div>}
    {reasoning && <details className="ledger-details"><summary>Reasoning and acceptance context</summary><p>{reasoning}</p></details>}
  </div>;
}

function LedgerSystemCard({ event }: { event: SocietyEvent }) {
  const payload = event.payload;
  const time = new Date(event.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (event.type === "readiness_vote_cast") {
    const ready = asBoolean(payload.ready);
    return <div className={`ledger-card readiness ${ready ? "ready" : "blocked"}`}><b>{event.actor ?? asString(payload.agent_id) ?? "Agent"}</b><span>{ready ? "READY" : "NOT READY"}</span><p>{asString(payload.reason) ?? "No reason recorded."}</p><small>{time}</small></div>;
  }
  if (event.type === "readiness_vote_tallied") {
    return <div className="ledger-card decision"><b>READINESS GATE</b><p>{event.message}</p><small>{time}</small></div>;
  }
  if (event.type === "leader_election_started" || event.type === "leader_elected") {
    return <div className="ledger-card decision"><b>{event.type === "leader_elected" ? "LEADER ELECTED" : "LEADER ELECTION"}</b><p>{event.type === "leader_elected" ? `${event.actor ?? asString(payload.leader_id) ?? "A leader"} now coordinates this run.` : event.message}</p><small>{time}</small></div>;
  }
  if (event.type === "agent_endorsed_peer" || event.type === "agent_deferred_ownership") {
    const target = asString(payload.target_agent_id) ?? asString(payload.deferred_to) ?? asString(payload.leader_id);
    return <div className="ledger-card endorsement"><b>{event.actor ?? "Agent"}</b><p>{event.type === "agent_endorsed_peer" ? "endorsed and deferred ownership to" : "deferred ownership to"} {target ?? "the elected leader"}.</p><small>{time}</small></div>;
  }
  if (event.type === "specialist_selection_proposed") {
    const assignments = Array.isArray(payload.assignments) ? payload.assignments : [];
    return <div className="ledger-card specialist"><b>FIXED SPECIALIST PLAN PROPOSED</b><p>{asString(payload.selection_rationale) ?? event.message}</p>{assignments.length > 0 && <ul>{assignments.map((item, index) => { const assignment = asRecord(item); const dependencies = asStringArray(assignment?.depends_on); return <li key={asString(assignment?.assignment_id) ?? index}>{asString(assignment?.assignment_id) ?? "assignment"}: {asString(assignment?.template_id) ?? "specialist"}{dependencies.length > 0 ? ` · depends on ${dependencies.join(", ")}` : ""}</li>; })}</ul>}<small>{time}</small></div>;
  }
  if (event.type === "specialist_selection_rejected") {
    const blockers = Array.isArray(payload.blockers) ? payload.blockers : [];
    return <div className="ledger-card specialist rejected"><b>PLAN REJECTED — CORRECTION REQUIRED</b>{blockers.map((item, index) => { const blocker = asRecord(item); return <p key={index}><code>{asString(blocker?.code) ?? "policy_blocker"}</code> · {asString(blocker?.assignment_id) ?? "selection"}: {asString(blocker?.message) ?? "No detail recorded."}</p>; })}<small>{time}</small></div>;
  }
  if (event.type === "run_failed" || event.type === "task_failed") {
    return <div className="ledger-card terminal"><b>RUN FAILED</b><p>{asString(payload.blocking_reason) ?? asString(payload.error) ?? event.message}</p><small>Phase: {asString(payload.phase) ?? "not recorded"} · {asString(payload.blocker_category) ?? "unclassified"} · {time}</small></div>;
  }
  if (event.type === "acceptance_evidence_evaluated") {
    const requiredPassed = asBoolean(payload.required_passed);
    const optionalFailures = asStringArray(payload.optional_failures);
    return <div className={`ledger-card terminal ${requiredPassed ? "passed" : ""}`}>
      <b>AUTHORITATIVE RUNTIME VERDICT</b>
      <p>{requiredPassed
        ? "Required delivery evidence passed: the artifact was exported, independently validated, and every selected sandbox closed. Earlier agent proposals are provisional opinions, not the runtime verdict."
        : "Required delivery evidence did not pass. Review the unresolved requirements before accepting the run."}</p>
      {optionalFailures.length > 0 && <small>Optional demo-proof gaps: {optionalFailures.join(", ")} · {time}</small>}
      {optionalFailures.length === 0 && <small>{time}</small>}
    </div>;
  }
  const executionLabels: Record<string, string> = {
    specialist_invocation_started: "Workspace started",
    work_node_started: "Workspace started",
    workspace_file_written: "File written",
    file_written: "File written",
    desktop_rendered: "Desktop rendered",
    mobile_rendered: "Mobile rendered",
    artifact_exported: "Artifact exported",
    artifact_validated: "Independent validation",
    local_independent_validation_reported: "Independent validation",
    composition_assignment_cleanup_completed: "Environments closed",
    composition_assignment_cleanup_warning: "Environment cleanup warning"
  };
  const executionLabel = executionLabels[event.type];
  if (executionLabel) {
    return <div className="ledger-card"><b>{executionLabel.toUpperCase()}</b><p>{event.message || "Recorded by the execution event stream."}</p><small>{time}</small></div>;
  }
  return <details className="ledger-audit-row"><summary>{humanEventLabel(event.type)}</summary><p>{event.message || "Recorded technical event."} · {time}</p></details>;
}

function LiveRunPage({
  prompt,
  setPrompt,
  task,
  cockpit,
  tasks,
  events,
  agents,
  error,
  selectedAgent,
  setSelectedAgent,
  agentMemory,
  openExplainer,
  setOpenExplainer,
  onSubmit,
  onReplay,
  isSubmitting
}: {
  prompt: string;
  setPrompt: (v: string) => void;
  task: TaskRun | null;
  cockpit: RunCockpit | null;
  tasks: TaskRun[];
  events: SocietyEvent[];
  agents: Agent[];
  error: string | null;
  selectedAgent: string | null;
  setSelectedAgent: (v: string | null) => void;
  agentMemory: AgentMemory[];
  openExplainer: string | null;
  setOpenExplainer: (id: string | null) => void;
  onSubmit: (e: FormEvent) => void;
  onReplay: (id: string) => void;
  isSubmitting: boolean;
}) {
  const [heartbeatNow, setHeartbeatNow] = useState(Date.now());
  const displayAgents = agents;
  const lastActor = events.length > 0 ? (events[events.length - 1].actor ?? asString(events[events.length - 1].payload.agent_id) ?? null) : null;
  const typedEventTypes = new Set([
    "agent_position_stated",
    "agent_objection_registered",
    "agent_changed_mind",
    "targeted_question_answered",
    "artifact_section_critiqued",
    "shared_artifact_revised"
  ]);
  const opinionByAgentAndRound = useMemo(() => new Map(
    events.filter((event) => event.type === "agent_goal_opinion").map((event) => [
      `${event.actor ?? asString(event.payload.agent_id) ?? "unknown"}:${String(event.payload.round ?? "")}`,
      event
    ])
  ), [events]);
  const systemEventTypes = new Set([
    "conversation_turn",
    "user_clarification_requested",
    "user_clarification_answered",
    "readiness_vote_cast",
    "leader_elected",
    "leader_election_started",
    "agent_endorsed_peer",
    "agent_deferred_ownership",
    "readiness_vote_tallied",
    "working_brief_finalized",
    "subtasks_assigned_from_brief",
    "delegation_assigned",
    "solution_selected",
    "meeting_recap",
    "specialist_selection_proposed",
    "specialist_selection_rejected",
    "specialist_selection_accepted",
    "specialist_invocation_approved",
    "specialist_invocation_started",
    "workspace_file_written",
    "file_written",
    "desktop_rendered",
    "mobile_rendered",
    "artifact_exported",
    "local_independent_validation_reported",
    "composition_assignment_materialized",
    "work_node_started",
    "work_node_blocked",
    "work_node_failed",
    "work_node_retry_scheduled",
    "work_node_canceled",
    "work_node_completed",
    "composition_assignment_cleanup_completed",
    "composition_assignment_cleanup_warning",
    "artifact_validated",
    "acceptance_evidence_evaluated",
    "run_failed",
    "task_failed"
  ]);
  const ledgerEvents = useMemo(() => events
    .filter((event) => {
      if (!(typedEventTypes.has(event.type) || systemEventTypes.has(event.type))) return false;
      if (event.type === "task_failed" && events.some((item) => item.type === "run_failed")) return false;
      // A peer endorsement and the matching ownership-defer event record the
      // same leadership decision. Keep one concise ledger row rather than
      // presenting it as two user-visible endorsements.
      if (event.type === "agent_deferred_ownership" && events.some((item) => item.type === "agent_endorsed_peer" && item.actor === event.actor)) return false;
      return true;
    })
    .sort((a, b) => (Date.parse(a.created_at) || 0) - (Date.parse(b.created_at) || 0)), [events]);

  const latestWorkingBrief = useMemo(() => (
    events.findLast((event) => event.type === "working_brief_finalized")?.payload ?? null
  ), [events]);
  const latestAssignments = useMemo(() => (
    events.findLast((event) => event.type === "subtasks_assigned_from_brief")?.payload ?? null
  ), [events]);
  const currentUnderstanding = useMemo(() => {
    const briefSummary = asString(latestWorkingBrief?.agreed_scope) ?? asString(latestWorkingBrief?.summary);
    const source = briefSummary ?? task?.prompt ?? null;
    if (!source) return null;
    return source.length > 240 ? `${source.slice(0, 237).trimEnd()}…` : source;
  }, [latestWorkingBrief, task?.prompt]);
  const replayTasks = useMemo(() => {
    const available = tasks.filter((item) => item.id !== task?.id);
    const recent = available.slice(0, 5);
    // A hard restart can leave the newest runs interrupted. Keep proven outcomes
    // visible beside recent history so a reviewer can inspect real completed work.
    const completed = available
      .filter((item) => isCompletedStatus(item.status) && !recent.some((candidate) => candidate.id === item.id))
      .slice(0, 3);
    return [...recent, ...completed];
  }, [task?.id, tasks]);

  const timelineEvents = useMemo(() => events.filter(isTimelineEvent), [events]);
  const currentPhase = cockpit?.current_phase && phaseOrder.some((item) => item.key === cockpit.current_phase)
    ? cockpit.current_phase as PhaseKey
    : isTerminalStatus(task?.status)
    ? "final_answer"
    : timelineEvents.length > 0
      ? phaseOf(timelineEvents[timelineEvents.length - 1].type, timelineEvents[timelineEvents.length - 1].payload)
      : null;
  const currentPhaseConfig = currentPhase ? phaseConfig(currentPhase) : null;
  const currentPhaseIndex = currentPhase
    ? societyWorkflow.findIndex((item) => item.phase === currentPhase)
    : -1;

  const selectedAgentName = agents.find((agent) => agent.id === selectedAgent)?.name;
  const isRunActive = task?.status === "queued" || task?.status === "running";
  // A paused run can have objection events before the cockpit has summarized
  // its blocker list; do not render a misleading zero in that interval.
  const activeBlockerCount = Math.max(
    cockpit?.blockers.length ?? 0,
    events.filter((event) => event.type === "agent_objection_registered").length
  );
  const lastEventTime = events.length > 0 ? Date.parse(events[events.length - 1].created_at) : null;
  const secondsSinceLastEvent = lastEventTime ? Math.max(0, Math.round((heartbeatNow - lastEventTime) / 1000)) : null;
  const activityLabel = !task
    ? "Ready for a mission."
    : task.status === "queued"
      ? "Run queued. The society is being convened."
      : task.status === "running"
        ? secondsSinceLastEvent !== null && secondsSinceLastEvent > 15
          ? "Still working. The backend may be waiting on an LLM/tool response."
          : "Live events are flowing from the society."
        : task.status === "waiting_for_user"
          ? "This retained run paused before autonomous completion."
          : task.status === "interrupted"
            ? "Run interrupted. The society is no longer active."
          : task.status === "remediation"
            ? "Run ended in remediation. Review the recorded gaps before starting a new mission."
          : task.status === "complete"
            ? "Run complete."
            : task.status === "complete_with_warnings"
              ? "Run complete with warnings."
              : "Run failed. See the failure details below.";

  useEffect(() => {
    if (!isRunActive) return;
    const timer = window.setInterval(() => setHeartbeatNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [isRunActive]);

  const liveTitle = !task
    ? "Ready for a mission."
    : task.status === "queued"
      ? "Society is convening."
      : task.status === "running"
        ? currentPhaseConfig?.label ?? "Society at work."
        : task.status === "waiting_for_user"
          ? "Retained run paused."
          : task.status === "interrupted"
            ? "Run interrupted."
          : task.status === "remediation"
            ? "Remediation required."
          : task.status === "complete"
            ? "Run complete."
            : task.status === "complete_with_warnings"
              ? "Run complete with warnings."
              : "Run did not complete.";

  const nextActionHint = task?.status === "waiting_for_user"
    ? "This retained run paused for clarification. New missions proceed autonomously and surface irreducible blockers as terminal outcomes."
    : isCompletedStatus(task?.status)
      ? task?.status === "complete_with_warnings"
        ? "Review the outcome (with warnings) on the Recap screen."
        : "Review the outcome on the Recap screen."
      : task?.status === "failed"
        ? "Check the failure details or start a new mission from Intake."
        : task?.status === "interrupted"
          ? "This retained run was interrupted. Start a new mission from Intake."
        : task?.status === "remediation"
          ? "This run requires remediation. Review its evidence, then start a new mission from Intake."
        : task?.status === "queued"
          ? "No action needed — the team is being assembled."
          : null;

  return (
    <section className="view">
      <div className="kicker">
        MISSION · <em>{(task?.status ?? "intake").toUpperCase()}{currentPhaseConfig ? ` · ${currentPhaseConfig.label.toUpperCase()}` : ""}</em>
      </div>
      <h1 className="hero-title">{liveTitle}</h1>

      <p className="hero-sub">
        {task?.status === "waiting_for_user"
          ? "This retained run paused before autonomous completion."
          : task?.status === "interrupted"
            ? "This run was interrupted and is not currently executing."
          : task?.status === "remediation"
            ? "This run ended with unresolved evidence or validation gaps and is not currently executing."
          : task?.status === "failed"
            ? "This run did not complete. Failure details are shown below when available."
          : latestWorkingBrief
            ? asString(latestWorkingBrief.summary) ?? "The brief is ratified and work is underway."
          : task
            ? "The society is convened and deliberating."
            : "Submit a problem to convene the society."}
      </p>

      {task && <a className="artifact-entry" href="#artifacts">View this run's artifacts →</a>}

      {nextActionHint && (
        <div className="next-action-hint" role="status">
          {nextActionHint}
        </div>
      )}

      {!task && (
        <form className="composer" onSubmit={onSubmit}>
          <textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="Describe the mission..." />
          <button type="submit" disabled={isSubmitting}>
            <Send size={18} /> {isSubmitting ? "Convening..." : "Convene society"}
          </button>
        </form>
      )}

      {error && <div className="error">{error}</div>}

      {cockpit?.failure && (
        <div className="error" style={{ marginBottom: 12 }}>
          <strong>RUN FAILED</strong> in phase {cockpit.failure.phase}: {cockpit.failure.blocking_reason}
          {cockpit.failure.missing_inputs.length > 0 && <div>Missing inputs: {cockpit.failure.missing_inputs.join(", ")}</div>}
          {cockpit.failure.missing_evidence.length > 0 && <div>Missing evidence: {cockpit.failure.missing_evidence.join(", ")}</div>}
          {cockpit.failure.system_error && <div>System error: {cockpit.failure.system_error}</div>}
        </div>
      )}

      {task?.status === "waiting_for_user" && cockpit && cockpit.trace_status !== "live" && !cockpit.failure && (
        <div className="error" style={{ marginBottom: 12 }}>
          This retained run is paused. {cockpit.stale_reason ?? "Its historical clarification state is preserved for replay."}
        </div>
      )}

      {cockpit?.leader_rationale && (
        <div className="card" style={{ marginBottom: 12 }}>
          <div className="card-head"><span className="card-title"><span className="ic">◈</span>LEADER RATIONALE</span></div>
          <div className="card-body"><p>{cockpit.leader_rationale}</p></div>
        </div>
      )}

      {cockpit && <SpecialistExecutionCard cockpit={cockpit} />}

      <div className="bignums">
        <div className="bignum">
          <div className="n">{String(Math.max(1, currentPhaseIndex + 1)).padStart(2, "0")}<small> / {String(societyWorkflow.length).padStart(2, "0")}</small></div>
          <div className="l">Current phase · {currentPhaseConfig?.label ?? (task ? "Setup" : "Intake")}</div>
        </div>
        <div className="bignum-sep"></div>
        <div className="bignum">
          <div className="n">{String(events.length).padStart(2, "0")}</div>
          <div className="l">Events emitted</div>
        </div>
        <div className="bignum-sep"></div>
        <div className="bignum">
          <div className="n">{String(cockpit?.participants.length ?? displayAgents.length).padStart(2, "0")}</div>
          <div className="l">Participants</div>
        </div>
        <div className="bignum-sep"></div>
        <div className="bignum">
          <div className="n">{String(activeBlockerCount).padStart(2, "0")}</div>
          <div className="l">Blocking signals</div>
        </div>
      </div>

      <div className={`run-heartbeat ${isRunActive ? "active" : ""}`} role="status" aria-live="polite">
        <span className="pulse"></span>
        <span>{activityLabel}</span>
        {secondsSinceLastEvent !== null && <strong>Last event {elapsedLabel(secondsSinceLastEvent)}</strong>}
      </div>

      <DotStrip currentPhaseIndex={Math.max(0, currentPhaseIndex)} cockpit={cockpit} />

      <div className="sect"><span className="ic">◎</span>THE ROOM</div>

      <div className="competence-roster" aria-label="Active specialist employees">
        {displayAgents.map((agent) => (
          <div className="competence-chip" key={agent.id}>
            <strong>{agent.name}</strong><span>{agent.role}</span>
            <small>{agent.profile.decision_bias || agent.skills.slice(0, 2).join(" · ")}</small>
          </div>
        ))}
      </div>

      <div className="workspace">
        <div>
          <div className="dials" data-explain-title="Stance dials" data-explain-text="Each office's live position. The arc shows stance strength — a firm block draws a nearly full ring, a soft dissent a short one. The dark tick marks when the stance last changed. Hover a dial for its stance history this run.">
            {displayAgents.map((agent, index) => (
              <OfficeDial agent={agent} index={index} cockpit={cockpit} runStatus={task?.status} key={agent.id} />
            ))}
            {displayAgents.length === 0 && <div className="empty-state">No backend agents are available. The room will not use demo offices.</div>}
          </div>

          {lastActor && <p className="floor-note"><b>{displayIdentity(lastActor, displayAgents)}</b> has the floor</p>}

          <div className="feed" data-explain-title="Causal debate ledger" data-explain-text="Only turns that add a claim, challenge an assumption, return evidence, revise an artifact, or make a decision appear here. Routine transport and duplicate meeting messages are collapsed.">
            <div className="feed-intro"><strong>DEBATE LEDGER</strong><span>Time-ordered · support / propose / challenge / block / revision</span></div>
            <p className="ledger-subtitle">Exact speech, decisions, blockers, and evidence in time order.</p>
            {currentUnderstanding && <div className="current-understanding"><span aria-hidden="true">◆</span><span><b>Current understanding</b> · {currentUnderstanding}</span></div>}
            {events.length === 0 && !task && (
              <div className="empty-state">No task is running yet. Start a run from Intake to populate the room with run updates.</div>
            )}
            {ledgerEvents.map((event, index) => (event.type === "conversation_turn" || event.type === "user_clarification_requested" || event.type === "user_clarification_answered")
              ? <ConversationTurnCard agents={displayAgents} event={event} index={index} key={event.id} opinion={opinionByAgentAndRound.get(`${event.actor ?? asString(event.payload.agent_id) ?? "unknown"}:${String(event.payload.round ?? "")}`) ?? null} />
              : typedEventTypes.has(event.type)
                ? <TypedTurn agents={displayAgents} event={event} index={index} key={event.id} />
                : <LedgerSystemCard event={event} key={event.id} />
            )}
            {task && task.status === "running" && (
              <div className="typing">
                <span className="orb st" style={{ width: 26, height: 26 }}></span>
                Society is deliberating. Waiting for the next update…
              </div>
            )}
          </div>
        </div>

        <div>
          <LivingBriefCard brief={latestWorkingBrief as Record<string, unknown> | null} prompt={task?.prompt ?? prompt} />
          <DelegationCard assignments={latestAssignments as Record<string, unknown> | null} />
          <ArtifactsCard events={events} />

          {cockpit?.demo_proof && (
            <div className={`card side-card ${cockpit.demo_proof.verified ? "" : "proof-incomplete"}`}>
              <div className="card-head">
                <span className="card-title"><span className="ic">◈</span>DEMO PROOF</span>
                <span className="right"><span className="ver">{cockpit.demo_proof.verified ? "VERIFIED" : "GAPS VISIBLE"}</span></span>
              </div>
              <div className="card-body proof-list">
                {Object.entries(cockpit.demo_proof.markers).map(([marker, present]) => (
                  <div className="drow" key={marker}>
                    <span>{present ? "✓" : "–"} {marker.replaceAll("_", " ")}</span>
                    <span className={`pillstat ${present ? "done" : "progress"}`}>{present ? "OBSERVED" : "MISSING"}</span>
                  </div>
                ))}
                {!cockpit.demo_proof.verified && <p className="hint">No completion claim is made for missing or incomplete proof points.</p>}
              </div>
            </div>
          )}

          {cockpit && cockpit.participants.length > 0 && (
            <div className="card side-card">
              <div className="card-head">
                <span className="card-title"><span className="ic">◎</span>PARTICIPANTS</span>
                <span className="right"><span className="ver">{cockpit.participants.length} active</span></span>
              </div>
              {cockpit.participants.map((p) => (
                <button
                  className="task-btn"
                  type="button"
                  key={p.agent_id}
                  style={{ alignItems: "center" }}
                >
                  <span className={`orb ${officeForIndex(agents.findIndex((a) => a.id === p.agent_id))}`} style={{ width: 22, height: 22 }}></span>
                  <strong>{displayIdentity(p.agent_id, agents)}{p.is_leader ? " ★" : ""}</strong>
                  <span>{p.current_stance}</span>
                </button>
              ))}
              {cockpit.delegation_summary.length > 0 && (
                <div style={{ borderTop: "1px solid var(--line)", padding: "10px 22px" }}>
                  <div className="kv">DELEGATION</div>
                  {cockpit.delegation_summary.map((d) => (
                    <div className="drow" key={d.subtask_id} style={{ marginBottom: 6 }}>
                      <span className="task">
                        <span className="t">{d.objective}</span>
                        <span className="o">{displayIdentity(d.agent_id, agents)}</span>
                      </span>
                      <span className={`pillstat ${d.status === "done" || d.status === "complete" ? "done" : "progress"}`}>{d.status.toUpperCase()}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {cockpit && cockpit.tool_usage_summary.length > 0 && (
            <div className="card side-card">
              <div className="card-head">
                <span className="card-title"><span className="ic">⚙</span>TOOL USAGE</span>
                <span className="right"><span className="ver">{cockpit.tool_usage_summary.length} calls</span></span>
              </div>
              {cockpit.tool_usage_summary.slice(-8).map((tool) => (
                <div className="memory-item" key={tool.source_event_id}>
                  <span className="badge">{tool.tool_name}</span>
                  <span style={{ fontSize: 10, color: "var(--faint)" }}>{displayIdentity(tool.agent_id, agents)}</span>
                  {tool.why_used && <p>Why: {tool.why_used}</p>}
                  {tool.result_summary && <p>Result: {tool.result_summary}</p>}
                </div>
              ))}
            </div>
          )}

          <div className="card side-card">
            <div className="card-head">
              <span className="card-title"><span className="ic">↻</span>REPLAY</span>
            </div>
            {replayTasks.map((item) => (
              <button className="task-btn" type="button" key={item.id} onClick={() => onReplay(item.id)}>
                <span>{item.status}<small>{item.created_at ? new Date(item.created_at).toLocaleString() : "Time unavailable"} · {item.id.slice(-8)}</small></span>
                <strong>{item.prompt}</strong>
              </button>
            ))}
          </div>

          <div className="card side-card">
            <div className="card-head">
              <span className="card-title"><span className="ic">≡</span>MEMORY</span>
              <span className="right"><span className="ver">{selectedAgentName ?? "all agents"}</span></span>
            </div>
            {agentMemory.length === 0 && <p className="hint" style={{ padding: "12px 22px" }}>Select an agent to inspect durable collaboration memory.</p>}
            {agentMemory.slice(-4).map((item, index) => (
              <div className="memory-item" key={`${item.task_id ?? "live"}-${index}`}>
                <ParsedMemory memory={item.memory} />
                <div className="capabilities" style={{ marginTop: 6 }}>
                  {(item.tags ?? []).map((tag) => <span className="badge" key={tag}>{tag}</span>)}
                </div>
              </div>
            ))}
          </div>

          <div className="card side-card">
            <div className="card-head">
              <span className="card-title"><span className="ic">◎</span>AGENT POOL</span>
            </div>
            {displayAgents.map((agent) => (
              <button
                className="task-btn"
                type="button"
                key={agent.id}
                onClick={() => setSelectedAgent(agent.id === selectedAgent ? null : agent.id)}
                style={{ alignItems: "center" }}
              >
                <span className={`orb ${officeForIndex(displayAgents.indexOf(agent))}`} style={{ width: 26, height: 26 }}></span>
                <strong>{agent.name}</strong>
                <span>{agent.role}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

export default function App() {
  const [page, setPage] = useHashPage();
  const [prompt, setPrompt] = useState("");
  const [agents, setAgents] = useState<Agent[]>([]);
  const [tasks, setTasks] = useState<TaskRun[]>([]);
  const [task, setTask] = useState<TaskRun | null>(null);
  const [events, setEvents] = useState<SocietyEvent[]>([]);
  const [cockpit, setCockpit] = useState<RunCockpit | null>(null);
  const [review, setReview] = useState<DecisionReview | null>(null);
  const [recap, setRecap] = useState<RunRecap | null>(null);
  const [artifacts, setArtifacts] = useState<TaskArtifact[]>([]);
  const [artifactsLoading, setArtifactsLoading] = useState(false);
  const [artifactsError, setArtifactsError] = useState<string | null>(null);
  const [dossier, setDossier] = useState<AgentDossier | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null);
  const [agentMemory, setAgentMemory] = useState<AgentMemory[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [openExplainer, setOpenExplainer] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listAgents().then((nextAgents) => {
      if (cancelled) return;
      setAgents(nextAgents);
      setSelectedAgent((current) => current ?? nextAgents[0]?.id ?? null);
    }).catch(() => {
      if (cancelled) return;
      setAgents([]);
      setError("Backend is offline or unavailable. Static navigation remains available; no society data is fabricated.");
    });
    listTasks().then((nextTasks) => {
      if (cancelled) return;
      setTasks(nextTasks);
      // Restore the most recently updated real run after a browser refresh.
      const latestTask = [...nextTasks].sort((a, b) => (Date.parse(b.updated_at ?? b.created_at ?? "") || 0) - (Date.parse(a.updated_at ?? a.created_at ?? "") || 0))[0] ?? null;
      setTask((current) => current ?? latestTask);
      // Hydrate the selected replay explicitly during bootstrap. Relying only
      // on the task-id effect can miss the state transition while React batches
      // the initial task list update, leaving a paused run at zero events.
      if (latestTask) {
        listTaskEvents(latestTask.id).then((nextEvents) => {
          if (!cancelled) {
            setEvents((current) => current.length === 0 ? nextEvents : current);
          }
        }).catch(() => undefined);
      }
    }).catch((err) => {
      if (cancelled) return;
      setError(runErrorMessage(err));
    });
    getHealth().then((nextHealth) => {
      if (!cancelled) setHealth(nextHealth);
    }).catch(() => {
      if (!cancelled) setHealth(null);
    });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!selectedAgent) {
      setAgentMemory([]);
      setDossier(null);
      return;
    }
    let cancelled = false;
    listAgentMemory(selectedAgent).then((memory) => {
      if (!cancelled) setAgentMemory(memory);
    }).catch(() => {
      if (!cancelled) setAgentMemory([]);
    });
    // Refetch on status transitions so the dossier reflects terminal-state truth
    // (e.g. waiting_for_user -> running -> failed/complete) rather than going stale.
    getAgentDossier(selectedAgent, task?.id ?? null).then((nextDossier) => {
      if (!cancelled) setDossier(nextDossier);
    }).catch(() => {
      if (!cancelled) setDossier(null);
    });
    return () => { cancelled = true; };
  }, [selectedAgent, task?.id, task?.status]);

  const refreshProjections = useCallback((taskId: string) => {
    Promise.all([
      getRunCockpit(taskId),
      getDecisionReview(taskId),
      getRunRecap(taskId)
    ]).then(([nextCockpit, nextReview, nextRecap]) => {
      setCockpit(nextCockpit);
      setReview(nextReview);
      setRecap(nextRecap);
    }).catch(() => {
      setCockpit(null);
      setReview(null);
      setRecap(null);
    });
  }, []);

  useEffect(() => {
    if (!task) {
      setCockpit(null);
      setReview(null);
      setRecap(null);
      return;
    }
    refreshProjections(task.id);
  }, [task, events.length, refreshProjections]);

  useEffect(() => {
    if (!task) {
      setArtifacts([]);
      setArtifactsError(null);
      setArtifactsLoading(false);
      return;
    }
    let cancelled = false;
    setArtifactsLoading(true);
    setArtifactsError(null);
    listTaskArtifacts(task.id).then((nextArtifacts) => {
      if (!cancelled) setArtifacts(nextArtifacts);
    }).catch((artifactError: unknown) => {
      if (!cancelled) {
        setArtifacts([]);
        setArtifactsError(artifactError instanceof Error ? artifactError.message : "The artifact record is unavailable.");
      }
    }).finally(() => {
      if (!cancelled) setArtifactsLoading(false);
    });
    return () => { cancelled = true; };
  }, [task?.id, events.length]);

  useEffect(() => {
    if (!task) {
      setEvents([]);
      return;
    }
    // Server-sent events only cover future activity; hydrate the selected run's
    // existing record so a refresh preserves the real timeline and projections.
    listTaskEvents(task.id).then((nextEvents) => {
      setEvents((current) => {
        const currentForTask = current.filter((e) => e.task_id === task.id);
        return mergeEvents(currentForTask, nextEvents);
      });
    }).catch(() => {});
  }, [task?.id]);

  useEffect(() => {
    if (!task || task.status === "waiting_for_user" || isTerminalStatus(task.status)) return;
    let cancelled = false;
    // EventSource starts at subscription time. Re-read the persisted timeline
    // while a run is active so a slow initial hydration cannot leave the Live
    // counter and ledger empty despite real events already having been stored.
    const hydratePersistedTimeline = () => {
      listTaskEvents(task.id).then((nextEvents) => {
        if (!cancelled) setEvents((current) => mergeEvents(current, nextEvents));
      }).catch(() => {});
    };
    hydratePersistedTimeline();
    const timer = window.setInterval(hydratePersistedTimeline, 3_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [task?.id, task?.status]);

  useEffect(() => {
    if (!task) return;
    if (task.status === "waiting_for_user" || isTerminalStatus(task.status)) return;
    const source = new EventSource(`${API_BASE}/tasks/${task.id}/stream`);
    let terminalEventSeen = false;
    let disposed = false;
    const appendEvent = (event: SocietyEvent) => {
      setEvents((current) => current.some((item) => item.id === event.id) ? current : [...current, event]);
    };
    // New event types are recovered via persisted hydration on reconnect, but
    // should be registered here for immediate SSE delivery without a poll gap.
    const eventTypes = [
      "task_received", "team_formed", "leader_elected", "child_agent_spawned",
      "no_spawn", "agent_negotiated", "proposal_challenged", "proposal_revised",
      "debate_round_completed", "negotiation_closed", "tool_call", "vote_cast",
      "ballots_tallied", "solution_selected", "workflow_checkpoint", "workflow_completed",
      "agno_team_ran", "peer_monitor_report", "learning_recorded", "reputation_updated",
      "task_metrics", "validation_gate_completed", "team_dissolved", "task_complete", "task_failed", "task_interrupted", "task_remediation", "error",
      "goal_discussion_started", "conversation_turn", "agent_goal_opinion", "readiness_vote_cast",
      "targeted_question_answered", "readiness_vote_tallied", "working_brief_finalized", "leader_election_started",
      "subtasks_assigned_from_brief", "agent_position_stated", "agent_objection_registered",
      "agent_endorsed_peer", "agent_changed_mind", "private_note_published",
      "agent_tool_bundle_selected", "agent_help_requested", "agent_deferred_ownership",
      "agent_joined_coalition", "trust_updated", "meeting_recap",
      "artifact_section_critiqued", "shared_artifact_revised", "personality_drifted",
      "failure_recovery_attempted", "user_clarification_requested",
      "user_clarification_answered", "user_decision_resolved", "society_resumed",
      "specialist_selection_proposed", "specialist_selection_rejected", "specialist_selection_accepted",
      "specialist_invocation_approved", "specialist_invocation_started", "composition_assignment_materialized",
      "work_node_started", "work_node_blocked", "work_node_failed", "work_node_retry_scheduled",
      "work_node_canceled", "work_node_completed", "artifact_validated",
      "composition_assignment_cleanup_completed", "composition_assignment_cleanup_warning",
      "agentbay_start_requested", "agentbay_start_succeeded", "agentbay_start_failed", "agentbay_start_rejected",
      "agentbay_command_requested", "agentbay_command_succeeded", "agentbay_command_failed", "agentbay_command_rejected",
      "agentbay_run_code_requested", "agentbay_run_code_succeeded", "agentbay_run_code_failed", "agentbay_run_code_rejected",
      "agentbay_artifact_export_requested", "agentbay_artifact_exported", "agentbay_artifact_export_failed",
      "local_independent_validation_reported"
    ];
    for (const type of eventTypes) {
      source.addEventListener(type, (message) => {
        const raw = (message as MessageEvent).data;
        if (!raw || raw === "undefined" || raw === "null") return;
        try {
          const parsed = JSON.parse(raw);
          if (
            parsed && typeof parsed === "object" &&
            typeof parsed.id === "string" &&
            typeof parsed.type === "string" &&
            typeof parsed.message === "string"
          ) {
            const event = parsed as SocietyEvent;
            appendEvent(event);
            if (TERMINAL_EVENT_TYPES.has(event.type)) {
              terminalEventSeen = true;
              source.close();
              void getTask(task.id).then((nextTask) => {
                setTask((current) => current?.id === task.id ? nextTask : current);
                setTasks((current) => [nextTask, ...current.filter((item) => item.id !== nextTask.id)]);
              }).catch(() => undefined).finally(() => refreshProjections(task.id));
            }
          }
        } catch {
          // ignore non-JSON heartbeats
        }
      });
    }
    source.onerror = () => {
      // A source from a replayed-away task can emit its close error after cleanup.
      // It must not overwrite the newly selected run's truthful status.
      if (terminalEventSeen || disposed) return;
      setError("The live event stream stopped. The task status will keep polling.");
      source.close();
    };
    return () => {
      disposed = true;
      source.close();
    };
    // task?.status is required: waiting_for_user closes the server stream, so
    // the transition back to running must tear down and reconnect EventSource.
  }, [task?.id, task?.status]);

  useEffect(() => {
    if (!task || task.status === "waiting_for_user" || isTerminalStatus(task.status)) return;
    const timer = window.setInterval(() => {
      getTask(task.id).then((nextTask) => {
        setTask((current) => current?.id === task.id ? nextTask : current);
      }).catch(() => undefined);
      listAgents().then(setAgents).catch(() => undefined);
      listTasks().then(setTasks).catch(() => undefined);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [task]);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpenExplainer(null);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, []);

  useEffect(() => {
    document.title = page === "intake" ? "Quendom Agent Society" : `Quendom | ${page.charAt(0).toUpperCase() + page.slice(1)}`;
  }, [page]);

  const openTour = useCallback(() => {
    setPage("intake");
    setOpenExplainer("intake-form");
  }, [setPage]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (isSubmitting) return;
    const validationError = validateMissionPrompt(prompt);
    if (validationError) {
      setError(validationError);
      return;
    }
    setError(null);
    setEvents([]);
    setIsSubmitting(true);
    const nextTask = await createTask(prompt).catch((err) => {
      setError(runErrorMessage(err));
      return null;
    }).finally(() => setIsSubmitting(false));
    if (!nextTask) return;
    setTask(nextTask);
    setTasks((current) => [nextTask, ...current.filter((item) => item.id !== nextTask.id)]);
    setCockpit(null);
    setReview(null);
    setRecap(null);
    setError(null);
    refreshProjections(nextTask.id);
    setPage("live");
  }

  async function intakeSubmit(combinedPrompt: string) {
    if (isSubmitting) return;
    setError(null);
    setEvents([]);
    setPrompt(combinedPrompt);
    setIsSubmitting(true);
    const nextTask = await createTask(combinedPrompt).catch((err) => {
      setError(runErrorMessage(err));
      return null;
    }).finally(() => setIsSubmitting(false));
    if (!nextTask) return;
    setTask(nextTask);
    setTasks((current) => [nextTask, ...current.filter((item) => item.id !== nextTask.id)]);
    setCockpit(null);
    setReview(null);
    setRecap(null);
    setError(null);
    refreshProjections(nextTask.id);
    setPage("live");
  }

  async function replayTask(taskId: string) {
    setError(null);
    const [nextTask, nextEvents] = await Promise.all([getTask(taskId), listTaskEvents(taskId)]);
    setTask(nextTask);
    setEvents(nextEvents);
    setCockpit(null);
    setReview(null);
    setRecap(null);
    setError(null);
    refreshProjections(taskId);
  }

  return (
    <>
      <Rail status={page === "intake" ? "ready" : task?.status ?? "ready"} health={health} currentPage={page} onNavigate={setPage} onTour={openTour} />
      <main className="main" onClick={(event) => {
        if (!(event.target as Element).closest(".has-exp")) setOpenExplainer(null);
      }}>
        <div className="wrap">
          {page === "intake" && (
            <IntakePage
              onSubmit={intakeSubmit}
              error={error}
              openExplainer={openExplainer}
              setOpenExplainer={setOpenExplainer}
            />
          )}
          {page === "live" && (
            <LiveRunPage
              prompt={prompt}
              setPrompt={setPrompt}
              task={task}
              cockpit={cockpit}
              tasks={tasks}
              events={events}
              agents={agents}
              error={error}
              selectedAgent={selectedAgent}
              setSelectedAgent={setSelectedAgent}
              agentMemory={agentMemory}
              openExplainer={openExplainer}
              setOpenExplainer={setOpenExplainer}
              onSubmit={submit}
              onReplay={replayTask}
              isSubmitting={isSubmitting}
            />
          )}
          {page === "review" && (
            <ReviewPage
              task={task}
              review={review}
              openExplainer={openExplainer}
              setOpenExplainer={setOpenExplainer}
            />
          )}
          {page === "recap" && (
            <RecapPage
              task={task}
              recap={recap}
              events={events}
              openExplainer={openExplainer}
              setOpenExplainer={setOpenExplainer}
            />
          )}
          {page === "artifacts" && (
            <ArtifactPage task={task} artifacts={artifacts} loading={artifactsLoading} error={artifactsError} />
          )}
          {page === "dossier" && (
            <DossierPage
              agents={agents}
              selectedAgent={selectedAgent}
              setSelectedAgent={setSelectedAgent}
              dossier={dossier}
              taskId={task?.id ?? null}
              openExplainer={openExplainer}
              setOpenExplainer={setOpenExplainer}
            />
          )}
          {page === "benchmark" && <BenchmarkPage />}
          {page === "architecture" && <ArchitecturePage />}
        </div>
      </main>

      <nav className="bottomnav" aria-label="Screens">
        {PAGE_KEYS.map((key) => (
          <a
            data-nav={key}
            aria-current={key === page ? "page" : undefined}
            href={`#${key}`}
            key={key}
            onClick={(e) => { e.preventDefault(); setPage(key); }}
          >
            {key === "dossier" ? "Dossier" : key.charAt(0).toUpperCase() + key.slice(1)}
          </a>
        ))}
      </nav>
    </>
  );
}
