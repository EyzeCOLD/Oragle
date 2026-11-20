# Working conventions for this repo

Read these before changing anything; they encode decisions the project
has already made. The full design history and mechanics notes are in
`PLAN.md` (append to it, don't rewrite it).

## Constraints

- **Python standard library only** — zero runtime dependencies, no
  third-party imports anywhere in `mtgjudge/`. Package/run with `uv`.
  Do not add a dependency to make something convenient; that has never
  been needed and is a deliberate feature (the app works in any venv).
- **One `index.html`** for the web frontend, no build step, no npm.
- **No emoji or decorative unicode** anywhere: prose, code, comments,
  commit messages, UI. Styled terminal output comes from `mtgjudge/ansi.py`.
- Match existing style: 4-space indent, ~79-col prose in docstrings,
  comments explain mechanisms (why), not narration.

## Principles (from PLAN.md — keep them true)

- **Deterministic first.** Retrieval, name matching, decklist parsing,
  everything possible is deterministic code, not an LLM call: it is
  fast, free, and testable. No LLM pre-passes; new vocabulary belongs in
  the synonym tables. Measure latency changes against the bench files.
- **Answers must stay honest.** The system prompt forces rule-number
  citations and "not covered" refusals. Never weaken this for smoother
  answers — hallucination is the failure mode that matters (PLAN.md,
  "Known risks").
- **Card references are explicit `[[Name]]` only.** Never reintroduce
  auto-detection from prose; the decklist parser is likewise literal
  (no "fixing" names, whitespace/accents excepted).
- **Continuing a conversation must be deliberate.** Threads carry
  context by default; both frontends nudge toward a new thread when the
  topic changes. Don't remove the nudge lines.
- **Frontends are peers.** A CLI feature (hint lines, thread semantics,
  progress events) has a web counterpart and vice versa; keep parity.

## Testing and verification

- `uv run --with pytest pytest -q` is the test suite (pytest is fetched
  ad hoc with `--with`; it is not a project dependency). Every behavior
  change gets a unit test in `tests/`.
- **Web changes are verified in a real browser**, headless Chromium via
  CDP: run the fake-pipeline server (`server.run(token, pipeline=...)`
  with a stub pipeline; see git history for `/tmp/fake_oragle*.py`
  examples) and drive both themes. Needs
  `uv run --with websocket-client python <script>`.
- Verify behavior by **computed styles and layout, not attributes**
  (a `hidden` attribute can be set while the element still renders —
  that shipped as a bug once). Check visibility with
  `getBoundingClientRect()`, not truthiness.
- Accessibility: `dev/a11y_check.py` runs an axe audit in both themes;
  keep new findings at zero (two pre-existing "needs review" contrast
  notes on the header chrome are known).
- Don't claim something works from code review alone: CLI changes get a
  real `oragle ask` run, streaming changes get the spinner/stream test.

## Data and network etiquette

- Scryfall gets at most one bulk-data freshness check per 24 h;
  symbol art and card data are cached/bundled and never re-fetched.
  Keep every path usable offline (`--offline` must work).
- Keep attribution intact: the Scryfall credit line in the web gate,
  the WotC trademark note in the README (fan-content usage).

## Git

- Work on a feature branch per task; merge to main with a merge commit
  whose message lists what the branch did. Never push without the
  user saying so.
- PLAN.md is updated in the same commit as the change it documents.
- `.env` is personal (contains real keys) and gitignored. Registered
  env vars are documented in `.env.example` and the README table;
  `oragle config` must show their provenance.
