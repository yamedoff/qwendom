# Qwendom society runtime

This document describes the current production path: Qwen-backed governance, fixed specialist composition, AgentBay execution, independent validation, and event-sourced review surfaces.

## Runtime shape

| Layer | Main modules | Responsibility |
|---|---|---|
| API and streaming | `backend/main.py` | Task submission, clarification, SSE, projections, verified artifact downloads, health |
| Society lifecycle | `backend/society/orchestrator.py` | Team formation, readiness, leadership, debate, composition, validation, terminal state |
| Fixed specialist policy | `backend/society/capability_registry.py` | Immutable templates, tool grants, skill hashes, resource and artifact contracts |
| Execution graph | `backend/society/composition_runtime.py` | Dependency-aware specialist scheduling, AgentBay/media execution, retries, blockers |
| Qwen agents | `backend/society/agents.py`, `team.py` | Agno agents and coordinated Qwen turns |
| AgentBay tools | `backend/society/tools/agentbay.py` | Task-scoped code and browser environments, commands, files, renders, exports, cleanup |
| Media tools | `backend/society/tools/image_generation.py`, `video_generation.py`, `media_store.py` | Qwen image and Wan video submission, collection, inspection, publication |
| Evidence and replay | `backend/society/memory.py`, `projections.py` | Append-only events and Live/Review/Recap/Dossier projections |
| Artifact delivery | `backend/main.py` | Task ownership, SHA-256 verification, download and view endpoints |

## The two-layer team

### Core society

Four persistent agents govern every mission:

| ID | Name | Role | Primary responsibility |
|---|---|---|---|
| `architect` | Ada | Systems Architect | scope, decomposition, interfaces, ownership |
| `researcher` | Ibn | Research Analyst | evidence, uncertainty, current technical context |
| `builder` | Lin | Builder | implementation feasibility and work planning |
| `critic` | Noor | Adversarial Reviewer | counterarguments, blockers, and acceptance risk |

These roles participate in readiness, leadership, debate, and voting. Their contributions remain linked in the event ledger rather than being flattened into parallel answers.

### Fixed execution specialists

The elected leader selects from a repository-owned catalog after readiness. These employees do not automatically join the voting roster.

| Template | Role | Pinned skill | Production rights | Validation rights |
|---|---|---|---|---|
| `builder@2` | Implementation Engineer | `repository_implementation@2` | repository changes and exported artifacts in AgentBay | cannot validate own output |
| `test_engineer@1` | Test Engineer | `independent_validation@1` | no product-file changes | independently inspects and validates exports |
| `image_creator@1` | Image Creator | `image_generation@1` | generated and published image artifacts | cannot accept own output |
| `frontend_engineer@2` | Frontend Engineer | `frontend_browser_delivery@1` | bounded UI, browser evidence, and exports in AgentBay | cannot validate own output |

Every template freezes its capabilities, exact tool IDs, required tools, resource limits, conflict domains, and artifact contract. Every skill reference includes a version and SHA-256. `resolve_specialist_bundle()` verifies the content and derives a tool-bundle hash before the invocation can proceed.

## Tool boundaries

### Implementation Engineer

Granted tools:

`start_execution_environment`, `execute_command`, `run_code`, `read_text_file`, `write_text_file`, `list_files`, `export_artifact`, `close_execution_environment`

The skill requires inspection before editing, read-back after writing, a successful supported check, explicit export, and truthful blocker reporting. It cannot supply independent acceptance evidence for its own work.

### Test Engineer

Granted tools:

`start_execution_environment`, `execute_command`, `run_code`, `read_text_file`, `list_files`, `inspect_artifact`, `report_independent_validation`, `close_execution_environment`

The validator works from exported artifacts and explicit dependency evidence. It verifies paths and hashes, runs acceptance and adversarial checks, fails closed on missing evidence, and never edits product files.

### Image Creator

Granted tools:

`generate_images`, `inspect_image`, `publish_image`

The skill creates a durable provider artifact, inspects its stored manifest, and publishes it with model provenance. Placeholder files, stock downloads, and unrecorded external assets are outside the contract.

### Frontend Engineer

Granted tools:

AgentBay environment, command, code, file, `browser_render`, export, and cleanup tools.

The skill writes a renderable build, confirms the output path, captures browser and console evidence, exports the owned artifacts, and closes the environment. Browser evidence still requires a separate Test Engineer verdict.

### Research and media

The Researcher may use the allowlisted Context7 MCP path when current technical evidence is necessary. Qwendom records the lookup objective, query, returned evidence, provenance, and failures.

Image execution uses Qwen Image through the configured DashScope/Model Studio endpoint. The runtime also contains Wan Video submission, status, collection, and inspection tools. Provider operations are recorded as tool evidence; the production fixed catalog currently assigns image generation to the Image Creator template.

