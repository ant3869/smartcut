# Cutroom UI

## Purpose

Cutroom is SmartCut's local editing and review surface: a compact nonlinear editor whose
automation (AI evidence, proposals, auto-edit) stays reviewable and undoable. It is not a marketing page.

## Working surface

- Header: SmartCut logo mark (`frontend/favicon.png`, also the favicon and exe icon) with "SmartCut / Cutroom", menus (File, Project, Sequence, Markers, Auto, Export, View, Help), gateway status, theme toggle, settings.
- Page header: job name with save state, and the pipeline actions (Import, Analyze, Auto-edit, Re-plan, Preview reel, Render).
- Project panel: media bins, search, list/icon views, reel selection.
- Source and Program monitors: transport, source-time evidence minimap, live captions and the AI verdict overlay.
- Timeline: AI and transcript lanes, V2/V1/A1/A2 tracks with filmstrips and waveforms, markers, trim/snap/link tools.
- Inspector: Effects, Review (AI summary, proposal queue, decisions, evidence, transcript), Pipeline.

## Interaction contract

- Every automatic edit is one undo step with a dry-run preview first; Keep/Protect ranges are never cut.
- Review decisions write the hash-bound `editor_review.json` (`timebase: source`); proposals are accepted or rejected into the same payload.
- Rendering is explicit. The UI never overwrites a source file or treats raw Eye flags as approved.
- Color never carries meaning alone: status pills pair a dot with text, flags have titles, proposals show strength labels.

## Visual system

Cutroom adopts the OpenEval visual system (github.com/RasputinKaiser/OpenEval, MIT). The reusable
`openeval-design` skill holds the tokens and recipes; `frontend/styles.css` is the reference build.

- Themes: charcoal dark and white light via `:root.light`; **Auto** follows the OS live. The View menu and
  the header toggle cycle Auto → Light → Dark; `index.html` applies the stored choice before first paint.
- Every color is a token or a `color-mix()` of tokens (`--color-bg/-subtle/-elev`, `--color-bd/-subtle`,
  `--color-fg/-muted/-dim`, `--color-accent/-soft`, `--color-ok/warn/err/info`). Video stages keep dark tokens in both themes.
- Surfaces: 12px cards with blended borders and soft shadows; 6px controls; 8px page actions and dialog buttons; 999px pills.
- Type: system sans; 10px uppercase `.12em` eyebrows in `--color-fg-dim`; numbers and timecodes in the mono stack with tabular, slashed-zero figures.
- Active states are accent-tinted fills with accent borders and `--color-accent-soft` text; hover blends toward `--color-bg-elev`; press scales to .96.
- Icons are lucide line icons vendored in `frontend/icons.mjs` (no build step, works offline).
- Motion is 120–180ms and interruptible, and is removed under `prefers-reduced-motion`.
- Narrow screens stack the panels; the timeline scrolls itself and the document never grows horizontally.
