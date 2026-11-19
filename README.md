# mtg-rules-judge

A local CLI that answers Magic: the Gathering rules questions using keyword
retrieval (BM25) over the Comprehensive Rules plus Scryfall card data, with an
LLM that must cite rule numbers. Goal: answers a human can verify by
citation, not an infallible judge. See `PLAN.md`.

Built with Python (stdlib only — no third-party runtime dependencies),
packaged with uv.

## Setup

```sh
uv sync                # creates .venv and installs the local `mtg` command
oragle refresh     # download + parse + index the Comprehensive Rules
oragle refresh cards   # build the local card database (~25 MB download)
```

Install it once as a global command (editable, so it tracks this checkout):

```sh
uv tool install --editable .
oragle ask "does deathtouch ignore damage prevention?"   # works from anywhere
```

Both names run the same CLI: `oragle` (installed by `uv tool install`) and
`uv run mtg` (inside the project). Examples below use `oragle`; substitute
`uv run mtg` if you haven't installed the tool.

## Commands

```sh
oragle search "deathtouch trample"      # retrieved rules, BM25 ranked
oragle rule 702.2                       # one rule plus its subrules
oragle stats                            # index stats, CR effective date
oragle card "Lightning Helix"           # local DB: oracle text + rulings
oragle card "Lightning Helix" --offline # no network at all
oragle ask "do blockers assign damage before attackers?" -v
oragle                                  # interactive session (REPL)
```

## Interactive mode

Run bare `oragle` for a prompt session (history via readline, Ctrl-D or
`:quit` to leave):

```
oragle — MTG rules judge (CR effective November 6, 2026)
Type a rules question; enter a :command (Tab completes), :quit to exit.
oragle> does Lightning Helix kill Grizzly Bears?
...answer with rule citations...
oragle> :quit
```

Anything that isn't a `:command` goes through the full ask pipeline
(citation answers, ambiguous `[[Name]]` refs prompt for a choice).
Card references are explicit only: wrap a card name in `[[Name]]` —
plain capitalized words are never auto-detected as cards. If you
mention a card without brackets, the model fetches its Oracle text
with its card-search tool (costs one extra round). Colon commands do
quick local lookups without the LLM: `:rule 704.5j`,
`:card Lightning Helix`, `:search deathtouch trample`, `:stats`, plus
session toggles `:backend`, `:model`, `:offline`, `:verbose` — `:help`
lists them all. A `:` prefix never prompts the model: unknown
`:commands` print the help instead, and the bare words `quit`/`exit`
print a "to quit, type `:quit`" reminder. Tab completion covers all
`:` commands.

### Follow-up questions

Questions in a session share context. The last few turns (question +
answer + retrieved rules) carry over, so you can ask `who goes first?`
and then just `I'm asking about two-headed giant` and the follow-up
resolves against the earlier topic. Follow-up retrieval also reruns the
search with the session's earlier questions, so topic rules resurface
even when the follow-up line alone is too vague to retrieve them. The
status line shows carried blocks as `N carried`.

This stays honest: earlier turns are only replayed to resolve
references, and the model must still ground every claim in the current
context blocks with rule-number citations. `:new` resets the
conversation. Because the shared-context default raises cost and can
muddy answers on a new topic, every REPL answer ends with a dim
reminder line: this thread continues with shared context — `:new`
starts a fresh thread for a new topic.

Piped stdin answers each line as a question, without prompts:
`echo "does deathtouch ignore damage prevention?" | oragle`.

Output is styled for terminals: bold/italic statements, cyan rule
citations, magenta card names for every referenced card, dim metadata.
Colors apply only when stdout is a TTY; `NO_COLOR` disables them and
`FORCE_COLOR` forces them on (e.g. for previews through pipes).

`ask` accepts inline card references with the `[[Name]]` syntax:

```sh
oragle ask "does [[Lightning Helix]] kill [[Grizzly Bears]]?"
```

Card Oracle text, its Scryfall rulings, and the rules for any keyword
abilities in the card text (derived from rule 702 headers) are added to the
retrieved context automatically.

