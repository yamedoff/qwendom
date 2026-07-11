# Remaining Human Behavior Gaps

Qwendom now mimics human work behavior better than it mimics natural human
conversation. The current system has identities, profiles, dissent, private
notes, social trace, trust movement, role-specific tools, and reusable social
lessons. The next product frontier is making the society feel like a real
working group in a room, not only a structured workflow with social events.

## 1. Real Conversational Transcript

The app needs a dedicated meeting-room transcript where agents speak in short
turns, reply to exact prior statements, and build a readable dialogue.

Implementation status: first slice implemented. The backend now emits typed
`conversation_turn` events during goal discussion, stores them in
`conversation_transcript`, and the frontend renders a Meeting Room transcript
above the raw timeline.

Current state:

- agents emit goal opinions and structured social events,
- the UI shows event cards and a society behavior summary,
- the raw experience still reads more like workflow telemetry than conversation.

Target:

- each agent turn should look like something a teammate would say,
- replies should quote or reference the prior speaker's specific point,
- users should be able to scan "what happened in the room" without opening raw
  tool payloads.

## 2. Dynamic Follow-Up Questions

Agents can raise blockers, but they do not yet ask targeted follow-up questions
and wait for another agent's answer before proceeding.

Implementation status: implemented as a bounded first slice. The
pre-execution conversation now records one typed targeted question-answer
exchange before readiness voting and carries the result into the working brief.

Target:

- agents ask another named agent a concrete question,
- the target agent answers before the workflow advances,
- unresolved questions either become blockers or assumptions.

## 3. Less Derived Social Behavior

Some social behavior is currently inferred from workflow events.

Examples:

- a vote creates a coalition signal,
- leader election creates deferral signals,
- assignment creates a help-request signal.

These are useful product signals, but the stronger version is for agents to
explicitly choose those moves.

Target:

- agents explicitly say "I defer to Ada on system shape",
- agents explicitly ask "Lin, can you own the first implementation check?",
- agents explicitly join or refuse a proposal coalition with a reason.

## 4. Persistent Personality Drift

Profiles are stable, but agents do not yet evolve much from experience.

Implementation status: first slice implemented. Collaboration learning now
emits `personality_drifted` events and mutates profile behavior from observed
signals such as valid blockers, mind changes, trusted domains, and delivery
penalties.

Target:

- an agent that repeatedly raises valid blockers becomes more assertive in
  review phases,
- an agent that over-blocks becomes more careful about offering mitigations,
- an agent trusted for a domain becomes more likely to lead that domain,
- an agent that misses risks becomes more cautious next time.

## 5. Deeper Conflict Resolution

Objections exist, but debate is still shallow.

Target:

- multi-round disagreement,
- counterproposals,
- concessions,
- unresolved dissent carried into the final answer,
- explicit "we disagree, but we proceed under this assumption" moments.

## 6. Actual Collaboration On Artifacts

Agents produce and judge proposals, but they do not deeply co-edit one shared
artifact.

Implementation status: first slice implemented. Debate revisions now emit
section-level artifact critique and shared revision events that name the
reviewer, editor, artifact section, critique, and change.

Target:

- one shared plan or document moves through revisions,
- each agent owns parts of the artifact,
- critiques attach to specific sections,
- final output shows what changed because of whom.

## 7. Better Failure Behavior

If a model or tool call fails, the society can still fail too abruptly.

Implementation status: first slice implemented. Failures now emit a
`failure_recovery_attempted` event with the failed actor, recovery owner,
phase, and safe-to-continue decision before the task failure is surfaced.

Target:

- reassign the failed step,
- ask another agent to recover,
- continue with caveats when safe,
- ask the user for clarification when the blocker is genuinely external.

## 8. Richer UI Replay

The UI has a useful social summary and event timeline, but it does not yet
produce a polished narrative of the collaboration.

Implementation status: first slice implemented. The backend emits
`meeting_recap`, and the frontend renders meeting recap, targeted Q&A,
artifact revision, personality drift, and recovery events.

Target:

- a "meeting recap" view,
- who influenced whom,
- where the plan changed,
- what dissent remained,
- which lessons were saved for next time.

## 9. User-In-The-Loop Moments

Humans should be pulled in when ambiguity is real.

Current state:

- the society can block on readiness,
- readiness blockers now become user-facing clarification prompts,
- user answers are injected back into the working brief,
- the society resumes from the paused readiness phase instead of restarting.

Implementation status: first slice implemented. Tasks can now enter
`waiting_for_user`, emit `user_clarification_requested`, accept
`POST /tasks/{task_id}/clarifications`, emit `user_clarification_answered` and
`society_resumed`, then continue the existing team/session in the background.

Target:

- readiness blockers become user-facing clarification prompts,
- user answers are injected back into the working brief,
- the society resumes from the blocked phase instead of restarting.

## 10. Empirical Quality Evaluation

The code now has preflights, including a behavior preflight, but there is not
yet a benchmark that scores human-like collaboration across many prompts.

Implementation status: first slice implemented. The backend now includes
`society.human_behavior_benchmark`, a small fixture suite that scores
transcript, targeted Q&A, dissent preservation, artifact revision, recap, and
profile drift signals.

Target:

- a small fixture suite of realistic tasks,
- assertions for social events, transcript quality, dissent, recovery, and
  memory quality,
- regression scores for "human-like work behavior" over time.

## Product Priority

The highest-impact next slice is the meeting-room transcript:

1. add a typed `conversation_turn` artifact, done,
2. render it as a transcript above the raw event stream, done,
3. require each turn to respond to a specific prior turn, done for generated
   transcript turns,
4. allow one targeted question and answer loop before readiness, done,
5. preserve unresolved dissent in the working brief, done.

That would make Qwendom feel much closer to a real group conversation while
reusing the society primitives that already exist.
