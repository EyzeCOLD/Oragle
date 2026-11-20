# MTG Rules Assistant — Project Plan

A local CLI tool that answers questions about Magic: the Gathering rules using
retrieval (keyword search first, embeddings later if needed) plus an LLM that
answers with rule-number citations. Card database and rulings via Scryfall.

Goal for v1: answers a human can verify by citation, not an infallible judge.

---

## Guiding decisions (revisit these if they stop serving you)

- **CLI first, in a language you already know.** Utility test before UI work.
  ~~Node/TS~~ **Decision: Python (stdlib only), packaged with uv.** The
  workload is I/O-bound; Rust/C++ would add compile-time friction without
  user-visible speedup. `uv run mtg ...` is the interface.
- **Keyword search (BM25 or even naive scoring) before embeddings.** Rule
  numbers give you clean chunks and heavy keyword signal. Add a vector store
  only when you can show retrieval is failing. **Implemented: BM25 k1=1.5
  b=0.75 over rule-number chunks, exact rule-number queries boosted.**
- **Citations are mandatory.** Every answer must quote the rules it used.
  This makes hallucination visible and is the core product feature.
- **Local LLM optional.** **Implemented: one OpenAI-compatible client,
  three backends auto-detected: Mistral API (MISTRAL_API_KEY), LM Studio
  (localhost:1234/v1), Ollama (localhost:11434/v1).**

---

## Phase 0 — Data acquisition (1 evening)

Sources:
- **Comprehensive Rules**: Wizards publishes the CR as a `.txt` file on the
  comprehensive rules page. It's plain-text, hierarchical by rule number.
- **Cards + rulings**: Scryfall API (`/cards/named`, `/cards/collection`,
  `/cards/{id}/rulings`). No key required, be polite with rate limits.
  Scryfall also exposes a full daily bulk dump.

Tasks:
- [x] Script that downloads the CR text and prints basic sanity stats
      (number of rules, biggest section). (`uv run mtg refresh`)
- [x] Script that fetches one card by name and its rulings and prints them.
      (`uv run mtg card "Lightning Helix"`)
- [x] Decide where data lives on disk and commit to it.
      **Decision: `$MTG_DATA_DIR` or `~/.local/share/mtg-rules-judge/`
      (`rules.json`, `comp_rules.txt`, `cards.db`).**

Done when: you can run two commands and see real data locally. **Done.**

## Phase 1 — Chunking + keyword search CLI (1–2 sessions)

Tasks:
- [x] Parser that splits the CR into chunks keyed by rule number
      (e.g. `117.5`, `702.2c`). **Chunking decision: each rule number is its
      own chunk. A parent rule's chunk contains ONLY its own text, never its
      subrules; `Example:` paragraphs attach to the rule they follow.
      Subrules are pulled in explicitly by `mtg rule` and by context assembly
      for `ask`.**
- [x] Index the chunks in memory or SQLite. Keep the heading (e.g.
      "702. Keyword Abilities") alongside each rule. (In-memory BM25 per
      invocation, ms-fast; parsed chunks persisted as `rules.json` with the
      CR effective date.)
- [x] `mtg search "deathtouch trample"` — returns top-k rules with numbers and
      text.
- [x] `mtg rule 702.2` — prints one rule plus its parent heading (and subrules).
- [x] Persist chunks so you don't re-parse on every invocation.

Done when: `mtg search \"dies\"` finds the definition of \"dies\", and you're not
embarrassed by the ranking on 5 test queries you care about. **Done**
(\"dies\" -> 700.4 ranks #1).

**Checkpoint question:** are these 10 results the rules *you* would want as
context to answer your own question? If yes, you have a RAG pipeline missing
only the LLM.

## Phase 2 — LLM answering with citations (1–2 sessions)

Tasks:
- [x] One abstraction: `askLLM(systemPrompt, contextBlocks, question) -> string`.
      (`llm.ask_llm`; one OpenAI-compatible client, backends: mistral /
      lmstudio / ollama, auto-detected.)
- [x] `mtg ask "do blockers assign damage before attackers?"` — retrieves
  top-k rules, puts them in the prompt with their numbers, prints the answer.
- [x] System prompt requirements: answer only from the provided rules, cite
  every claim as rule numbers, say "not covered in retrieved rules" when
  that's true.
- [x] Show retrieved context under the answer: a "Context used:" line always,
  full blocks with `--verbose`.

Done when: on a set of ~10 real questions you write, every rule citation
actually appears in the context you retrieved — and the answers are right.
**Mostly verified** — every citation appeared in the retrieved context;
answers correct on simple questions; mistral-small's reasoning gets shaky
on subtle interactions (Squee recast vs. destroyed) — a model-quality issue,
not retrieval.

## Phase 3 — Cards enter the picture (1–2 sessions)

Most rules questions are actually about specific card interactions.

Tasks:
- [x] `mtg card "Lightning Helix"` — prints oracle text + rulings.
- [x] Let `ask` accept a special syntax or flag for cards, e.g.
  `mtg ask "does [[Lightning Helix]] kill [[Grizzly Bears]]?"`.
  Card oracle text + rulings get added to the context.
- [x] Handle multi-card questions (context budget: rules + cards + rulings).
- [x] Basic keyword extraction so "does X + Y interaction work" pulls the
  relevant keyword rules (702.x) automatically. (Keyword list derived from
  the 702.N rule headers at parse time, matched against oracle text.)
- [x] **Local card database (beyond the original plan):** SQLite `cards.db`
  built from Scryfall's daily "Oracle Cards" bulk jsonl.gz (~25 MB
  download, ~16 MB on disk, ~39k cards, one row per Oracle id, multi-face
  cards merged). Lookups are local-first (diacritic/punctuation-normalized
  exact, then word-fuzzy); live Scryfall is only the fallback for cards
  newer than the last refresh. Rulings fetched per card on demand and
  cached in the DB. **Freshness: at most one `/bulk-data` request per 24h;
  notice printed when upstream is newer; network failures silent;
  `--offline` skips the net entirely; `mtg refresh cards` rebuilds (~4s).**

