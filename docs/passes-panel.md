# Passes Panel

First-class orchestration over the existing Auto menu features. The panel
orders, configures, and runs passes — it never reimplements them.

## Architecture

- `pipeline/passpanel.py` — static `PASSES` catalog (id, label, kind,
  executor, modifies flag, route), per-project state defaults, project.json
  migration, `derive_status()` hints, `BUILTIN_RECIPES` (Vertical Social,
  Clean Longform).
- `pipeline/web.py` — `GET/PUT /api/projects/{id}/passes` (catalog + stored
  state + derived applied-status + recipes), `POST/DELETE .../pass-recipes`.
  No generic "run anything" endpoint: server passes execute through their
  existing routes (transitions/apply, watermark, audio/*, intro-outro/apply,
  qc/run), which take sequence-in-body and return the updated sequence.
- `frontend/passes-panel.mjs` — DOM-free registry mirroring the catalog.
  Each entry names its implementation via `reuses` (the exact Auto-menu
  function or endpoint wrapper). `runPass` / `runEnabled` take injected
  runners; `registerPass()` adds future passes without touching the panel.
- `frontend/app.js` — `passRunners` map wires each `reuses` key to the real
  function (quickAuto, post transitions/apply, PUT watermark, ...). The
  Passes dialog renders rows from catalog + stored state, with enable
  checkboxes, status pills, Settings/Run/Clear per row, drag reorder,
  recipe controls, and Run Enabled Passes.

## Pass kinds

- `timeline` — edits the sequence (undoable commit per pass). Badge: timeline.
- `render` / `verify` / `export` — render-only, read-only, or dialog.
  Badge: render-only. Audio Cleanup saves a spec honored at render;
  QC never modifies the timeline; Render opens the export dialog.

`captions` is `executor: "none"` (planned): row renders, Run is disabled
with the reason, no runner to fake.

## Failure policy

`runEnabled(state, {runners, stopOnError})` runs enabled passes in stored
order. `stopOnError=true` (panel default, checkbox) halts at the first
throw; a QC summary matching `/[1-9]\d* error/` also halts when the pass
has block enabled. `false` collects every result and keeps going.

## Persistence format (project.json)

```json
"passes": {"qc": {"enabled": true, "order": 9, "settings": {"deep": true},
                   "status": "complete", "summary": "0 errors · 2 warnings"}},
"recipes": [{"name": "Clean Longform", "passes": ["audio-cleanup", ...],
             "settings": {"render": {"preset": "youtube-1080"}}}]
```

`ensure_project_passes()` backfills missing passes/recipes on read, drops
state for retired pass ids, and seeds built-ins once (deletes survive
refresh; Defaults re-adds). Applying a recipe sets enabled/order from its
list (others disabled) plus its settings; a recipe `disabled` list keeps
entries visible but off until configured (captions has no runner yet; an
unconfigured watermark would halt the batch on validation).
`validatePassSettings()` blocks runs with no media before anything
destructive. The render runner opens the export dialog with the recipe's
preset dimensions applied.

## Files

- `pipeline/passpanel.py`, `tests/test_passpanel.py` (8: catalog, state,
  migration, derive, endpoints incl. recipe CRUD)
- `frontend/passes-panel.mjs`, `tests/passes-panel.test.mjs` (8: ordering,
  enabled filter, single run, stop/continue policy, runner reuse)
- `frontend/app.js` (runners, dialogs, drag/drop, recipes), `styles.css`
- `docs/passes-panel.md` (this file)

## Migration concerns

- Old projects: migration is additive on first GET/PUT; sequence.json
  untouched; revision counter not bumped by pass-config saves.
- Pass statuses are hints, not truth: `derived` recomputes applied-state
  from the sequence every GET; stored `complete` survives but the panel
  prefers live `running/complete/warning/error`.
- Render-pass toggles in the export dialog are unchanged; the panel's
  `render` pass opens that dialog rather than duplicating it.
