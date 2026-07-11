# Hackathon first-run QA — 2026-07-11

## Scope

Verified the journey a hackathon judge follows: use the documented local
startup commands, open the control room, submit a real mission, observe the
live collaboration, and inspect the completed decision, recap, and dossier.

## Verified outcome

- `python -m uvicorn main:app --port 8000` and `npm run dev` from `frontend/`
  served a healthy backend and control room.
- A real mission completed successfully, producing 202 events, four completed
  subtasks, a leader decision, final answer, team dissolution, and persisted
  evaluation metrics.
- The browser showed the completed Review, Recap, and Dossier pages without
  console warnings or errors.
- `frontend` production build passed; backend tests passed (`26 passed`).

## Fixed blocker

`frontend/.env.local` targeted port `8002`, while the README and root dev
script start FastAPI on port `8000`. A new user therefore saw the offline
state after following the documented commands. The frontend API base is now
aligned to `http://127.0.0.1:8000`.

## Hackathon risks found

1. **P1 — first live run takes too long for a judged demo.** The verified
   mission completed in `596.651` seconds (about 9 minutes 57 seconds), not
   the three-minute narrative requested by the mission. The run made 67 tool
   calls and recorded three failed tool calls. A presenter needs a prepared
   completed run, a significantly shorter live path, or an explicitly
   product-approved fast-demo mode before relying on an unscripted run.
2. **P1 — runtime identity is not yet Qwen.** The verified `/health` response
   reported `provider: cerebras` and `model: gemma-4-31b`. This contradicts
   the Qwen hackathon positioning, but model/reference migration was deferred
   by the user during this QA pass.
3. **P2 — root build command is absent.** `npm run build` at the repository
   root fails because there is no root `build` script. The documented
   deployment path, `cd frontend && npm run build`, does pass.

## Evidence

- Task ID: `ff74a9d8-bbe0-4e6f-be7a-baa61a86a59a`
- Final status: `complete`
- Terminal event: `task_complete`
- Metrics: `total_duration_seconds: 596.651`, `tool_calls_total: 67`,
  `tool_calls_failed: 3`
