# Qwendom Agent Society Plan

## Objective

Build a hackathon-ready Agent Society application for the Qwen Cloud challenge. The product should demonstrate a society of capable agents that can solve arbitrary problems through identity, skills, memory, temporary teams, elected task leaders, negotiation, voting, peer monitoring, child-agent spawning, dissolution, and continuous learning from previous collaborations.

## Stack

- Backend: Python, FastAPI, Agno SDK.
- LLM provider: Qwen Cloud / DashScope-compatible OpenAI endpoint by environment variable.
- Frontend: React + TypeScript + Vite.
- Storage: local JSON event/memory store for the MVP, documented so it can be swapped for a managed database on Alibaba Cloud.
- Deployment target: Alibaba Cloud VM/container deployment with environment variables.

## Build Stages

1. Scaffold the backend and frontend project structure.
2. Implement the agent society domain model:
   - agent identities, roles, skills, reputations, and memory
   - task-scoped teams
   - leader election
   - negotiation rounds
   - conflict voting
   - peer monitoring
   - workload-based child-agent spawning
   - team dissolution
   - learning records after collaboration
3. Wire Agno agents so each society member can contribute real model-backed analysis when Qwen credentials are present, with deterministic fallback behavior for local demos without secrets.
4. Expose API endpoints for running tasks and reading society state.
5. Build a frontend control room for submitting tasks, watching the collaboration timeline, and inspecting agents, teams, votes, monitoring, and final answers.
6. Add documentation for setup, Qwen Cloud configuration, architecture, and Alibaba Cloud deployment.
7. Validate locally and run OpenCode Go review passes before final handoff.

## Acceptance Criteria

- A new user can run the backend and frontend locally from the README.
- The app visibly demonstrates all required society behaviors.
- The code is documented where behavior is not obvious.
- The app can run with real Qwen Cloud credentials and has a no-secret demo fallback.
- The repo includes submission-focused documentation and architecture material.