**Card references are explicit only.** Capitalized words are never
auto-detected as cards — early versions did that and could not reliably
tell a card name from prose (a sentence-starting "Actually" matched the
joke card *Slobad, Actually Just Fine*). If you mention a card without
brackets, the model notices the missing CARD block and fetches the
Oracle text with its `search_cards` tool (`!"Exact Name"` query) —
honest, but it costs one extra model round, so `[[Name]]` is the fast
path. The app also reminds you: when an exact-name lookup runs for an
unbracketed card, the status line suggests surrounding card names like
`[[Name]]` with brackets for faster lookup (once per answer, interactive
only).

When a `[[Name]]` reference is ambiguous ([[Selvala]] — several
cards), interactive `ask` and the REPL prompt with a numbered list
ordered by EDHREC rank (Scryfall's popularity signal, stored in the
local card DB) and take the most popular on Enter. Piped runs and
`bench` skip the prompt and take the default.

## Progress indication & speed

Interactive `ask`/REPL answers **stream line by line as the model writes
them** (styled as they arrive), so the first lines of an answer usually
appear within seconds even when the full write-up takes half a minute.
Piped runs and `bench` stay buffered.

Latency anatomy, roughly: tool rounds dominate. Each round is a full
model pass over the whole context, so a 1-round answer is fast (~10-40s)
and a 3+ round answer can take minutes. Retrieval is kept deterministic
and token-free:

- Slang expansion (kill→destroy, bounce→return to hand, ETB, cmdr, ...)
  maps player vocabulary to Comprehensive Rules vocabulary before BM25.
- Format anchors preload format rules (Two-Headed Giant → 810.8a, 805.1;
  Commander → 903.8, 903.10a).
