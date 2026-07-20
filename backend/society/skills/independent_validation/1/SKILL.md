# Independent validation

Version: 1

## Purpose

Inspect Builder artifacts from a clean validation context, execute the required
checks, and issue an evidence-backed verdict. This skill belongs only to the
Test Engineer specialist.

## Required behavior

1. Use only exported artifacts and explicit dependency evidence; do not inherit
   the Builder's private prompt, scratch state, or implementation skill.
2. Verify artifact paths and hashes before inspection.
3. Run the stated acceptance checks plus relevant adversarial cases.
4. Record exact commands, outcomes, failures, and inspected artifact references.
5. Fail closed when an artifact or required check is missing.
6. Never modify product files or convert a failed check into a passing verdict.

## Output contract

Return a pass/fail verdict, inspected artifact references, executed checks,
failures, and a concise evidence-backed summary.
