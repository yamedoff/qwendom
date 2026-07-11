# Qwendom v4.2 — component catalog for porting to React (or any framework)

This prototype is five standalone HTML files sharing one CSS core (`core.css`)
and one JS behavior layer (`core.js`). This document is the porting spec: for
each component it lists the props/data schema, the CSS hooks, and any
behavior a coding agent needs to reproduce. Components are grouped by how
they're currently implemented here.

## JS-driven components (logic lives in `core.js`)

These already take data as explicit props (`data-*` attributes) or a plain
data object, and a pure render step turns that data into markup. Porting
these is mostly copy-the-math-into-JSX.

### `<Nav current items />`
- Props: `current` (screen key), `items: {key, label, href}[]`.
- Behavior: the item whose `key === current` gets `aria-current="page"`.
- CSS hooks: `.railnav a`, `.bottomnav a`, `[aria-current]`.

### `<Explainer title text>children</Explainer>`
- Props: `title: string`, `text: string`, `children: ReactNode`.
- Behavior: renders an ⓘ button; click toggles a popover positioned near the
  wrapped content; clicking elsewhere or Escape closes all open popovers.
- CSS hooks: `.has-exp`, `.exp-btn`, `.exp-pop`, `.exp-open`.
- Gotcha: popovers are mutually exclusive — opening one closes all others
  (a single "which one is open" piece of state at the page level, not one
  boolean per instance).

