# Security policy

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability or exposed secret.
Use GitHub's private vulnerability-reporting feature for
`yamedoff/qwendom`. Include affected revision, reproduction steps, impact, and
any suggested mitigation. Do not include real API keys, personal data, or
third-party credentials in the report.

If private reporting is unavailable, contact the repository owner through the
private contact method listed on their GitHub profile and request a secure
reporting channel before sending technical details.

## Supported code

Only the current `main` branch is supported during the hackathon. Local JSONL
storage and the postponed deployment/isolation boundaries are demonstration
constraints, not production security guarantees.

## Secret handling

- Keep Qwen and other provider keys in environment variables or an uncommitted `.env`.
- Never place credentials in frontend variables, screenshots, benchmark outputs, or event payloads.
- Revoke and rotate any credential that appears in Git history or a public artifact.
- Treat model-generated commands and retrieved content as untrusted input.
