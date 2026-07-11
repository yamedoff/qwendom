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
  TaskRun,
  createTask,
  getAgentDossier,
  getDecisionReview,
  getHealth,
  getRunCockpit,
  getRunRecap,
  getTask,
  listAgentMemory,
  listAgents,
  listTaskEvents,
  listTasks,
  submitClarification
} from "./api";
import "../../qwendom-v42-source/core.css";
import "./styles.css";

type PageKey = "intake" | "live" | "review" | "recap" | "dossier";

const PAGE_KEYS: PageKey[] = ["intake", "live", "review", "recap", "dossier"];
const PAGE_ALIASES: Record<string, PageKey> = {
  dossiers: "dossier",
};
const MIN_PROMPT_LENGTH = 8;

function validateMissionPrompt(value: string): string | null {
  if (value.trim().length >= MIN_PROMPT_LENGTH) return null;
  return "Add a short mission before convening the society.";
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
    return PAGE_ALIASES[h] ?? "live";
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

const asString = (value: unknown): string | null =>
  typeof value === "string" && value.length > 0 ? value : null;
const asStringArray = (value: unknown): string[] =>
  Array.isArray(value) ? value.map((item) => String(item)).filter((item) => item.length > 0) : [];
const asNumber = (value: unknown): number | null => (typeof value === "number" && Number.isFinite(value) ? value : null);
const asBoolean = (value: unknown): boolean | null => (typeof value === "boolean" ? value : null);
const asRecord = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;

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

const officeLabelForAgent = (agent: Agent, index: number) => {
  const role = roleKeyOf(agent);
  if (role === "architect") return "Strategist";
  if (role === "researcher") return "Archivist";
  if (role === "critic") return "Risk Officer";
  if (agent.profile.risk_tolerance === "high") return "Critic";
  return index === 0 ? "Strategist" : "Builder";
};

function Rail({ status, health, currentPage, onNavigate, onTour }: { status: string; health: Health | null; currentPage: PageKey; onNavigate: (key: PageKey) => void; onTour: () => void }) {
  const navItems: { key: PageKey; label: string }[] = [
    { key: "intake", label: "Intake" },
    { key: "live", label: "Live run" },
    { key: "review", label: "Review" },
    { key: "recap", label: "Recap" },
    { key: "dossier", label: "Dossier" }
  ];
  return (
    <>
      <aside className="rail">
        <div className="glyph"></div>
        <div className="wordmark">Qwendom</div>
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
        <div><div className="wordmark">Qwendom</div></div>
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

function OfficeDial({ agent, index, cockpit }: { agent: Agent; index: number; cockpit?: RunCockpit | null }) {
  const stance = stanceForAgent(agent, cockpit);
  const office = officeForIndex(index);
  const label = officeLabelForAgent(agent, index);
  const isLeader = cockpit?.participants.some((p) => p.agent_id === agent.id && p.is_leader) ?? index === 0;
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
      <div className="name">{label}</div>
      {isLeader && <div className="lead">LEAD</div>}
      <span className={`chip ${stance}`}>{stance.toUpperCase()}</span><span className="since">live</span>
      <div className="dial-hist">
        <div className="dh-label">STANCE HISTORY</div>
        <div className="dh-row"><span>{agent.profile.communication_style}</span><span className="t">now</span></div>
        <div className="dh-row"><span>{agent.profile.risk_tolerance} risk</span><span className="t">profile</span></div>
      </div>
    </div>
  );
}

function typedTurnClass(event: SocietyEvent) {
  if (event.type === "agent_objection_registered") return asBoolean(event.payload.blocks_execution) ? "block" : "challenge";
  if (event.type === "agent_changed_mind") return "concede";
  if (event.type === "agent_position_stated") {
    const stance = asString(event.payload.stance);
    if (stance?.includes("challenge")) return "challenge";
    if (stance?.includes("block")) return "block";
    return "propose";
  }
  if (event.type === "conversation_turn") return "direct";
  return "propose";
}

function TypedTurn({ event, index, agents }: { event: SocietyEvent; index: number; agents: Agent[] }) {
  const turnType = typedTurnClass(event);
  const actor = event.actor ?? asString(event.payload.agent_id) ?? asString(event.payload.actor) ?? `office-${index + 1}`;
  const office = officeForIndex(index);
  const reason = asString(event.payload.reason) ?? asString(event.payload.critique) ?? asString(event.payload.says);
  const delegateTargets = asRecord(event.payload.delegates) ?? asRecord(event.payload.delegation);
  return (
    <div className={`turn ${turnType}`}>
      <div className="turn-top">
        <span className={`orb ${office}`}></span>
        <span className="name">{actor}</span>
        <span className="role">{event.type.replaceAll("_", " ")}</span>
        <span className={`ttag ${turnType}`}>{turnType.toUpperCase()}</span>
        <span className="time">{new Date(event.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
      </div>
      <div className="tx">{event.message}</div>
      {turnType === "direct" && (delegateTargets || agents.length > 1) && (
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
      {reason && turnType !== "direct" && <div className="t-anchor">↳ {reason}</div>}
      {turnType === "block" && (
        <>
          <div className="t-struct t-violation"><span className="lbl">VIOLATED CONSTRAINT</span><span className="cid">{asString(event.payload.constraint_id) ?? "C-?"}</span>{asString(event.payload.target) ?? "Execution path blocked until risk is resolved."}</div>
          <div className="t-struct t-lift"><span className="lbl">CONDITION TO LIFT</span>{asString(event.payload.resolution_condition) ?? asString(event.payload.condition) ?? "A responsible office must revise the proposal and satisfy the blocker."}</div>
        </>
      )}
      {turnType === "concede" && (
        <div className="t-struct t-flip"><span className="lbl">WHAT CHANGED</span><span className="from">{asString(event.payload.previous_position) ?? "Previous stance"}</span><span className="arr">→</span><span className="to">{asString(event.payload.new_position) ?? "Updated stance"}</span></div>
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
  openExplainer,
  setOpenExplainer
}: {
  onSubmit: (prompt: string) => void;
  openExplainer: string | null;
  setOpenExplainer: (id: string | null) => void;
}) {
  const [title, setTitle] = useState("");
  const [scope, setScope] = useState("");
  const [constraints, setConstraints] = useState("");
  const [criteria, setCriteria] = useState("");

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const combined = `${title}\n\nScope: ${scope}\n\nConstraints:\n${constraints}\n\nSuccess criteria: ${criteria}`;
    onSubmit(combined);
  }

  return (
    <section className="view">
      <div className="kicker">NEW MISSION</div>
      <h1 className="hero-title">Brief the society.</h1>
      <p className="hero-sub">A task force convenes around this brief, aligns on the goal, and decides whether it is ready to begin.</p>

      <form className="intake-wrap" onSubmit={handleSubmit}>
        <Explainer id="intake-form" title="Numbered brief form" text="Mission, scope, constraints, and success criteria as numbered sections. The society uses this brief as the starting mission context and revises it only when the run emits supporting events." openId={openExplainer} setOpenId={setOpenExplainer}>
          <div className="field">
            <span className="fnum">01</span>
            <label htmlFor="f-title">Mission</label>
            <input className="input big" id="f-title" value={title} onChange={(e) => setTitle(e.target.value)} />
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

function EvidenceNotice({ status, missing }: { status: string; missing: string[] }) {
  if (status === "complete") return null;
  return (
    <div className="error">
      Evidence is {status}. Missing sources: {missing.length > 0 ? missing.join(", ") : "none reported"}.
    </div>
  );
}

function ReviewPage({
  task,
  review,
  openExplainer,
  setOpenExplainer
}: {
  task: TaskRun | null;
  review: DecisionReview | null;
  events: SocietyEvent[];
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
  const hasBlocking = review.blocking_objections.length > 0;
  const hasNonBlocking = review.non_blocking_dissent.length > 0;
  const hasUnresolved = review.unresolved_dissent.length > 0;
  const isRunning = task.status === "running";
  const failedBeforeProposals = task.status === "failed" && review.proposals.length === 0;

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
          <strong>BLOCKING OBJECTIONS ({review.blocking_objections.length})</strong>
          {review.blocking_objections.map((obj, i) => <div key={`bo-${i}`}>{obj}</div>)}
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
        <div className="bignum"><div className="n">{String(review.unresolved_dissent.length).padStart(2, "0")}</div><div className="l">Unresolved dissent</div></div>
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
                {review.unresolved_dissent.map((item, i) => <div className="dissent-note" key={`ud-${i}`}>{item}</div>)}
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
  const outcomeLabel = recap.completion_outcome === "complete" ? "COMPLETE" : recap.completion_outcome === "failed" ? "FAILED" : recap.completion_outcome === "waiting_for_user" ? "WAITING" : "PARTIAL";
  const isRunning = task.status === "running";
  const recapTitle = isRunning ? "Run in progress" : "Run recap";
  const recapKicker = isRunning ? "RUN STATE" : "AFTER-ACTION";

  return (
    <section className="view">
      <div className="kicker">{recapKicker} · <em>{recap.evidence_status.toUpperCase()}</em> · {outcomeLabel}</div>
      <h1 className="hero-title">{recapTitle}</h1>
      <p className="hero-sub">
        {isRunning
          ? `This run is still active. The recap is intentionally partial and only reflects events emitted so far. Status: ${recap.status}.`
          : recap.completion_outcome === "complete"
          ? "Projected from completed society events."
          : recap.completion_outcome === "failed"
            ? "This run did not complete. See failure details below."
            : `Partial recap from actual events. Status: ${recap.status}.`}
      </p>
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
        <div className="bignum"><div className="n">{String(recap.dissents.length).padStart(2, "0")}</div><div className="l">Dissents raised</div></div>
        <div className="bignum-sep"></div>
        <div className="bignum"><div className="n">{String(recap.mind_changes.length).padStart(2, "0")}</div><div className="l">Mind changed</div></div>
        <div className="bignum-sep"></div>
        <div className="bignum"><div className="n">{String(recap.saved_lessons.length).padStart(2, "0")}</div><div className="l">Lessons saved</div></div>
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
            {recap.saved_lessons.length === 0 && recap.dissents.length === 0 && <p className="empty-state">No saved lessons or carried dissent have been emitted.</p>}
            {recap.saved_lessons.map((item, index) => <div className="lesson" key={`lesson-${index}`}><span className="lid">LESSON</span><p>{item}</p></div>)}
            {recap.dissents.map((item, index) => <div className="lesson dnote" key={`dissent-${index}`}><span className="lid">DISSENT</span><p>{item}</p></div>)}
          </div>
        </div>
        <div className="card">
          <div className="card-head"><span className="card-title"><span className="ic">◈</span>TRUST & REPUTATION</span></div>
          <div className="card-body">
            {recap.trust_reputation_changes.length === 0 && <p className="empty-state">No trust or reputation change events have been emitted.</p>}
            {recap.trust_reputation_changes.map((item, index) => (
              <div className="point" key={`tr-${index}`}>{item.type?.toString().replaceAll("_", " ") ?? "change"}: {JSON.stringify(item.payload ?? {}).slice(0, 200)}</div>
            ))}
          </div>
        </div>
      </div>

      {recap.social_deltas.length > 0 && (
        <div className="card" style={{ marginTop: 18 }}>
          <div className="card-head"><span className="card-title"><span className="ic">↕</span>SOCIAL DELTAS</span></div>
          <div className="card-body">
            {recap.social_deltas.map((item, index) => (
              <div className="point" key={`sd-${index}`}>{JSON.stringify(item).slice(0, 240)}</div>
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
          <div className="card-body"><p>{recap.final_answer}</p></div>
        </div>
      )}
    </section>
  );
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
                {dossier.run_tool_usage.map((tool) => (
                  <div className="point" key={tool.source_event_id}>
                    <strong>{tool.tool_name}</strong> [{tool.phase ?? "?"}]
                    {tool.why_used && <div className="hint">Why: {tool.why_used}</div>}
                    {tool.result_summary && <div className="hint">Result: {tool.result_summary}</div>}
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
              {dossier?.trust.map((item, index) => <div className="point" key={`trust-${index}`}>{JSON.stringify(item.payload)}</div>)}
            </div>
          </div>
        </div>
      </div>
    </section>
  );

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
  clarificationAnswer,
  setClarificationAnswer,
  selectedAgent,
  setSelectedAgent,
  agentMemory,
  openExplainer,
  setOpenExplainer,
  onSubmit,
  onReplay,
  onAnswerClarification,
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
  clarificationAnswer: string;
  setClarificationAnswer: (v: string) => void;
  selectedAgent: string | null;
  setSelectedAgent: (v: string | null) => void;
  agentMemory: AgentMemory[];
  openExplainer: string | null;
  setOpenExplainer: (id: string | null) => void;
  onSubmit: (e: FormEvent) => void;
  onReplay: (id: string) => void;
  onAnswerClarification: (e: FormEvent) => void;
  isSubmitting: boolean;
}) {
  const [heartbeatNow, setHeartbeatNow] = useState(Date.now());
  const displayAgents = agents;
  const lastActor = events.length > 0 ? (events[events.length - 1].actor ?? asString(events[events.length - 1].payload.agent_id) ?? null) : null;
  const typedEventTypes = new Set([
    "conversation_turn",
    "agent_position_stated",
    "agent_objection_registered",
    "agent_changed_mind",
    "agent_goal_opinion",
    "private_note_published"
  ]);
  const typedEvents = events.filter((event) => typedEventTypes.has(event.type));
  const systemEventTypes = new Set([
    "leader_elected",
    "leader_election_started",
    "readiness_vote_tallied",
    "working_brief_finalized",
    "subtasks_assigned_from_brief"
  ]);
  const systemEvents = events.filter((event) => systemEventTypes.has(event.type));

  const latestWorkingBrief = useMemo(() => (
    events.findLast((event) => event.type === "working_brief_finalized")?.payload ?? null
  ), [events]);
  const latestAssignments = useMemo(() => (
    events.findLast((event) => event.type === "subtasks_assigned_from_brief")?.payload ?? null
  ), [events]);
  const latestClarificationRequest = useMemo(() => (
    events.findLast((event) => event.type === "user_clarification_requested")?.payload ?? null
  ), [events]);

  const timelineEvents = useMemo(() => events.filter(isTimelineEvent), [events]);
  const currentPhase = cockpit?.current_phase && phaseOrder.some((item) => item.key === cockpit.current_phase)
    ? cockpit.current_phase as PhaseKey
    : task?.status === "complete" || task?.status === "failed"
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
          ? "The society is waiting for clarification."
          : task.status === "complete"
            ? "Run complete."
            : "Run failed. See the failure details below.";

  useEffect(() => {
    if (!isRunActive) return;
    const timer = window.setInterval(() => setHeartbeatNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [isRunActive]);

  return (
    <section className="view">
      <div className="kicker">
        MISSION · <em>{(task?.status ?? "intake").toUpperCase()}{currentPhaseConfig ? ` · ${currentPhaseConfig.label.toUpperCase()}` : ""}</em>
      </div>
      <h1 className="hero-title">{task?.prompt ?? "Convene the society."}</h1>
      <p className="hero-sub">
        {latestWorkingBrief
          ? asString(latestWorkingBrief.summary) ?? "The brief is ratified and work is underway."
          : task
            ? "The society is convened and deliberating."
            : "Submit a problem to convene the society."}
      </p>

      <form className="composer" onSubmit={onSubmit}>
        <textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="Describe the mission..." />
        <button type="submit" disabled={isSubmitting || task?.status === "queued" || task?.status === "running"}>
          <Send size={18} /> {isSubmitting ? "Convening..." : task?.status === "queued" || task?.status === "running" ? "Run in progress" : "Convene society"}
        </button>
      </form>

      {task?.status === "waiting_for_user" && latestClarificationRequest && (
        <form className="clarificationPanel" onSubmit={onAnswerClarification}>
          <p><strong>Clarification needed:</strong> {asString(latestClarificationRequest.question) ?? "The society needs more detail."}</p>
          <textarea value={clarificationAnswer} onChange={(event) => setClarificationAnswer(event.target.value)} placeholder="Answer the society's blocker..." />
          <button type="submit" disabled={!clarificationAnswer.trim()}><Send size={18} /> Resume society</button>
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

      {cockpit && cockpit.trace_status !== "live" && !cockpit.failure && (
        <div className="error" style={{ marginBottom: 12 }}>
          Trace status: {cockpit.trace_status.toUpperCase()}{cockpit.stale_reason ? ` — ${cockpit.stale_reason}` : ""}
        </div>
      )}

      {cockpit?.leader_rationale && (
        <div className="card" style={{ marginBottom: 12 }}>
          <div className="card-head"><span className="card-title"><span className="ic">◈</span>LEADER RATIONALE</span></div>
          <div className="card-body"><p>{cockpit.leader_rationale}</p></div>
        </div>
      )}

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
          <div className="n">{String(cockpit?.blockers.length ?? 0).padStart(2, "0")}</div>
          <div className="l">Active blockers</div>
        </div>
      </div>

      <div className={`run-heartbeat ${isRunActive ? "active" : ""}`} role="status" aria-live="polite">
        <span className="pulse"></span>
        <span>{activityLabel}</span>
        {secondsSinceLastEvent !== null && <strong>Last event {secondsSinceLastEvent}s ago</strong>}
      </div>

      <DotStrip currentPhaseIndex={Math.max(0, currentPhaseIndex)} cockpit={cockpit} />

      <div className="sect"><span className="ic">◎</span>THE ROOM</div>

      <div className="workspace">
        <div>
          <div className="dials" data-explain-title="Stance dials" data-explain-text="Each office's live position. The arc shows stance strength — a firm block draws a nearly full ring, a soft dissent a short one. The dark tick marks when the stance last changed. Hover a dial for its stance history this run.">
            {displayAgents.map((agent, index) => (
              <OfficeDial agent={agent} index={index} cockpit={cockpit} key={agent.id} />
            ))}
            {displayAgents.length === 0 && <div className="empty-state">No backend agents are available. The room will not use demo offices.</div>}
          </div>

          {lastActor && <p className="floor-note"><b>{lastActor}</b> has the floor</p>}

          <div className="feed" data-explain-title="Typed turn cards" data-explain-text="Turns are typed by intent — direct, propose, challenge, block, concede — and each type has its own structure: a block turn shows the violated constraint and the condition to lift it; a concede turn shows exactly what changed the mind. Ruled lines record system events.">
            {events.length === 0 && !task && (
              <div className="empty-state">No task is running yet. Start a run from Intake to populate the room with real events.</div>
            )}
            {systemEvents.map((event) => (
              <div className="t-event" key={event.id}>
                <span>{event.message} · {new Date(event.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
              </div>
            ))}
            {typedEvents.map((event, index) => (
              <TypedTurn agents={displayAgents} event={event} index={index} key={event.id} />
            ))}
            {task && task.status === "running" && (
              <div className="typing">
                <span className="orb st" style={{ width: 26, height: 26 }}></span>
                Society is deliberating. Waiting for the next real event…
              </div>
            )}
          </div>
        </div>

        <div>
          <LivingBriefCard brief={latestWorkingBrief as Record<string, unknown> | null} prompt={task?.prompt ?? prompt} />
          <DelegationCard assignments={latestAssignments as Record<string, unknown> | null} />
          <ArtifactsCard events={events} />

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
                  <strong>{p.agent_id}{p.is_leader ? " ★" : ""}</strong>
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
                        <span className="o">{d.agent_id}</span>
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
                  <span style={{ fontSize: 10, color: "var(--faint)" }}>{tool.agent_id}</span>
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
            {tasks.slice(0, 8).map((item) => (
              <button className="task-btn" type="button" key={item.id} onClick={() => onReplay(item.id)}>
                <span>{item.status}</span>
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
  const [dossier, setDossier] = useState<AgentDossier | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null);
  const [agentMemory, setAgentMemory] = useState<AgentMemory[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [clarificationAnswer, setClarificationAnswer] = useState("");
  const [openExplainer, setOpenExplainer] = useState<string | null>(null);

  useEffect(() => {
    listAgents().then((nextAgents) => {
      setAgents(nextAgents);
      setSelectedAgent((current) => current ?? nextAgents[0]?.id ?? null);
    }).catch(() => {
      setAgents([]);
      setError("Backend is offline or unavailable. Static navigation remains available; no society data is fabricated.");
    });
    listTasks().then(setTasks).catch(() => setTasks([]));
    getHealth().then(setHealth).catch(() => setHealth(null));
  }, []);

  useEffect(() => {
    if (!selectedAgent) {
      setAgentMemory([]);
      return;
    }
    listAgentMemory(selectedAgent).then(setAgentMemory).catch(() => setAgentMemory([]));
    getAgentDossier(selectedAgent, task?.id ?? null).then(setDossier).catch(() => setDossier(null));
  }, [selectedAgent, task?.id]);

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
    if (!task) return;
    if (task.status === "complete" || task.status === "failed") return;
    const source = new EventSource(`${API_BASE}/tasks/${task.id}/stream`);
    let terminalEventSeen = false;
    const appendEvent = (event: SocietyEvent) => {
      setEvents((current) => current.some((item) => item.id === event.id) ? current : [...current, event]);
    };
    const eventTypes = [
      "task_received", "team_formed", "leader_elected", "child_agent_spawned",
      "no_spawn", "agent_negotiated", "proposal_challenged", "proposal_revised",
      "debate_round_completed", "negotiation_closed", "tool_call", "vote_cast",
      "ballots_tallied", "solution_selected", "workflow_checkpoint", "workflow_completed",
      "agno_team_ran", "peer_monitor_report", "learning_recorded", "reputation_updated",
      "task_metrics", "validation_gate_completed", "team_dissolved", "task_complete", "task_failed", "error",
      "goal_discussion_started", "conversation_turn", "agent_goal_opinion", "readiness_vote_cast",
      "targeted_question_answered", "readiness_vote_tallied", "working_brief_finalized", "leader_election_started",
      "subtasks_assigned_from_brief", "agent_position_stated", "agent_objection_registered",
      "agent_endorsed_peer", "agent_changed_mind", "private_note_published",
      "agent_tool_bundle_selected", "agent_help_requested", "agent_deferred_ownership",
      "agent_joined_coalition", "trust_updated", "meeting_recap",
      "artifact_section_critiqued", "shared_artifact_revised", "personality_drifted",
      "failure_recovery_attempted", "user_clarification_requested",
      "user_clarification_answered", "society_resumed"
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
            if (event.type === "task_complete" || event.type === "task_failed") {
              terminalEventSeen = true;
              source.close();
            }
          }
        } catch {
          // ignore non-JSON heartbeats
        }
      });
    }
    source.onerror = () => {
      if (terminalEventSeen) return;
      setError("The live event stream stopped. The task status will keep polling.");
      source.close();
    };
    return () => source.close();
  }, [task]);

  useEffect(() => {
    if (!task || task.status === "waiting_for_user" || task.status === "complete" || task.status === "failed") return;
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

  const openTour = useCallback(() => {
    setPage("intake");
    setOpenExplainer("intake-form");
  }, [setPage]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (isSubmitting || (task && (task.status === "queued" || task.status === "running"))) return;
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
    refreshProjections(nextTask.id);
    setPage("live");
  }

  async function intakeSubmit(combinedPrompt: string) {
    if (isSubmitting || (task && (task.status === "queued" || task.status === "running"))) return;
    const validationError = validateMissionPrompt(combinedPrompt);
    if (validationError) {
      setError(validationError);
      setPage("live");
      return;
    }
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
    refreshProjections(nextTask.id);
    setPage("live");
  }

  async function replayTask(taskId: string) {
    setError(null);
    const [nextTask, nextEvents] = await Promise.all([getTask(taskId), listTaskEvents(taskId)]);
    setTask(nextTask);
    setEvents(nextEvents);
    refreshProjections(taskId);
  }

  async function answerClarification(event: FormEvent) {
    event.preventDefault();
    if (!task || !clarificationAnswer.trim()) return;
    setError(null);
    const resumedTask = await submitClarification(task.id, clarificationAnswer.trim());
    setTask(resumedTask);
    setClarificationAnswer("");
    const nextEvents = await listTaskEvents(task.id);
    setEvents(nextEvents);
    refreshProjections(task.id);
  }

  return (
    <>
      <Rail status={task?.status ?? "ready"} health={health} currentPage={page} onNavigate={setPage} onTour={openTour} />
      <main className="main" onClick={(event) => {
        if (!(event.target as Element).closest(".has-exp")) setOpenExplainer(null);
      }}>
        <div className="wrap">
          {page === "intake" && (
            <IntakePage
              onSubmit={intakeSubmit}
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
              clarificationAnswer={clarificationAnswer}
              setClarificationAnswer={setClarificationAnswer}
              selectedAgent={selectedAgent}
              setSelectedAgent={setSelectedAgent}
              agentMemory={agentMemory}
              openExplainer={openExplainer}
              setOpenExplainer={setOpenExplainer}
              onSubmit={submit}
              onReplay={replayTask}
              onAnswerClarification={answerClarification}
              isSubmitting={isSubmitting}
            />
          )}
          {page === "review" && (
            <ReviewPage
              task={task}
              review={review}
              events={events}
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
