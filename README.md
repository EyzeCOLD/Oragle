# Oragle — MTG rules judge

A local app that answers Magic: the Gathering rules questions with
**rule-number citations**, grounded in the official Comprehensive Rules and a
copy of every Oracle card text on your disk. Its goal is answers a human can
verify by citation — not an infallible judge, and it says so when the rules it
retrieved don't cover the question.

Two frontends, one engine: a terminal CLI (one-shot questions and an
interactive session) and a small web app you can put behind a token for
your phone. Everything is local-first: rules and card data live on your
machine, and the model only ever sees retrieved context.

Built with Python 3.10+ (standard library only — zero runtime
dependencies), packaged with [uv](https://docs.astral.sh/uv/). Bring your
own LLM: any OpenAI-compatible API, plus LM Studio or Ollama for fully
local answering.

## Setup

```sh
git clone https://github.com/EyzeCOLD/Oragle.git
cd Oragle
uv sync                     # creates .venv; the `oragle` command is runnable

uv run oragle refresh       # download + parse + index the Comprehensive Rules
uv run oragle refresh cards # build the local card database (~25 MB download)
cp .env.example .env        # then edit .env (see below) and put your API key in
```

Install the CLI globally (editable, tracks this checkout):

```sh
uv tool install --editable .
oragle ask "does deathtouch ignore damage prevention?"   # works from anywhere
```

## Pointing it at your LLM

Oragle speaks the OpenAI chat-completions protocol, so it works with any
compatible provider — OpenRouter, OpenAI, Z.ai, Groq, Together, Mistral,
DeepSeek, ... Set these in `.env` (or your shell):

```sh
MTG_API_BASE=https://openrouter.ai/api/v1
MTG_API_KEY=sk-...
MTG_MODEL=<provider/model-name on your provider>
```

`MTG_MODEL` is required (or auto-picked from the provider's `/models`
listing where available). To just try a provider quickly without editing
files:

```sh
MTG_API_BASE=... MTG_API_KEY=... MTG_MODEL=... uv run oragle ask "who goes first?"
```

Local models, no cloud, no key:

- `MTG_BACKEND=lmstudio` — the LM Studio server (Developer tab, `localhost:1234`); uses the first loaded model
- `MTG_BACKEND=ollama` — `localhost:11434/v1`

`MTG_BACKEND=<name>` also forces a backend when several are configured;
otherwise oragle auto-detects (custom endpoint first, then a
`MISTRAL_API_KEY` if set, then local servers). `oragle config` shows
what it picked and where every setting came from.

## Commands

```sh
oragle search "deathtouch trample"      # retrieved rules, BM25 ranked
oragle rule 702.2                       # one rule plus its subrules
oragle stats                            # index stats, CR effective date
oragle card "Lightning Helix"           # local DB: oracle text + rulings
oragle card "Lightning Helix" --offline # no network at all
oragle ask "do blockers assign damage before attackers?" -v
oragle                                  # interactive session (REPL)
oragle serve                            # web app on http://127.0.0.1:10000
oragle cards "c<=2 colors:blue t:instant o:exile legal:pauper"
```

### Interactive mode

Run bare `oragle` for a prompt session (history via readline, `:quit` or
Ctrl-D to leave). Anything that isn't a `:command` goes through the full
ask pipeline. Colon commands are quick local lookups that never prompt
the model — `:rule 704.5j`, `:card Lightning Helix`,
`:search deathtouch trample`, `:stats`, plus `:backend`/`:model`/
`:offline`/`:verbose` toggles and Tab completion. A `:` prefix never
prompts the model: unknown commands print the help, and the bare words
`quit`/`exit` explain how to actually quit. Piped stdin answers each line
without prompts.

### Follow-ups, threads, and honesty

Questions in a session share context: the last few turns (including the
rules that were retrieved) carry over, follow-up retrieval reuses the
session's earlier questions, and the model must still ground every claim
in the current context blocks with citations. Because that default costs
money and can muddy a new topic, every answer ends with a dim reminder,
and `:new` starts a fresh thread.

### Card references

Card lookups are **explicit**: wrap the name in `[[Name]]`.

```sh
oragle ask "does [[Lightning Helix]] kill [[Grizzly Bears]]?"
```

Oracle text, Scryfall rulings, and keyword-ability rules from the card
text are added to the context automatically. Capitalized words are never
auto-detected as cards — if you mention one without brackets, the model
fetches it with its card-search tool (honest, but one extra model
round), and the app suggests `[[Name]]` for faster lookup. Ambiguous
names (`[[Selvala]]`) prompt with a numbered list ordered by popularity;
piped runs take the default.

### Decklists

Paste a decklist into the question and every card is resolved locally in
one pass — the model gets all the Oracle text immediately:

```sh
oragle ask "$(printf 'any infinite combos here?\n4 Priest of Titania\n20 Mountain (ROE) 187\nStaff of Domination x1\n')"
```

Recognized: `4 Card` / `4x Card` / `Card x4` counts, set suffixes
(`(ROE) 187`), `//`/`#` line-leading comments (split-card names like
`Brazen Borrower // Petty Theft` are untouched), and Arena-style
section headers. Decks cap at 60 unique cards. This is for rules
questions about a list's composition ("does this go infinite?"), not
deck advice — the model has no metagame data.

### Speed and progress

Answers stream line by line as the model writes them. Retrieval is
deterministic and token-free: player slang is expanded to CR vocabulary
before ranking, format keywords preload their rules, the golden rules
(101.1, 101.2) are always in context, and rules cited by retrieved rules
are pulled in up front. The model can run two more retrieval tools
itself (`search_rules`, always local; `search_cards`, full Scryfall
syntax, online) when the initial context misses something. A
stream/timeout watchdog can switch to a fallback model mid-answer.
Progress, retries, and timing print to stderr.

## Data on disk and staying fresh

Everything lives at `$MTG_DATA_DIR` or `~/.local/share/mtg-rules-judge/`:

- `rules.json` — the parsed, indexed Comprehensive Rules. `oragle refresh`
  re-downloads after a CR release.
- `cards.db` — SQLite, built from Scryfall's Oracle Cards bulk dump
  (~16 MB, one row per Oracle id). `oragle refresh cards` rebuilds it.

Card lookups hit `cards.db` first, so `card` and `ask` work offline
(`--offline` makes it explicit). At most one request per 24 h asks
Scryfall whether the bulk dump was updated; if it's newer than yours,
commands print a notice suggesting a refresh.

## Web app

```sh
echo 'WEB_TOKEN=pick-a-long-random-string' >> .env
oragle serve                        # binds 127.0.0.1:10000
```

Feature parity with the CLI: same retrieval, threads (the New-thread
button is `:new`), `[[Name]]` refs, deck panel, progress notes and
hint lines. Rule numbers in answers and in the footer are links that
expand rule text from the local index; card names link to Scryfall;
mana symbols render with Scryfall's icon set (bundled, offline).

### Hosting it (optional)

The repo's `Dockerfile` and `render.yaml` describe a free-tier Render
deployment: rules and cards are baked into the image at build time,
secrets (your API key, `WEB_TOKEN`) are environment variables. Requests
are jobified (`POST /ask` returns a job id; the browser polls) because
hosted proxies cut long requests before answers finish. The page
detects the free-tier wake-up and tells the user. Any host that runs a
Docker image works the same way.

## Bench

`oragle bench [file]` runs a question file through the ask pipeline and
writes a markdown report. The repo ships two sets in `dev/`: `bench.txt`
(common tricky cases) and `bench2.txt` (eleven infamously hard
interactions). `--only 1,4` re-runs specific questions.

## Configuration

Settings are plain environment variables; `.env` (auto-found next to the
checkout, `$MTG_DATA_DIR/.env`, or `$MTG_ENV_FILE`) is the config file,
and the real environment always wins. `oragle config` / `:config` shows
every effective value and its origin.

| Variable | Default | Meaning |
|---|---|---|
| `MTG_API_BASE` | — | any OpenAI-compatible endpoint root, e.g. `https://openrouter.ai/api/v1` |
| `MTG_API_KEY` | — | bearer key for that endpoint |
| `MTG_MODEL` | backend default | model name (required for custom endpoints without a listing) |
| `MTG_BACKEND` | auto | force `openai`/`mistral`/`lmstudio`/`ollama` |
| `MISTRAL_API_KEY` | — | shortcut: selects api.mistral.ai as the backend |
| `MTG_FALLBACK_MODEL` | empty | switch to this model when the primary is too slow or failing |
| `MTG_SLOW_AFTER` | `75` | seconds without answer text before switching (0 disables) |
| `MTG_LLM_TIMEOUT` | `240` | per-request timeout, seconds |
| `MTG_DATA_DIR` / `MTG_ENV_FILE` | — | data and env-file locations |
| `WEB_TOKEN` | — | required by `serve` (bearer token for the web app) |
| `NO_COLOR` / `FORCE_COLOR` | — | disable / force terminal styling |

## Development

```sh
uv run --with pytest pytest -q          # test suite
```

Design decisions, mechanics notes, and the project history live in
`PLAN.md`; the working conventions are in `AGENTS.md`; development-only
tools (bench question sets, the accessibility audit) are in `dev/`.

This is an unofficial fan tool. Magic: the Gathering is a trademark of
Wizards of the Coast; the Comprehensive Rules are published by Wizards
of the Coast. Card data and symbol art are courtesy of
[Scryfall](https://scryfall.com) — this project is not affiliated with
or endorsed by Wizards of the Coast or Scryfall.

The project's own code is licensed under the [MIT License](LICENSE);
the card data, symbol art, and Comprehensive Rules it downloads are not
covered by that license.