- Golden rules (101.1, 101.2 can't-beats-can) are always in context:
  no other rule cites them, so neither BM25 ranking nor the citation hop
  can guarantee their presence when a conflict is embedded in a
  specific-interaction question.
- A **citation hop**: rules cited by retrieved rules are added to the
  context up front, so the model rarely needs a `search_rules`
  round-trip.
- If the provider cuts a stream or is out of capacity, oragle falls back
  automatically: a stream closed with nothing delivered -> one buffered
  retry, HTTP 429 -> one retry; remaining rounds then skip streaming
  entirely so you don't pay that tax twice.
- Slow-model watchdog: a streamed attempt with no answer text after
  `MTG_SLOW_AFTER` seconds (default 75, `0` disables) is abandoned
  safely — thinking arrives but the answer never starts — and the round
  is retried on `MTG_FALLBACK_MODEL` (default `mistral-small-latest` on
  the mistral backend, empty disables). Hard failures (e.g. capacity)
  get one fallback attempt too. The footer shows which model actually
  answered.

Progress goes to stderr: the spinner with elapsed model time, event
lines for model actions (`→ search_rules(...)`, round numbers,
retries), and a closing timing line
(`→ done: 2 model round(s), 35s model time, 36.1s total`).
LLM requests default to 240s (`MTG_LLM_TIMEOUT`).

## Bench

`oragle bench [file]` (default `bench.txt`) runs all questions through
the ask pipeline, prints each answer live with timing, and writes a
reviewable markdown report (default `bench-last.md`). Use `--only 1,4` to
re-run specific questions.

## Card search (`cards` command and model tool)

```sh
oragle cards "c<=2 colors:blue t:instant o:exile legal:pauper"
```

Full Scryfall search syntax, live from their API (search is online; card
*lookups* go to the local DB). `mtg ask` exposes the same search to the LLM
as a `search_cards` tool, so plain-English discovery questions work:

```sh
oragle ask "are there any blue instants for 2 mana or less that can exile a creature?"
```

The model runs a Scryfall query, gets compact results back, and answers
from them. Use `--offline` to disable the tool.

A second, fully local tool — `search_rules` — lets the model fetch rules the
initial retrieval missed (legend rule, hybrid mana colors, priority): when
the context doesn't cover the question, the model searches the Comprehensive
Rules itself instead of refusing. It works with `--offline` too; only
`search_cards` needs the network. Tool rounds are bounded (default 3);
`MTG_LLM_TIMEOUT` controls the LLM request timeout (default 120s, one
automatic retry on timeout).

## Data on disk

Everything lives at `$MTG_DATA_DIR` or `~/.local/share/mtg-rules-judge/`:

- `rules.json` + `comp_rules.txt` — parsed Comprehensive Rules + raw text.
  `mtg refresh` (or `mtg refresh rules`) re-downloads after a CR release.
- `cards.db` — SQLite card database built from Scryfall's daily "Oracle
  Cards" bulk dump (one row per Oracle id, ~16 MB for ~39k cards).

Card lookups hit `cards.db` first, so `card` and `ask` work offline
(`--offline` makes that explicit). Cards printed after your last refresh
fall back to a live Scryfall lookup and rulings are cached on first fetch.

## Keeping the card database fresh

- At most one request per 24h to Scryfall's `/bulk-data` endpoint compares
  the upstream dump date with yours; if upstream is newer, commands print a
  notice suggesting `mtg refresh cards`.
- Network problems during that check are ignored silently — offline use is
  the point of the local DB. `--offline` skips the check entirely.
- `mtg refresh cards` does the actual re-download and rebuild (~4s).

## LLM backends

Auto-detected in this order, or force with `--backend`:

- `mistral` — if `MISTRAL_API_KEY` is set (default model
  `zai-glm-5-3-flash`)
- `lmstudio` — LM Studio server (Developer tab, `localhost:1234`); uses the
  first loaded model
- `ollama` — `localhost:11434/v1`

Model overrides: `--model` or the `MTG_MODEL` environment variable. Timeout:
`MTG_LLM_TIMEOUT` (seconds).

For standalone (outside Vibe) use, put the key in a `.env` file instead of
your shell rc — the CLI loads `$MTG_ENV_FILE`, then `./.env`, then
`~/.local/share/mtg-rules-judge/.env` at startup (the real environment always
wins; the file is gitignored):

```sh
echo 'MISTRAL_API_KEY=<your key from console.mistral.ai>' > .env
chmod 600 .env
```

Note: running inside a Mistral Vibe session needs no setup — Vibe injects its
own configured key into the environment.

## Tests

```sh
uv run python -m unittest discover -s tests -v
```

## Chunking decision

Each rule number (`117.5`, `702.2c`) is its own chunk. A parent rule's chunk
contains only its own text, never subrules; `mtg rule` and `ask` context
assembly pull subrules in explicitly when needed. Example paragraphs attach
to the rule they follow.

## Configuration

Settings are plain environment variables; put them in `.env` (the project
root file is found automatically, from any working directory) to make
them permanent — that file is the config file. `oragle config` (or `:config`
in the session) shows every effective value and where it came from:

```sh
oragle config
# backend: mistral  [environment]
# model: zai-glm-5-3-flash
# fallback model: mistral-small-latest
# slow switch after: 75s (0 disables)   [/home/you/.local/share/.../.env]
# llm timeout: 240s
```

| Variable | Default | Meaning |
|---|---|---|
| `MISTRAL_API_KEY` | — | API key (also works for any OpenAI-compatible backend via `MTG_API_KEY`-style env) |
| `MTG_MODEL` | backend default | model override (`:model`, `--model`) |
| `MTG_FALLBACK_MODEL` | `mistral-small-latest` on mistral | model used when the primary is too slow or failing; empty disables |
| `MTG_SLOW_AFTER` | `75` | seconds without answer text before switching (0 disables) |
| `MTG_LLM_TIMEOUT` | `240` | per-request timeout |
| `MTG_DATA_DIR` / `MTG_ENV_FILE` | — | data and env file locations |

In an interactive session, if the watchdog switches to the fallback
model, the session stays on it (`staying on ... for this session`) — a
model that needed rescuing once is usually slow for a while — until you
change it with `:model <name>`.