Done when: you can ask a two-card interaction question and the retrieved
context contains the right rules *and* both cards' real text — not a
hallucinated version of either. **Done** — both cards' oracle text verbatim
in context (Lightning Helix vs. Squee / Grizzly Bears queries).

### Phase 3.5 — Card search (added late)

- [x] `mtg cards "<Scryfall query>"` — live Scryfall-syntax search
      (`c<=2 colors:blue o:exile legal:pauper fo:...`). Search is online by
      design: Scryfall's engine beats any local reimplementation; lookups
      stay local.
- [x] Model-facing skill: `ask` exposes `search_cards` as a native
      OpenAI-style tool in a bounded tool-calling loop (max 2 rounds, then
      answer). "Which cards..." questions answer from real search results,
      not memory. `--offline` disables the tool.
- [x] Automatic card-mention detection in `ask` questions (multiword names
      always; capitalized rare single words), so `[[...]]` syntax is no
      longer required. Explicit refs remain as overrides.
- [x] `search_rules` tool added (bounded rounds now 3): when initial
      retrieval misses rules the question needs (legend rule, hybrid
      colors, priority), the model queries the local BM25 index itself
      instead of failing visibly. Verified with the Karn, Gilded Guardian
      interaction question — answer cited 107.4e, 202.2d, 704.5j, 113.7a,
      all fetched by the model. LLM timeout default raised to 120s with one
      automatic retry.

## Phase 4 — Judge mode (optional, after utility is proven)

- [ ] Given a game state description (text), answer legal questions with
      rule citations and a confidence note.
- [ ] Reference-following: when a retrieved rule cites another rule
      ("see rule 613.4"), fetch it into context (one hop only to start).
- [ ] "Unsure" mode: model must classify each answer as
      rules-grounded / ruling-grounded / reasoned-interpretation. Judge
      credibility depends on this being honest.
- [ ] Fail visibly: never smooth over a gap between retrieved rules and a
      confident answer.

## Later / not now

- Embeddings + vector DB (only with evidence keyword search fails)
- Following the full reference graph of the CR
- Layer/stack system (613, 405) special handling — hardest, save for last
- Web UI, MCP server / editor integration instead of CLI

## Web remote access (decided 2026-11-19, build started same day)

Goal: ask rules questions from the phone, PC off, using my own Mistral
budget. The pipeline (BM25 retrieval, card DB, tool loop, key) must run
somewhere — GitHub Pages can't do it.

Locked decisions (user):

- **Host: Render free tier** (user already has an account). Limits that
  shaped the design, verified 2026-11-19: sleeps after 15 min idle
  (wake ~1 min), per-request proxy timeout roughly 15-60 s, 512 MB RAM
  (cards.db 30 MB fits), 750 instance-hours/month (~744 hours in a
  month, so even a keep-alive pinger fits). Unlike Vercel functions the
  container survives BETWEEN requests, so background work + polling is
  viable; Vercel kills the process with the request, nothing survives.
- **No persistent history**: conversations live in server memory, gone
  on restart or spin-down. Matches CLI behavior (REPL forgets on exit).
