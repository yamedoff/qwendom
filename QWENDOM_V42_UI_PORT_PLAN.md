# Qwendom v4.2 UI Port Plan

## Objective

Port the current Qwendom frontend toward the Notion v4.2 UI prototype without changing backend behavior.

Source of truth:
- Notion page: `Porting Guide - Qwendom v4.2 Components`
- Local app: `C:\Users\boudi\Documents\gemma kingdom\frontend\src\App.tsx`
- Local styles: `C:\Users\boudi\Documents\gemma kingdom\frontend\src\styles.css`

## Constraints

- Keep the current React/Vite stack.
- Preserve current API calls and task/event behavior.
- Make minimal, reviewable edits.
- Prefer documented, typed helper components over ad hoc markup.
- Use the v4.2 tokens: warm canvas/card/wash, copper, green, amber, red, slate, Georgia for editorial text, Menlo for mono data.

## Implementation Slices

1. Theme and shell
   - Replace generic Inter/light dashboard styling with v4.2 CSS variables.
   - Keep the current two-column shell but make it feel like the prototype: editorial topbar, parchment cards, copper rails, mono metadata.

2. Prop-driven v4.2 primitives
   - Add documented `Explainer`, `StanceDial`, and `PhaseStrip` components inside `App.tsx`.
   - Use one shared explainer-open state so popovers are mutually exclusive.
   - Use phase progress from the existing workflow state.

3. Existing surface upgrades
   - Wrap key workflow panels and social summaries with explainers.
   - Add a phase strip to the top status area.
   - Add stance dials to agents using current role/reputation/risk data.
   - Restyle timeline events toward typed turn cards without changing event parsing.

4. Validation and review
   - Run frontend build/typecheck.
   - Ask DeepSeek reviewers to inspect the diff for component contract mismatch, regressions, and accessibility issues.
   - Apply only concrete findings.

## Acceptance Criteria

- `npm run build` succeeds in `frontend`.
- The UI visibly follows the v4.2 direction: copper/parchment tokens, serif editorial hierarchy, mono metadata, explainers, phase strip, stance indicators.
- Existing task creation, replay, event rendering, clarification submission, agent memory, and health display remain wired.
- No backend files are changed for this UI slice.
