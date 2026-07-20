# Repository implementation

Version: 2

## Purpose

Implement a bounded repository change inside the assigned sandbox and return
verifiable artifacts. This skill belongs only to the Builder specialist.

## Required execution lifecycle

1. Work only on the objective and artifact paths owned by the assignment.
2. Inspect relevant code before editing and preserve unrelated user changes.
3. Write every owned file before attempting execution. Write documented,
   maintainable code without placeholders or fabricated execution claims.
4. List and read back every owned path after writing it. Correct missing or
   misplaced files before running any check.
5. Execute a supported validation command with `execute_command` and retain
   its exact result. Prefer this command path for scripts that import product
   files; do not use `run_code` for those imports because its runtime may be
   restricted.
6. If execution fails, correct the reported issue and rerun a supported
   command. A later successful execution is required; otherwise report the
   blocker truthfully and do not claim the check passed.
7. Export every required artifact only after successful execution, with its
   path and content hash.

## Output contract

Return changed artifact paths, read-back confirmation, executed commands and
their results, exported artifact references, and any remaining blocker. The
Builder cannot validate its own artifacts as independent acceptance evidence.
