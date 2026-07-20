# Contributing to Qwendom

Qwendom is currently optimized for the Qwen Cloud Track 3 hackathon. Keep
changes small, evidence-backed, and truthful: do not fabricate agent actions,
tool results, benchmark trials, votes, or successful terminal states.

## Local checks

```powershell
cd backend
python -m pytest -q

cd ..\frontend
npm run build

cd ..
git diff --check
```

Changes to orchestration or benchmark behavior should include focused tests.
Do not alter the frozen benchmark fixture, ground truth, or evaluator merely to
improve a reported score. Preserve failed trials and document any budget or
configuration change.

## Pull requests

Describe the user-visible outcome, the evidence used to verify it, and any
known limitation. Never commit `.env` files, provider keys, runtime event logs,
local databases, generated frontend bundles, or unlicensed media.

Report security issues through [SECURITY.md](SECURITY.md), not a public issue.
