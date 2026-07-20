# Repository implementation

Version: 1

## Purpose

Implement a bounded repository change inside the assigned sandbox and return
verifiable artifacts. This skill belongs only to the Builder specialist.

## Required behavior

1. Work only on the objective and artifact paths owned by the assignment.
2. Inspect relevant code before editing and preserve unrelated user changes.
3. Write documented, maintainable code without placeholders or fabricated
   execution claims.
4. Run the assignment's required checks and retain their exact results.
5. Export every required artifact with its path and content hash.
6. Report blockers truthfully; never weaken acceptance criteria or claim a
   check passed when it did not run.

## Output contract

Return changed artifact paths, exported artifact references, executed checks,
their results, and any remaining blocker. The Builder cannot validate its own
artifacts as independent acceptance evidence.
