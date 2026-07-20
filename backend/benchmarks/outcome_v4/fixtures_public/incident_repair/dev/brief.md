# Multi-service incident repair

Repair the fictional local repository copy in this workspace.

Public facts:

- The API authorization path regressed for support scope.
- The worker processes duplicate jobs more than once.
- The database configuration reads the wrong environment key.
- A reachable dependency version is vulnerable.
- The available logs and traces are incomplete and partly misleading.
- The rollback requirement is fifteen minutes or less.

Deliverables:

- working repository fixes in place;
- deterministic test, security, and evidence reports;
- a machine-readable rollback plan;
- durable cleanup evidence.