### `<Tour steps />`
- Props: `steps: ExplainerRef[]` — the ordered list of `<Explainer>` instances
  on the page (in the current prototype this is "every Explainer in DOM
  order"; a real app would collect refs or ids explicitly).
- Behavior: a ▸ TOUR button; each click opens the next step's popover, scrolls
  it into view, and relabels itself "NEXT n/total"; after the last step it
  resets to ▸ TOUR.
- CSS hooks: `#tour` / `.tourbtn`, `.tour-focus`.

### `<StanceDial stance strength tick />`
- Props: `stance: "aligned"|"dissent"|"blocking"|"changed"`, `strength: number
  (0..1)`, `tick: number (0..1) | null`.
- Renders: an SVG ring — gray track circle, colored arc sized by `strength`
  (arc length = `strength * circumference`, rotated to start at 12 o'clock),
  and (if `tick` isn't null) a short radial tick mark at that angle.
- Colors: aligned `#4E9A6F`, dissent `#C07C33`, blocking `#CE5F4E`, changed
  `#64809A` (see `Qwendom.data.stanceColors` in `core.js`).
- CSS hooks: `.dial`, `.dial-ring`, `.dial-hist`, `.dh-row` (history rows,
  shown on hover/focus — not part of the SVG itself, see Ownership/markup
  components below for the surrounding card).

### `<PhaseStrip config />`
- Props: `config: { total: number, current: number, gates: Record<number,
  "passed"|"open"> }`.
- Behavior: renders `total` tick marks; ticks `<= current` are colored along
  a fixed gradient from `rgb(242,224,198)` to `rgb(122,62,34)`; ticks listed
  in `gates` render as a keyhole ring instead (`passed` vs `open` = CSS
  modifier only, no visual difference is encoded beyond the class today —
  confirm intended styling before porting); a badge is horizontally centered
  over the `current` tick.
- CSS hooks: `#phase-dots`/`.dotstrip`, `.gate.passed`, `.gate.open`, `.cur`,
  `#phase-badge`.

### `<ArcTimeline data />`
- Props: single `data` object — schema is documented inline in `core.js`
  above `Qwendom.data.arcTimeline` (layout bounds, `officeColors`, `arcs`,
  `events`, `trend`, `trendLabel`, `trendSplitIndex`).
- Behavior: draws a time axis, curved influence arcs with arrowheads and
  labels above it, event ticks with gate styling below it, and a stacked-dot
  "alignment trend" chart beneath that, all on one shared minute-based x
  scale (`minMinute`–`maxMinute` mapped to `minX`–`maxX`).
- Used only on Recap. Pure function of `data` — no other DOM reads.

## Markup-only components (currently hand-authored HTML per screen)

These don't have JS in this prototype — the HTML below *is* the render
output. To port them, treat each one's existing markup as the "expected JSX
output" and derive the props schema from what varies between instances.

### Typed turn card — Live run (`.feed .turn.<type>`)
- Props: `type: "direct"|"propose"|"challenge"|"block"|"concede"`, `office:
  {code, name}`, `role: string`, `time: string`, `body: string`,
  `delegations?: {office, note, id}[]` (direct/propose turns),
  `violatedConstraint? / liftCondition?` (block turns — render the specific
  constraint text and the condition that would lift the block),
  `changedFrom? / changedBecause?` (concede turns — render what changed and
  why).
- CSS hooks: `.turn`, `.turn-top`, `.ttag.<type>`, `.tx`, `.t-delegate`,
  `.dr` (delegation row), `.t-event` (system/ledger events between turns, not
  a turn itself — `{text: string}`).

### Ownership token — Live run "THE WORK" section (`.drow`)
- Props: `office: {code}`, `task: string`, `owner: string`, `dependency?:
  {direction: "waits"|"holds", label: string}`, `status: string` (pill text,
  e.g. "IN PROGRESS").
- CSS hooks: `.drow`, `.orb.<code>`, `.task`, `.dep.waits` / `.dep.holds`,
  `.pillstat.<status-modifier>`.

### Living brief with blame — Live run / Recap (`.brief-obj`, `.blame`)
- Props per line: `text: string`, `blame: {author, time}`, `hot?: boolean`
  (recently changed — highlights the line).
- CSS hooks: `.blame`, `.blame.hot`, `.obj-blame`.

### Artifact passport — Recap / Dossier (`.pp-sub` inside an artifact chip)
- Props: `name: string`, `typeIcon: string`, `typeLabel: string`, `revision:
  number`, `sparkline: number[]` (bar heights, last = current rev),
  `openCritiques: number`, `openBlocks: number`, `owner: string`.
- CSS hooks: `.pp-sub`, `.spark i` (one `<i>` per sparkline bar, `.hot` on the
  current/last bar), `.crit`, `.blk`.

### Review artifact renderers — Review screen (`.card[data-explain-title]`)
Five renderer variants sharing one card shell and one type-agnostic verdict
panel:
- **Text memo**: `paragraphs: string[]`, `critiques: {anchorParagraphIndex,
  author, note}[]`.
- **Code diff** (`.diff`, `.dl.add|.del`): `lines: {op: "add"|"del"|"ctx",
  text, anchored?: boolean}[]`, `critiques: {anchorLineIndex, author, note}[]`.
- **Spec table** (`.mono.cell-anchor`): `rows: object[]`, `columns:
  string[]`, `critiques: {anchorRow, anchorCol, author, note}[]`.
- **Mockup** (region pins): `imageUrl: string`, `pins: {x, y, index, author,
  note}[]`.
- **Checklist** (`.step.accepted|...`): `steps: {label, verdict:
  "accepted"|"deferred"|...}[]` — supports partial-accept.
- **Verdict panel** (`.verdict`, type-agnostic, always last): `decision:
  string`, `vote: string`, `reasoning: string`, `unresolvedDissent?: string`.
- Shared CSS hooks: `.card`, `.prop-head`, `.nm`, `.prop-type`, `.pillstat`.

## Shared design tokens (`core.css`)
CSS custom properties already act as a theme object — port directly to a
JS theme (e.g. a `theme.ts` or CSS-in-JS tokens object):
`--ink #2B2419, --canvas #FAF6EF, --card #FFFEFA, --wash #F3ECE0, --line
#EDE4D4, --copper #B4633A, --copper-deep #7A3E22, --green #4E9A6F, --amber
#C07C33, --red #CE5F4E, --slate #64809A, --sub #7D7365, --faint #8F8370`.
Type: serif Georgia for headings/body, Menlo for mono/timestamps/ids.

## Suggested porting order
1. Theme tokens → CSS variables or a theme object (no logic, low risk).
2. `StanceDial`, `PhaseStrip`, `ArcTimeline` — already prop-driven in
   `core.js`, straightforward 1:1 React components.
3. `Explainer` + `Tour` — needs one shared "which popover is open" context.
4. The five markup-only components, using the prop schemas above, backed by
   real data instead of hand-authored HTML.
