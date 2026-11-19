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

## Web remote access (plan, not built)

Goal: ask rules questions from the phone, PC off, using my own Mistral
budget. The pipeline (BM25 retrieval, card DB, tool loop, key) must run
somewhere — GitHub Pages can't do it.

Architecture: small stdlib HTTP server (`server.py`) wrapping
`answer_question()` directly (it already returns answer + context ids):

- `POST /ask {"question": ...}` -> `{"answer", "context_ids", "elapsed"}`;
  single origin also serves a one-page frontend (no CORS, no Pages needed).
- Data deployed with the container: `rules.json` + `cards.db`
  (or first-boot `refresh`).
- Auth: bearer token in a header (the endpoint fronts my Mistral key);
  long request timeout, streaming or spinner-equivalent ping so the phone
  UI shows churn during 10-90 s answers.
- Hosting options, in order of preference: Cloud Run (per-request billing,
  effectively free at personal volume) > Hetzner/Oracle always-on VM >
  Raspberry Pi at home behind Tailscale (zero public exposure, no auth
  needed). Dockerfile + deploy config; no runtime deps to install.

Frontend ideas (when served as HTML, cheap wins over terminal output):

- Card names in answers link to Scryfall (`scryfall.com/card/<set>/<num>`,
  or the `scryfall_uri` fetched at lookup time; store it in cards.db or
  use `scryfall.com/search?q=!"<name>"` as a lazy fallback).
- Rule-number citations become links: `#702.2b` view showing the rule
  block (already available from the local index — endpoint or client-side
  from the JSON).
- Collapse/show official rulings per card on tap; show which rulings the
  answer relied on.
- History of recent questions in localStorage; PWA manifest so it installs
  to the home screen.

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