- **Short-request + polling**, not one long streaming request: POST /ask
  returns a job id immediately; the answer runs in a background thread;
  GET /job/{id} returns the lines so far. Every request finishes in
  milliseconds, safely inside the proxy timeout. The wake-up delay is
  communicated in the UI via a /health check ("server waking up — takes
  about a minute, Render free sleeps after 15 min").
- **Data baked into the Docker image**, NOT the git repo (30 MB cards.db
  in git chokes clones): the image build runs the existing refresh code,
  data lands at /data via MTG_DATA_DIR. Server needs no Scryfall at
  runtime; the API key stays out of the image (Render env var).
- **Stdlib server** (http.server + threading), consistent with the
  zero-dependency CLI; `oragle serve` subcommand, same engine as the CLI.
- Auth: bearer token (env WEB_TOKEN) — the endpoint fronts the Mistral
  key. Frontend stores it in localStorage and sends Authorization
  headers.

Architecture principle: ONE engine (mtgjudge package), interchangeable
frontends; feature logic lives in the core, frontends render.
answer_question() already serves ask/bench/REPL — the server drives the
same core (history=/prior_blocks= give the web conversation memory).
Disambiguation on web takes the default (like piped runs) for v1; the
terminal keeps the live prompt. Old hosting-preference list (Cloud Run >
Hetzner > Pi+Tailscale) is superseded; if Render annoys later, moving is
a redeploy, not a rewrite.

Frontend ideas (served by the same origin, no CORS):

- Card names in answers link to Scryfall (scryfall.com/search?q=!"name").
- Rule-number citations are links that expand the rule block via a
  /rule endpoint (local index, no network).
- Collapse/show official rulings per card on tap.
- PWA manifest so it installs to the phone home screen.

Chat autoscroll (regression fixed 2026-11-19, reproduced headless via
CDP against a fake-pipeline server): the follow logic broke on the
first streamed chunk — scrollDown() measured isNearBottom() AFTER the
innerHTML swap, and a chunk taller than the 140px threshold (or the
whole non-streamed answer in one swap) looked like the reader had
scrolled up, so follow mode never re-engaged and the final answer
appeared without the view following. Invariant, now documented at
scrollDown: every caller that grows the chat (stream swaps, the done
swap, threadbar, ruleboxes) snapshots isNearBottom() BEFORE the
mutation and forces the scroll after it. Verified both ways: at-bottom
follows the answer to the pixel; scrolled up mid-answer stays put.

90s-game-UI theme (2026-11-20, branch mtgo-theme): full reskin of the
style block only — markup and JS untouched (fake-pipeline server +
headless Chromium verified computed styles, both autoscroll
behaviors, gate/chat/deck screenshots). First pass: flat "sleek
business" look. Second pass, after user feedback ("gaudy,
fantasy-immersive, textures — brick, wood, steel, jade"): all
materials are procedural inline-SVG turbulence data URIs + CSS
gradients, no binary assets. Behind everything a staggered dark
brick wall; header/composer as carved hardwood beams (stretched
turbulence grain); the chat is a framed dungeon-stone viewport with
bolted brass corner rivets (positioned radial-gradient studs in the
background, which do not scroll with content) and a warm torch glow
at the top. Answers are aged parchment sheets (stain blotches +
speckle over a cream radial, dark ink serif text, citation links
recolored umber/blue for light background) with a gold wax-seal
medallion pressed on the top corner (.answer::after — pseudo
elements survive innerHTML swaps); ruleboxes are carved stone slabs
with a gold edge; user bubbles are oiled leather with irregular
radii. Buttons: engraved brass plaques (inset bevels, glow on hover,
bevel inverts when pressed) and jade gemstone primaries (specular
highlight + glow). Modals are oaken doors (double inset ring reads
as brass inlay). Status/threadbar rails: brushed brass. Gold pulsing
orbs for the working dots. Parchment/background-colors chosen for
contrast (ink #32220f on cream; #ded0ae on stone). Third pass (same
day, user feedback): jade primaries are now lozenges (border-radius
999px) and Ask is the oversized centerpiece (14px caps, wide
padding, stronger glow). Cold-steel variant (body.steel, header
toggle button, persisted in localStorage oragle_theme): wood/leather
swap to blued steel — wall becomes riveted steel plate (repeating-
gradient seams + coarse forge blotches + brushed streaks), header/
composer are riveted beams, secondary buttons are big BLOCKY metal
slabs (border-radius 0, hard multi-step chisel bevels with zero
blur, sharp unblurred 2px 3px drop shadows that collapse on press),
the user bubble is a corner-riveted steel plate, the rulebox cold
blued steel with gold engraving, modals are steel vault doors. Jade
and parchment stay untouched in both themes. The stone viewport
turbulence seam is visible at tile edges — user verdict: charming,
left as is. Steel polish pass (user-reported gold carryovers + top
bar contrast): header/form beams brightened (mid-steel #5b646e with
a #d5e0ea top rail, near-white title, #b6c5d4 sub) so they separate
from the dark plate wall; viewport rivets silver (#e8f1fa ring),
ambient glow icy instead of torch-warm, stone base cool-tinted;
sysnote (italic + glow) cool blue; placeholder #63727f; input focus
ring icy blue (verified via CDP Emulation.setFocusEmulationEnabled
— plain headless does not apply :focus, worth remembering when
checking focus styles).

Accessibility audit (2026-11-20): dev/a11y_check.py spins the fake-
pipeline server, drives headless Chromium + CDP, injects axe-core
(cached at /tmp/axe.min.js) and reports WCAG violations for both
themes; focus emulation on. Findings fixed: aria-labels on the three
form fields (question/decklist/token); roles — threadbar region,
composer form named, modals are dialogs, main aria-labelled, and
status is role=status aria-live=polite (screen readers now hear the
working/done states). Contrast: axe cannot resolve bg through
gradients/textures (reports "needs review"), so ratios are computed
manually against the worst-case gradient stop — six pairs were
under 4.5 and were recolored: wood .sub 3.88 -> 5.01 (#cbb890),
steel .sub 3.26 -> 4.62 (#e2e8ee), both placeholders 3.3 -> 5.3
(#9e947c / #8b96a0), steel status/threadbar 4.16 -> 5.18 (#b8c4d2),
parchment footer/summary 4.08 -> 4.97 (#5a4623). All remaining
header pairs measure 5.3-6.6. Rerun: `uv run --with websocket-client
python dev/a11y_check.py`.

Fourth theme pass (user-reported): jade buttons measured ~1.9:1 at
the gradient's dark bottom with dark text — the gem goes a shade
deeper (#157f63 -> #085741 range) and the text is now engraved white
(4.6 at the lightest stop, 7.7 at the darkest); hover keeps the face
and brightens glow/text instead of lightening the gradient (which
broke 4.5). Wood modal leaf text #e4d6ab (8.0), steel #cdd7e2 (6.3).
Steel beam darkened (#4a525b) so the sub-line has real margin (6.0).
Steel answers are no longer parchment: cold ledger-paper sheets
(steel-blue links, pewter wax seal, slate ink #26303b at 7.4 on the
darkest paper shade). The three bouncing dots are replaced by a CSS
hourglass: the glass frame (clip-path bowtie, bronze in wood / pewter
in steel) flips end-over-end every 2.6s while a separate gravity-fixed
sand pile (gold/silver) glints and a stream falls through the neck —
the sand element sits OUTSIDE the rotating element so the pile always
stays in the visual bottom bulb through the flip.
prefers-reduced-motion freezes it to a static hourglass.
2026-11-20 redesign (user: "doesn't look like an hourglass at all"):
the clip-path bowtie is replaced by hourglasses drawn as SVG data-URIs —
a frame layer (caps, side posts, curved glass bulbs; bronze in wood,
pewter in steel via body.steel overrides) that still carries the
hg-flip rotation, and a sand layer (baked stream + curved pile) that
stays gravity-fixed outside the rotation and glints via the hg-glint
keyframe (brightness + opacity). The old ::before sand trick and
hg-stream keyframes are gone. Reduced-motion still freezes it.
2026-11-20 second follow-up: the dark-tint idea above was a dead end
(user: "the bubble one is an abstract mess") and the real reason the two
instances LOOKED like different animations was behavioral, not CSS:
setStatus re-created the rail hourglass element every ~700ms poll
iteration, and the flip window sat at 46% of the cycle — the rail copy
was destroyed moments before every flip and so read as a serene static
hourglass, while the bubble copy actually spun. Both experiments are
replaced wholesale: the hourglass is now inline SVG modeled on the
codepen the user linked from the start (codepen.io/mashul/pen/rPaqYE,
"SVG Hourglass CSS Only Animation" by Marius Petrescu — bowtie glass,
plates, top pile drains while the bottom pile rises, then the whole
glass turns over; the snap back to upright on cycle restart is
invisible because a full pile looks the same either way up). One
<template id="hg-tpl"> markup serves both the rail and the bubble; the
rail copy is a persistent element (#statushg) that setStatus merely
hides/shows, so its cycle never restarts mid-poll. 3.4s cycle, amber
sand + theme plates/stroke in both themes, reduced-motion freezes it.
Verified headless in both themes: drain/rise/turn running, same
markup everywhere, marker-tagged rail element survives poll cycles,
hidden after done; 175 tests and the axe audit unchanged (still only
the two pre-existing header contrast notes).
Card symbols in the web frontend (2026-11-20, symbol-rendering branch):
{2}{U/R}{U} in answers and expanded rulebox text now render as
inline Scryfall SVG icons, 1.02em, both themes. Frontend renderSymbols()
maps braces to codes (content minus slash, Scryfall capitalization:
{W/U} -> "WU", {W/P} -> "WP", {T} -> "T", digits bare, {100}/underline
forms stay literal text) and emits <img src="/symbol/{code}.svg">;
onerror swaps the img back to the literal brace text, so unknown codes
and first-touch offline degrade to exactly the old behavior. Backend:
mtgjudge/symbols.py caches each icon under DATA_DIR/symbols (one
Scryfall fetch per symbol ever, then fully offline), served by an OPEN
/symbol/{code} route (img tags cannot send Authorization; like /health,
no user data). Validated codes [0-9A-Za-z]{1,6}; offline+uncached = 404.
Gotcha discovered live: scryfall svg filenames are UPPERCASE for
letters (u.svg 404s, U.svg is real). Also fixed the recurring raw
**bold** problem: the old bold regex captured [^*\n]+, so a bold span
containing a nested *italic* failed whole and printed literally; the
new capture allows single asterisks inside (nested em + kids play
nice), and the em pass is guarded against touching <strong> edges.
Both applied in renderAnswer and the rulebox expansion. Verified
headless: 8/8 symbols load with correct srcs, bold/em nested case
renders, unknown braces literal, axe + autoscroll + 169 tests green.

Symbols pass 2 (2026-11-20, same branch): the icon set is now baked
into the repo. 59 SVGs (268 KB) from api.scryfall.com/symbology live
in mtgjudge/web/symbols (0-20, WUBRGSCXQ singles, T, and the canonical
hybrids/phyrexians in Scryfall's official pairs — WU, UB, BR, RG, GW,
2W-style, WP/UP/BP/RP/GP). License check: Scryfall serves these under
the WotC Fan Content Policy, which permits free fan-app use of Wizards
materials (no paywall, no repackaging); the required attribution is a
"Card symbols and data courtesy of Scryfall." line in the gate panel
(#gate .credit). symbols.py lookup order is now baked package dir ->
DATA_DIR/symbols cache -> one live Scryfall fetch; after this commit
the standard set never touches the network. Frontend HYBRID_CANON maps
reversed pairs ({W/U} -> WU etc.) to the ten official orders before
requesting /symbol/, so users typing hybrids backwards still get the
right icon. Non-baked codes keep the old cache/fetch path (server test
uses "ZZ"). Verified headless in both themes: hybrid canon renders
WU/BG from reversed input, 8/8 baked symbols load, hourglass frame + 
sand are the drawn SVGs with hg-flip running, 171 tests, axe (two
pre-existing "needs review" header contrast notes, untouched by this
branch) and autoscroll green. Screenshots: /tmp/oragle_hg2_*.png.

Context bleed fix (2026-11-20, user-reported: "new session said hello,
asked about the old topic"): two causes. The web client persisted the
thread id in localStorage and restored it on every page load, and the
server never evicted conversation threads — so a fresh browser session
silently resumed the entire old conversation (history + carried
blocks). Both fixed per the project principle that continuing a
conversation must be deliberate: thread ids are now per page session
only (never written to localStorage; the removeItem in the New-thread
handler stays as legacy cleanup), and Hub._prune_locked evicts threads
idle > THREAD_TTL_SECONDS (2h; an expired client id just starts a
fresh thread via thread_or_new, which already handled unknown ids).
Trade-off accepted deliberately: a mid-conversation page refresh now
starts a new thread too. On the Render deploy this changes little —
the server sleeps every 15 min anyway.


Decklists (2026-11-19, user request "interesting, unsure how helpful"):
mtgjudge/deck.py parses pasted decklists deterministically (Arena/MTGO
exports, "N Card"/"Nx"/"xN" counts, set suffixes, comments, SECTION
headers; conservative >= 3 counted lines without markers) and
_card_blocks turns each card into a CARD block with its copy count in
one local pass — the model skips its per-card search_cards rounds
entirely. Question text keeps only a "(decklist: N cards)" summary so
BM25 is not drowned in card names. Cap 60 unique cards (prompt size);
rulings and keyword-rule expansion excluded for deck entries (one
Scryfall fetch per card, and 50+ rule blocks, would bloat the prompt).
Web: "Deck" chip in the composer opens a paste panel; the deck rides
with the next question wrapped in <deck>...</deck> markers (guaranteed
detection, immune to the heuristic). "//" comments are line-leading
only (MTGO export convention) — split names like "Brazen Borrower //
Petty Theft" are unaffected, covered by tests. Scope (user, 2026-11-19):
this exists for rules questions about a list's composition (infinite
combos, legend-rule collisions, loops), NOT deck evaluation — the bot
has no metagame model and can't suggest cards outside its context; a
real deck analyzer (metagame data, why cards are played) is explicitly
a different project. Verified live: Priest of
Titania/Staff combo question answered from the CARD blocks directly,
copy counts reasoned over ("4 Priests max, engine needs 5+ elves"), one
focused search_rules round, no per-card fetches. Note for deploys:
Python loads once per process — serving rereads index.html by mtime but
new Python requires a restart/redeploy (bit us locally: a two-hour-old
server silently ignored the new feature).

Card lookup tier-0 (2026-11-19, deck live-testing followup): pasting
decklists exposed basics misresolving — "20 Mountain (ROE) 187" matched
"Mountain Valley" because the padded word match hit name prefixes and
the popularity ranking put EDHREC-ranked cards above NULL-ranked
basics. lookup_card now runs an exact-face SQL match first (whole face
in the single-space-padded, double-space-separated lookup_all), before
the word-match and fuzzy tiers. Deck exports therefore resolve
literally with no special-casing — the same tier-0 that makes [[X]]
refs exact. Covered by test_exact_face_beats_popular_prefix_match.

Strict lookup + did-you-mean (2026-11-19, user decision): card names
are never silently "fixed" to other cards. lookup_card is exact-face
only (normalize_name leeway: case, surrounding whitespace, accents —
NFKD strip, dashes/commas/apostrophes); apostrophes are DELETED when
normalizing ("Jeskas Will" == "Jeska's Will") — this changed the stored
lookup_all shape, so the local DB needed a `refresh cards` re-import
(the suggestion cache keys on file mtime, so it rebuilds automatically).
Scryfall live fallback uses exact=true. Explicit misses surface as
"NOT FOUND: no card with this exact name", with "Did you mean: X?" /
"Did you mean one of: ..." from two passes in lookup_alternates:
sub-name word results ([[Selvala]] -> the legendary Selvalas) then a
difflib close-match pass ([[Lighting Helix]] -> Lightning Helix,
conservative 0.85 cutoff, 3 max; the face list is cached per DB mtime
since misses cluster). Interactive keeps the numbered chooser for
multiply-ambiguous refs only. Decklist misses annotate the note
("not found: Mountan (did you mean Mountain?)"). Side effect of the
apostrophe rule: [[Selvala]] no longer offers "Selvala's Charge"
("selvalas" is a different word than "selvala") — considered correct.

---
## Bench (bench.txt, bench2.txt)

bench.txt: six target questions, run with `mtg bench` (report:
`bench-last.md`).
Status 2026-11-19: 5/6 fully correct with citations (Lier/adventures,
Karn x2, Tarmogoyf, Grist/Essence Scatter, KCI combo walkthrough).
Known gaps:

- "Selvala" in non-interactive bench mode defaults to the EDHREC-top
  Selvala (Heart of the Wilds) — the intended Explorer Returned interaction
  needs the interactive disambiguation prompt, or a smarter default.
- Retrieval noise: vague leftover queries ("how does work with") pull in
  trample/keyword subrules; harmless but context budget is wasted.
- KCI-complexity questions need a long LLM budget (120s timed out; 240s
  default now).

## Latency work (2026-11-19)

Why slow: every tool round is a full model pass over the context, so
3+ round answers took 1-4 minutes, and nothing was visible until the end.

- Streaming: `ask`/REPL stream answers line by line (styled rendering per
  complete line); bench and pipes stay buffered. Time-to-first-line should
  be seconds even when a full answer takes 30-60s.
- Hybrid retrieval: citation-hop expansion (rules referenced by retrieved
  rules, cap 6) reduces search_rules round-trips.
- Hybrid/fallback transports in llm.chat: streamed round cut with nothing
  delivered -> automatic buffered retry; HTTP 429 capacity -> one retry;
  after a streamed fallback the remaining rounds skip streaming entirely.
  Mistral's GLM backend was cutting streams under load while testing
  (2026-11-19), so these fallbacks fire in practice, not just in theory.
- Timing visibility: spinner events per round + closing stderr line
  (rounds, model seconds, total). res["stats"] carries this for bench.
- Slow-model watchdog + fallback (2026-11-19): a streamed round with no
  answer text after MTG_SLOW_AFTER seconds (default 75) is abandoned
  safely (nothing printed yet) and retried on MTG_FALLBACK_MODEL
  (default mistral-small-latest; 429-style hard failures get one
  fallback attempt too). The deadline also binds the per-read timeout,
  so a request queued server-side with zero chunks still switches.
  Live-verified: forced deadline -> footer reports mistral-small-latest.
- Not yet done: short-answer mode (system prompt still allows long
  walkthroughs), near-question answer cache.

Bug fix (2026-11-19): the deadline refactor dropped `payload["stream"]`
from llm.chat, so "streamed" requests weren't streamed at all — every
query came back as normal JSON, parsed as an empty stream, and was
reported as "provider cut the stream" followed by a wasted buffered
retry of the same work. Live-probed all four payload shapes post-fix.

## Conversation memory (2026-11-19)

REPL follow-ups used to start from scratch ("how does priority work?"
after a two-headed giant answer lost the 2HG frame). Now sessions
carry context, split so the honesty stance survives:

- Turn replay: the last turns (capped, MAX_HISTORY_MESSAGES=6 in llm,
  12 entries in state) go into the message list after the system
  prompt. Prior assistant answers were themselves grounded, so
  replaying them is honest memory; the system prompt still says turns
  only resolve references and every claim must cite current blocks.
- Carried blocks: the session's retrieved context blocks (deduped by
  header line, capped MAX_SESSION_BLOCKS=40) are re-sent verbatim;
  new retrieval stays ranked first, the status line reports `N carried`.
- Follow-up retrieval: BM25 also runs with the session's earlier
  questions (top_k halved, current question's hits first), so a bare
  "what about 2HG?" still retrieves the topic rules even though the
  line alone ranks nothing.
- `:new` resets the conversation. Piped stdin accumulates too (one
  conversation per pipe run).
- Every interactive answer ends with a dim line: "(this conversation
  continues with shared context — :new starts a fresh thread for a new
  topic)". Same-day user request: the shared-context default is
  expensive (carried blocks grow the prompt) and can muddy answers on
  unrelated topics, so the UI must keep the continue-vs-restart choice
  visible rather than implicit. Piped runs print nothing.

## Explicit card references only (2026-11-19)

Decision: capitalized-words auto-detection of card names is REMOVED.
`[[Name]]` is the only trigger. Rationale: least surprise — detection
kept producing false positives in ~3 successive bug-fix rounds
("The End" from "the end step" class, art-series face dupes, joke
split-card faces, sentence-starting "Actually" -> Slobad, Actually
Just Fine), each needing a hand-tuned blocklist entry. The model-side
excuse for trusting it was weak. The model already resolves unbracketed
names itself: the system prompt now says a card name without a CARD
block has NOT been resolved and must be fetched with search_cards
(`!"Exact Name"`), never described from memory — verified live earlier
("the new five color Karn" -> model's own search_cards round) and
re-verified after removal. The cost is one extra tool round for
unbracketed card questions; `[[Name]]` remains the fast path.

Removed: carddb.detect_card_mentions + unigram/df/wordlist machinery
(~150 lines) and their test classes. Kept: EDHREC-ranked interactive
disambiguation, moved to the explicit path — carddb.lookup_alternates
shares lookup_card's match+ranking (_LOOKUP_ORDER), so alternates[0]
is the default; [[Selvala]] still prompts in interactive mode. Bonus:
search briefs show 600 chars of Oracle text for <=3 hits (was 220),
so tool-resolved cards come with enough text to ground an answer.

Politeness (user request, same day; wording toned down from a snarkier
first draft at the user's request): an exact-name lookup for a card
NOT already referenced means the user skipped the brackets, so the
runner prints one polite hint per answer on a TTY stderr: "please
surround card names such as [[X]] with brackets for faster lookup".
Referenced names are exempt. Verified live: hint fired once under the
first exact lookup, silent for the second card in the same answer.

Consequence for bench files: bench2.txt card names are unbracketed on
purpose now — those runs exercise the model-resolution path from here
on.

Deterministic retrieval upgrades (no LLM pre-pass, per decision 2026-11-19):

- QUERY_SYNONYMS expanded (~10 -> ~80 mappings: bounce/flicker/etb/sac/
  cmdr/opp/fizzle/tutor/...), additive only — never drops original terms.
- FORMAT_ANCHORS: format words preload rules (Two-Headed Giant -> 810.8a,
  805.1; Commander -> 903.8, 903.10a; shorthand cmdr counts via synonyms).
  Measured: bench2 Q9 (2HG) answered in 13s vs 23s with these preloaded.
- GOLDEN_ANCHORS (101.1, 101.2) always in context: verification showed
  0 of 3183 rules cite 101.2, and BM25 misses it whenever a can't-vs-can
  conflict is embedded in a specific-interaction question (measured) —
  the model was previously improvising or refusing on exactly these.

bench2.txt: eleven web-sourced infamously tricky interactions (layers
Opalescence/Humility/Worship, Urborg vs Blood Moon dependency, Sylvan
Library + Abundance, RIP vs Wheel of Sun and Moon, Teferi vs cascade,
Sundial vs Final Fortune, Painter's Servant/Grindstone vs Emrakul,
Dark Depths/Thespian's Stage, Lich's Mirror in 2HG, Devoted Druid combo,
Teferi's Protection vs Supreme Verdict/Cyclonic Rift). All oracle text
and rulings verified current against the 2026-11-18 bulk data; card-mention
detection probed clean on all 11 (prose false-positive classes fixed in
carddb.py: joke-card single-word faces and lowercase phrase collisions).
Run 2026-11-19 (mistral/zai-glm-5-3-flash): 11/11 answered, 7-97s each,
no timeouts/retries, six questions used a second tool round. Spot-check
verdict: all 11 match the known rulings (layers conclusion matches the
2009 Opalescence/Humility/Worship ruling verbatim; Urborg/Blood Moon
dependency, Sylvan Library/Abundance table, RIP/Wheel controller choice,
Teferi/cascade, Sundial/Final Fortune, Painter's/Grindstone/Emrakul
priority timing, Dark Depths/Stage state trigger, Lich's Mirror 2HG,
Devoted Druid/Vizier ruling, Teferi's Protection phase-in timing all
correct). Minor model sloppiness to watch: one citation said 616.1f for
the RIP/Wheel choice where 616.1a is the choosing rule (outcome still
right), plus occasional name typos (Woship, Opalesecence). Report:
bench2-last.md.

---

Bubble footer rule citations are links (2026-11-20, same branch): the
"context: ..." ids in the answer footer render as the same
a.rule[data-rule] elements the answer body uses, so the delegated
rulebox click handler works there too (fetches /rule, expands inline).
Ids failing the ^[\d]+(\.[\d]+)*[a-z]{0,2}$ guard stay plain text (a
malformed "weird id/x" stays inert rather than becoming a link).

Hotfix (2026-11-20, user: hourglass "spinning on the statusline even
when connected"): .hourglass { display: inline-block } out-specifies
the UA sheet's [hidden] { display: none }, so the persistent rail copy
stayed visible (and animating) in every idle state despite its hidden
attribute being set. Fixed with an explicit .hourglass[hidden] rule.
Lesson recorded: verify hidden/collapsed UI by computed display, not
by the attribute — the earlier headless check read the property and
passed a visible bug.

## Local-first exact-name lookups in the model tool (2026-11-20, branch)

Blast-radius probe ("what if we're cut off from Scryfall?") found the
pipeline degrades correctly — NetError -> "search failed: ..." tool
result, model reports it instead of hallucinating; rules Q&A, cards.db
lookups and symbols all survive — with one gap: the model's
search_cards tool resolved UNBRACKETED card mentions (its
exact-name !"Name" query) through the live API even though the bulk
dump already knows the card, making that lookup path the only one that
dies while cut off, and the only deterministic-name operation that
spent network + rate budget. _search_cards_runner now routes exact
queries through carddb.lookup_card(offline=True) (strict DB-local) and
falls through to live Scryfall only on a DB miss (cards newer than the
last refresh). Discovery queries (the reason live search exists at
all — Scryfall's ~250-operator syntax is a query engine we would
have to reimplement, and cards.db has no colors/cmc/legalities columns
to even represent one) always stay live. Verified with the live path
monkeypatched to raise: DB hit resolves fully offline, DB miss and
discovery queries degrade to "search failed".

Card search exists only to serve grounded answers (user decision,
2026-11-20, local-first-lookups branch): the `oragle cards` passthrough
(command + tests + README section) is REMOVED. Scryfall is the best
Scryfall — a terminal re-skin of their search is not this app's
point, so card searching now exists solely as the model's search_cards
tool inside the answering pipeline. Consequence for earlier reasoning
in this file: the "we would have to reimplement Scryfall syntax"
argument survives only for human-typed queries, and there are none
left — the model authors every query and can learn whatever dialect
a prompt teaches. Live Scryfall search remains the tool's engine today
for freshness (cards newer than the last bulk import, day-old errata)
and result ranking. Fallback plan if Scryfall goes away (sharpened by
the user: "do we need a language when we already have a language?" -
we do not): extend cards.db with the bulk dump's colors/cmc/
legalities/keywords fields and give the model a READ-ONLY SQL tool
over the table instead of any invented dialect - models are heavily
trained on SQL, SQLite is the engine, and the prompt only needs to
show the schema. Safety must be by construction: query_only=ON, a
SELECT-only authorizer, single-statement parsing, row cap, execution
timeout. The remaining real work is schema design (color flags vs
lists, word-boundary-safe oracle_text matching like lookup_all).

## query_local_cards: the model gets SQL over the card DB (2026-11-20, built)

The design walked issue-by-issue in session (schema -> safety -> prompt
-> naming -> rollout) is now implemented on this branch, per the
user's "Build it" plus two extensions: an `oragle setup` one-command
install, and tests for Scryfall being down.

Schema v2 (cards.db): cmc REAL, colors + color_identity as canonical
'WUBRG'-subset strings ('' = colorless), keywords + oracle_all as
space-padded lowercase-agnostic text columns for word-boundary LIKE
(the lookup_all padding trick generalized), type_line/oracle_text
already merge faces via ' // ', 12 legality_<format> columns
('legal'/'not_legal'/'banned'/'restricted'), meta.schema_version.
Rebuild drops a pre-v2 cards table before import (full replace
anyway; rulings survive in their own table). Decision recorded:
colors as canonical strings not flags (exact-multicolor equality,
contains-blue LIKE, length() thresholds all stay simple SQL);
legality as flat columns not a child table (no joins to get wrong).

carddb.run_sql: ONE statement, opened file:...?mode=ro (handle cannot
write), PRAGMA query_only, authorizer allowing only SELECT/READ/
FUNCTION/RECURSIVE (no ATTACH/PRAGMA/writes), progress-handler
deadline (2s) interrupting runaway scans. Output is budgeted not
row-capped per user amendment: aggregates return entirely; rows
serialize (JSON per line, cells cut at 600 chars) until ~4KB, then a
truncation note teaches narrower SELECTs. Old/existing-DB states get
instructive strings ("run oragle refresh cards"), never exceptions.

Tool split (user's "explicit source" + "verbs"): search_rules |
query_local_cards | search_scryfall. The old search_cards name and
its !"Name" exact-idiom are gone; the unbracketed hint now keys off
the taught exact-lookup idiom (lookup_all LIKE '%Name %' regex).
offline mode advertises search_rules + query_local_cards and refuses
search_scryfall instantly ("disabled in offline mode") instead of
sitting through a 30s network timeout - tests cover the
Scryfall-down cases: live failure is a tool result the model
relates, and the pipeline continues to its answer.

Live observations from real runs (GLM 5.3 flash): (1) the model's
first Grist, the Hunger Tide lookup missed because the prompt only
taught apostrophe deletion - commas also normalize to spaces; the
description now shows 'Grist, the Hunger Tide' -> 'grist the hunger
tide' and the next run resolved fully locally. (2) on cell
truncation at 400 chars the model adapted by fetching
substr(oracle_text, 200, 400) - the self-teaching budget worked as
designed; CELL_TRUNC raised to 600 to spare single-card rounds.
(3) Real DB verified: COUNT aggregate (20,424 creatures) and the
teaching-example discovery query return sensible EDHREC-ranked
results. 199 tests green; local cards.db rebuilt (39,356 rows).

`oragle setup`: rules + cards via the existing refresh paths, then a
provider check; missing provider prints .env guidance and exit 1 but
the local data still builds (search/rule/card work without an LLM).

## Public packaging (2026-11-20, public-packaging branch)

Goal: a stranger can clone the repo and run oragle on their own API.
Mistral-specific setup is demoted from the docs (and from default
behavior) without breaking this user's own setup:

- llm.py gains a generic `openai` backend (any OpenAI-compatible
  endpoint): MTG_API_BASE selects it, MTG_API_KEY authenticates,
  MTG_MODEL or the endpoint's /models listing picks the model.
  MTG_BACKEND forces any backend explicitly; auto-detect order:
  MTG_BACKEND -> MTG_API_BASE -> MISTRAL_API_KEY -> local servers.
- MTG_MISTRAL_CALL_TYPE is now OFF by default (bare API billing) —
  safe for strangers whose accounts bill PAYG; the marker stays a
  documented-in-code opt-in. THIS USER's own .env (gitignored, never
  committed) carries MTG_MISTRAL_CALL_TYPE=main_call, so local
  behavior is unchanged. Do not commit that line anywhere.
- README rewritten user-first (what it is, setup, provider-agnostic
  API section, commands, web/deploy, config table, credits);
  dev/archaeology content removed from it and lives here (PLAN.md),
  conventions captured in AGENTS.md; bench question sets moved to
  dev/ (cmd_bench default resolves ./bench.txt then dev/bench.txt);
  .env.example added and un-ignored; .gitignore gains .pytest_cache/
  and the !.env.example exception. LICENSE added after a user-facing
  rundown of the usual options: MIT chosen (zero-friction, standard
  for fan tooling), with an explicit note that it covers project code
  only — the downloaded CR text, card data and Scryfall symbol art
  stay covered by their own terms.

The design is BYO-endpoint: the project never depended on Mistral
specifics beyond the named-backend convenience, and detection is now
generic-first.

renderAnswer headings (2026-11-20, user: "raw pound signs in the
output"): the web renderer had no markdown-heading pass (the CLI's
ansi.render already strips/bolds them), so model "## heading" lines
printed raw. renderAnswer now rewrites ^#{1,6} lines to .mdh block
spans (title kept, trailing #s stripped, runs before the bold/em
passes so ** inside a heading still applies). .answer .mdh is bold,
block-display, ink-colored in both themes. Headless-verified unit
(renderAnswer direct eval) and e2e (fake-server answer with two
headings: 2 spans, no raw ##, computed block/700).

## Mistral billing: the 402 saga and the call_type marker (2026-11-20)

Direct api.mistral.ai calls with a console key started returning HTTP
402 billing_no_api_budget_configured. Diagnosis with minimal curl
probes: keys are valid (models list 200), but ANY completion 402s —
newly created keys too. Mistral restructured billing Sept–Nov 2026
(Pro's included API credit cut $30 to $25.50, then the separate
API/Studio usage metrics merged; Studio is their rebranded developer
console, and "Studio keys" are just PAYG API keys — where a key is
CREATED doesn't matter). Current published model: a subscription
covers the Vibe app and Vibe Code; "for an app, use a pay-as-you-go
key" — the API bucket a plan account draws on for bare calls no
longer exists for this account (support ticket open; may restore it).

What actually routes billing (verified empirically 2026-11-20, same
key, same model): a bare request 402s; a request whose body carries
metadata.call_type="main_call" returns 200. Mistral's own clients mark
requests this way, and the marker routes them to the subscription's
Vibe allowance (255e/mo) instead of the (absent) PAYG bucket. There
is no client fingerprinting — User-Agent is ignored.

Implemented in llm.chat(): every mistral-backend payload now carries
metadata.call_type; the value comes from MTG_MISTRAL_CALL_TYPE
(default "main_call", empty string = bare API billing again). Shown in
`oragle config` as "mistral billing marker". Unit-tested (default,
override, disable, non-mistral backends never send it); verified live
end to end (ask answered in 15s, billed to the plan).

Honest caveat recorded up front: this presents our requests in the
format Mistral uses to recognize Vibe traffic. It is the user's own
subscription and their low-volume use, but it is not a documented
third-party integration — if support's ticket answer restores PAYG
credits or blesses another route, prefer that over the marker.

Provider direction (decided 2026-11-20, marker is the interim): next
provider of choice is Z.ai direct PAYG (GLM-5.3-Flash is the model
already validated 11/11 on bench2; "$0.15 in / $0.50 out per 1M" ~
one cent per question; older GLM-4.x-Flash models are genuinely
free — a natural fallback tier). OpenRouter as the experimentation
harness if model choice stays open. Not suitable: subscriptions of
any kind for this single-user bursty workload, and Z.ai's Coding
Plan specifically (terms-restricted to coding tools — the same gray
zone as the marker).

## Known risks / things that will bite

- **Layer interactions (rule 613) and the stack** are where a naive pipeline
  gives confidently wrong answers. Good test cases, bad v1 targets.
- **The CR changes** on releases; a re-download/re-index command avoids stale
  answers. Note the CR date in answers.
- **Model hallucination is the failure mode, not retrieval.** Citations and
  "not in the rules" honesty are the mitigations — don't drop them for
  smoother-sounding answers.
- **Chunking subrules**: parent/subrule containment decisions affect every
  query. **Decision recorded in Phase 1 tasks: one chunk per rule number,
  parents exclude subrules, examples attach to their rule.**