## Mission lifecycle

1. **Intake** — `POST /tasks` creates a task and emits `task_received`.
2. **Team formation** — the four core roles enter a task-scoped team and publish a working brief.
3. **Goal discussion and readiness** — roles reply to the mission, identify missing information, and cast typed readiness ballots.
4. **Pause when necessary** — user-input, system-capability, and safety blockers can pause the run. `POST /tasks/{task_id}/clarifications` resumes from the persisted phase.
5. **Leadership** — the society elects a task-specific leader.
6. **Composition** — the leader selects fixed templates and produces a dependency-aware work graph. Unknown templates, unavailable required tools, and invalid bundles fail closed.
7. **Execution** — specialists run only when dependencies and resource limits allow. AgentBay, media, and research calls emit typed results and failures.
8. **Debate and selection** — proposals, direct replies, counterproposals, revisions, ballots, and the selected approach remain linked in the ledger.
9. **Independent validation** — exported artifacts and acceptance evidence are checked by a role separate from the producer.
10. **Terminal gate** — the runtime derives `complete`, `complete_with_warnings`, remediation, interruption, or failure from evidence. Prose cannot override the gate.

## AgentBay lifecycle

AgentBay is the execution boundary for code, tests, and browser work:

1. start a task-scoped code or browser environment;
2. stage approved inputs;
3. expose only the resolved specialist tools;
4. execute bounded commands, code, file, or browser operations;
5. export explicit artifacts;
6. retrieve bytes and record provenance plus SHA-256;
7. close the environment; and
8. preserve cleanup failure as a blocker.

The host repository is not the Builder's workspace. A file inside AgentBay is not a public artifact until it is exported, retrieved, hashed, attached to the task, and independently validated.

## Evidence model

The append-only event store is the source of truth for:

- conversation turns and direct replies;
- readiness ballots and typed blockers;
- leader and specialist selection;
- work-graph dependencies and ownership;
- tool intent, results, retries, and failures;
- AgentBay session lifecycle and cleanup;
- artifact exports, provenance, and hashes;
- independent validation; and
- final acceptance status.

React consumes stable projections of these events. Live, Review, Recap, Artifacts, and Dossier are different views of the same history.

## Configuration

The production path uses `backend/.env`:

| Setting | Purpose |
|---|---|
| `QWEN_API_KEY`, `QWEN_BASE_URL`, `QWEN_MODEL` | Qwen Cloud reasoning |
| `AGENTBAY_API_KEY`, `AGENTBAY_ENDPOINT`, `AGENTBAY_REGION_ID` | isolated execution |
| `AGENTBAY_IMAGE_ID`, `AGENTBAY_BROWSER_IMAGE_ID` | code and browser sandbox images |
| `TEAM_COMPOSITION_EXECUTION_ENABLED=true` | fixed specialist execution |
| `SOCIETY_MAX_MODEL_WORKERS` | concurrent model work |
| `SOCIETY_MAX_AGENTBAY_SESSIONS` | concurrent AgentBay environments |
| `SOCIETY_MAX_MEDIA_JOBS` | concurrent media jobs |
| `CONTEXT7_MCP_ENABLED`, `CONTEXT7_MCP_COMMAND` | technical research path |
| `QWEN_IMAGE_MODEL`, `WAN_VIDEO_MODEL` | media model IDs |
| `ALLOW_DETERMINISTIC_NO_KEY=false` | fail honestly without credentials |

Deterministic no-key mode exists for labelled lifecycle tests only. It is not provider, sandbox, or benchmark evidence.

## API surface

| Endpoint | Purpose |
|---|---|
| `GET /health`, `/health/preflight` | provider and execution readiness |
| `GET /agents` | core society roster |
| `GET /agents/{agent_id}/dossier?task_id=...` | participant record |
| `POST /tasks` | submit a mission |
| `POST /tasks/{task_id}/clarifications` | resume a paused mission |
| `GET /tasks/{task_id}` | task state |
| `GET /tasks/{task_id}/events` | append-only history |
| `GET /tasks/{task_id}/stream` | live SSE stream |
| `GET /tasks/{task_id}/cockpit` | live projection |
| `GET /tasks/{task_id}/review` | ownership and acceptance projection |
| `GET /tasks/{task_id}/recap` | decision-path projection |
| `GET /tasks/{task_id}/artifacts` | task-owned outputs |
| `GET /tasks/{task_id}/artifacts/{artifact_id}` | verified artifact download |

## Validation commands

From the repository root:

```powershell
python -m compileall backend
python -m pytest backend/tests/test_capability_registry.py backend/tests/test_composition_runtime.py -q
cd frontend
npm run build
```

For a live provider run, also require a ready `/health/preflight`, visible AgentBay session closure, downloadable artifact hashes, and an independent validation event.
