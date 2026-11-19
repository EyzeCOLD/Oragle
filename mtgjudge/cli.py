"""mtg — Magic rules CLI.

Commands:
  mtg refresh [url]                    download + parse + persist the CR
  mtg stats                            show index stats
  mtg search <query...> [--top N]      BM25 search over rules
  mtg rule <NUMBER>                    one rule plus its subrules
  mtg card <name>                      oracle text + rulings via Scryfall
  mtg ask <question...> [--verbose]    retrieved rules (+cards) -> LLM answer
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from . import carddb, cards, cr, envfile, llm, net, search, store
from . import ansi
from .spinner import Spinner


def cmd_refresh(args):
    target = args.target or "rules"
    if target == "rules":
        url, text = cr.download_cr(args.url)
        meta, chunks = cr.parse_cr(text)
        meta["source_url"] = url
        store.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (store.DATA_DIR / "comp_rules.txt").write_text(text, encoding="utf-8")
        path = store.save_rules(meta, chunks)
        s = store.stats(chunks)
        print(f"Downloaded: {url}")
        print(f"CR effective date: {meta['effective_date']}")
        print(f"Parsed {s['rules']} rules across {s['chapters']} chapters.")
        print(f"Biggest chapter: {s['biggest_chapter']} ({s['biggest_chapter_rules']} rules)")
        print(f"Index saved to {path}")
    else:  # cards
        entry = carddb.bulk_entry()
        size_mb = entry.get("compressed_size", 0) / 1e6
        print(f"Downloading Oracle Cards bulk dump ({size_mb:.0f} MB, "
              f"updated {entry['updated_at'][:10]})...")
        count = carddb.build_from_bulk(entry)
        print(f"Imported {count} cards into {carddb.DB_FILE}")
    return 0


def cmd_stats(args):
    meta, chunks = store.load_rules()
    s = store.stats(chunks)
    print(f"CR effective date: {meta['effective_date']}")
    print(f"Rules: {s['rules']} in {s['chapters']} chapters")
    print(f"Biggest chapter: {s['biggest_chapter']} ({s['biggest_chapter_rules']} rules)")
    return 0


def cmd_search(args):
    meta, chunks = store.load_rules()
    query = " ".join(args.query)
    results = search.search_rules(chunks, query, top_k=args.top)
    if not results:
        print(f"No rules matched {query!r}.")
        return 1
    for rule, score in results:
        _print_rule(rule, score)
    print(f"(CR effective {meta['effective_date']})")
    return 0


def cmd_rule(args):
    meta, chunks = store.load_rules()
    try:
        rules = cr.match_rule(chunks, args.number)
    except KeyError:
        print(f"No rule {args.number}.")
        return 1
    for rule in rules:
        _print_rule(rule)
    print(f"(CR effective {meta['effective_date']})")
    return 0


def cmd_card(args):
    name = " ".join(args.name)
    notice = carddb.freshness_notice(offline=args.offline)
    if notice:
        print(f"note: {notice}", file=sys.stderr)
    try:
        card = carddb.lookup_card(name, offline=args.offline)
    except cards.CardNotFound as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    rulings = carddb.get_rulings(card, offline=args.offline)
    blob = cards.format_card(card)
    first, _, rest = blob.partition("\n")
    print(ansi.card_name(first))
    print(ansi.render(rest))
    if rulings:
        print("\n" + ansi.styled("Rulings:", ansi.BOLD, ansi.BRIGHT_CYAN))
        print(ansi.render(cards.format_rulings(rulings)))
    elif not args.offline:
        print("\nNo rulings in Scryfall's database.")
    else:
        print("\n(Rulings not cached yet; run online once to fetch them.)")
    return 0


def cmd_cards(args):
    query = " ".join(args.query)
    found, total = cards.search_cards(query, limit=args.top)
    if not found:
        print(f"No cards matched {query!r}. Check the Scryfall syntax "
              "(e.g. c<=2 colors:blue t:instant o:exile).")
        return 1
    print(f"{total} match(es); showing {len(found)}:\n")
    for card in found:
        print(cards.format_card(card))
        print()
    return 0


def _print_rule(rule, score=None):
    heading = " - ".join(h for h in [rule["chapter"], rule["section"]] if h)
    suffix = f"  (score {score:.3f})" if score is not None else ""
    print(ansi.styled(f"[{heading}]", ansi.GRAY))
    text = ansi.render(rule["text"] + suffix)
    print(f"{ansi.styled(rule['number'], ansi.BOLD, ansi.CYAN)} {text}\n")


SYSTEM_PROMPT_TEMPLATE = """You are a Magic: the Gathering rules judge. Answer the user's question using ONLY the context blocks provided (Comprehensive Rules excerpts, card Oracle text, and official rulings from Scryfall).

Answering rules:
- Ground every claim in the provided context. Cite the exact rule number(s) (e.g. 702.2b) for every statement about the rules.
- When you rely on a card's Oracle text or an official ruling, say so.
- If the retrieved context does not cover the question, say exactly: "This is not covered in the retrieved rules." Do not guess or fill gaps from memory.
- Never invent rule numbers or rule text. Only cite rules that appear in the context blocks.
- Be concise: direct answer first, then the reasoning with citations.
- The rules excerpts are from the Comprehensive Rules, effective {date}.
- If the user asks which cards exist or meet some criteria (mana value, color, format legality, oracle text containing something), use the search_cards tool with a Scryfall syntax query instead of answering from memory. Examples: color<=2 colors:blue oracle:"exile target creature". Format-legal questions use legal:pauper / legal:modern / etc.
- Card references the user intended arrive as blocks titled CARD <name>. A card name in the question without such a block has NOT been resolved: use the search_cards tool with an exact-name query (e.g. !"Lightning Helix") to fetch its Oracle text, then answer grounded in the returned text. Never describe a card's printed text from memory.
- If part of the question involves rules NOT present in the context blocks (e.g. the legend rule for two copies of a legendary card, or how hybrid mana symbols determine color), do not fill the gap from memory: use the search_rules tool with rule keywords (e.g. "legendary permanent same name" or "hybrid mana symbol colors") to fetch the relevant rules, then answer citing them.
- The conversation may include earlier turns; use them only to resolve references in the current question (e.g. "what about two headed giant?" refers to the earlier topic). Earlier turns never justify claims on their own: every statement in your answer must still be grounded in the current context blocks and cited, or met with "This is not covered in the retrieved rules."
"""

SEARCH_CARDS_TOOL = {
    "type": "function",
    "function": {
        "name": "search_cards",
        "description": (
            "Search Magic: the Gathering cards with a Scryfall syntax query. "
            "Use for 'which cards...' questions. Filters include color/mana "
            "(c<=2), colors (colors:blue or c:u), type (t:instant), oracle text "
            "(o:exile, o:\"exile target creature\"), legality (legal:pauper), "
            "and free text. Multiple filters are space-separated AND."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Scryfall search syntax, e.g. c<=2 colors:blue t:instant o:exile",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max cards to return, default 8, max 25",
                },
            },
            "required": ["query"],
        },
    },
}


SEARCH_RULES_TOOL = {
    "type": "function",
    "function": {
        "name": "search_rules",
        "description": (
            "Keyword search over the Magic Comprehensive Rules (local index). "
            "Use when the provided context blocks lack a rule the question "
            "needs (legend rule, hybrid mana colors, state-based actions, "
            "priority, ...). Returns the top rules with their numbers and text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Rules keywords, rule vocabulary works best, e.g. 'legendary permanents same name'.",
                },
                "top": {
                    "type": "integer",
                    "description": "Max rules to return, default 6, max 15",
                },
            },
            "required": ["query"],
        },
    },
}


def _search_cards_runner(name, args):
    if name != "search_cards":
        return f"error: no such tool '{name}'"
    query = (args.get("query") or "").strip()
    if not query:
        return "error: empty query"
    limit = max(1, min(int(args.get("limit") or 8), 25))
    try:
        found, total = cards.search_cards(query, limit=limit)
    except Exception as e:  # noqa: BLE001 - report search failures to the model
        return f"search failed: {e}"
    if not found:
        return "0 cards matched. Try a broader or corrected Scryfall query."
    return cards.format_search_brief(found, total)


EXACT_QUERY_RE = re.compile(r'^!\s*"([^"]+)"|^!\s*(\S+)$')

# An exact-name search_cards lookup (!"Name") happens only when the
# question named a card without [[ ]] brackets -- user-caused extra
# work. Point it out politely: once per answer, interactive stderr
# only, never for names already resolved via [[Name]].
UNBRACKETED_HINT = ("please surround card names such as [[{name}]] with "
                    "brackets for faster lookup")


def _exact_card_query(query):
    """The card name from an exact-name Scryfall query, else None."""
    m = EXACT_QUERY_RE.match(query)
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip() or None


def make_tool_runner(chunks, referenced=()):
    """Runs both model tools: search_cards (Scryfall, online) and
    search_rules (local BM25 over the Comprehensive Rules).
    `referenced` names (cards already resolved from [[ ]] refs) suppress
    the unbracketed-name hint."""

    referenced_names = {carddb.normalize_name(n) for n in referenced}
    hinted = {"done": False}

    def hint(name):
        if hinted["done"] or not sys.stderr.isatty():
            return
        if carddb.normalize_name(name) in referenced_names:
            return  # the user did bracket this one
        hinted["done"] = True
        print("  " + ansi.styled(UNBRACKETED_HINT.format(name=name),
                                 ansi.YELLOW),
              file=sys.stderr, flush=True)

    def runner(name, args):
        if name == "search_cards":
            card = _exact_card_query((args.get("query") or "").strip())
            if card:
                hint(card)
            return _search_cards_runner(name, args)
        if name == "search_rules":
            query = (args.get("query") or "").strip()
            if not query:
                return "error: empty query"
            top = max(1, min(int(args.get("top") or 6), 15))
            results = search.search_rules(chunks, query, top_k=top)
            if not results:
                return (
                    "No rules matched. Try Comprehensive Rules vocabulary "
                    "(destroy/dies/cast/battlefield/state-based actions) "
                    "instead of slang."
                )
            return "\n\n".join(_rule_block(rule) for rule, _score in results)
        return f"error: no such tool '{name}'"

    return runner


def _rule_block(rule):
    heading = " - ".join(h for h in [rule["chapter"], rule["section"]] if h)
    return f"RULE {rule['number']} [{heading}]\n{rule['text']}"


def _describe_card(card):
    cost = card.get("mana_cost", "")
    year = (card.get("released_at") or "")[:4]
    bits = [card["name"], cost, card.get("type_line", "")]
    parts = " — ".join(b for b in bits if b)
    rank = card.get("edhrec_rank")
    if rank:
        parts += f" [EDHREC #{rank}]"
    elif year:
        parts += f" ({year})"
    return parts


def _choose_card(word, default, alternates, prompt=input):
    """Interactive disambiguation: numbered list, Enter keeps the default.
    Returns the chosen card or None to skip the mention entirely."""
    print(f"\n'{word}' is ambiguous. Which card did you mean?")
    options = [default] + list(alternates)
    for i, card in enumerate(options, 1):
        print(f"  {i}) {_describe_card(card)}")
    print("Enter = 1, or pick a number (0 = don't reference this card):")
    try:
        answer = prompt(f"choice [1-{len(options)}] (default 1): ").strip()
    except (EOFError, KeyboardInterrupt):
        return default
    if not answer or answer == "1":
        return default
    if answer == "0":
        return None
    try:
        index = int(answer)
    except ValueError:
        return default
    if 2 <= index <= len(options):
        return options[index - 1]
    return default


def _card_blocks(chunks, question, offline=False, interactive=False):
    """Resolve cards for a question. References are explicit only:
    [[Name]]. (Automatic detection of capitalized plain words was removed
    — telling a card name from prose was too finicky; an unbracketed
    name is the model's job, resolved with the search_cards tool.)
    Ambiguous refs like [[Selvala]] prompt interactively. Returns
    (release_question, blocks); release_question keeps the card names
    as plain words for the rules search."""
    blocks = []
    explicit, cleaned = cards.extract_card_refs(question)
    seen = set()
    referenced = []
    for name in explicit:
        try:
            card = carddb.lookup_card(name, offline=offline)
        except cards.CardNotFound as e:
            print(f"warning: {e}", file=sys.stderr)
            blocks.append(f"CARD {name}\nNOT FOUND (local database and Scryfall).")
            continue
        if interactive:
            alternates = carddb.lookup_alternates(name, limit=5)
            if len(alternates) > 1:
                chosen = _choose_card(name, alternates[0], alternates[1:5])
                if chosen is None:
                    continue  # user said this isn't a card reference
                card = chosen
        if card["id"] in seen:
            continue
        seen.add(card["id"])
        referenced.append(card)
        blocks.append(f"CARD {card['name']}\n{cards.format_card(card)}")
        rulings = carddb.get_rulings(card, offline=offline)
        rulings_text = cards.format_rulings(rulings)
        if rulings_text:
            blocks.append(f"OFFICIAL RULINGS for {card['name']}\n{rulings_text}")
    if referenced:
        print("cards referenced: " + ", ".join(c["name"] for c in referenced), file=sys.stderr)
        oracle = " ".join(c.get("oracle_text", "") or "" for c in referenced)
        for kw_name, rule_num in cr.keyword_abilities(chunks).items():
            if re.search(rf"\b{re.escape(kw_name)}\b", oracle, re.IGNORECASE):
                for rule in cr.match_rule(chunks, rule_num):
                    blocks.append(_rule_block(rule))
    return cleaned, blocks


def _context_ids(blocks):
    """Deterministic list of context block ids (rule numbers, card names)."""
    ids = re.findall(
        r"^(?:RULE|OFFICIAL RULINGS for|CARD) (.*?)(?:\s*\[|\n|$)",
        "\n".join(blocks), re.MULTILINE,
    )
    return list(dict.fromkeys(ids))


def _watchdog_settings(backend):
    """Slow-model watchdog knobs. MTG_SLOW_AFTER seconds without answer
    text (default 75, 0 disables) triggers a switch to
    MTG_FALLBACK_MODEL (default mistral-small-latest on the mistral
    backend; empty string disables the fallback entirely)."""
    try:
        slow_after = float(os.environ.get("MTG_SLOW_AFTER", "75") or 0)
    except ValueError:
        slow_after = 75.0
    fallback_model = os.environ.get(
        "MTG_FALLBACK_MODEL",
        "mistral-small-latest" if backend == "mistral" else "").strip()
    return slow_after, fallback_model


def settings_report(backend=None, model=None):
    """Effective settings with their provenance, for `oragle config`
    and the REPL `:config`."""
    backend = backend or llm.resolve_backend()
    model = model or os.environ.get("MTG_MODEL") or llm.default_model(backend)
    slow_after, fallback_model = _watchdog_settings(backend)
    return [
        ("backend", backend, envfile.origin("MISTRAL_API_KEY")
         if backend == "mistral" else None),
        ("model", model, envfile.origin("MTG_MODEL")),
        ("fallback model", fallback_model or "(none)",
         envfile.origin("MTG_FALLBACK_MODEL")),
        ("slow switch after", f"{slow_after:.0f}s (0 disables)",
         envfile.origin("MTG_SLOW_AFTER")),
        ("llm timeout", f"{net.llm_timeout()}s",
         envfile.origin("MTG_LLM_TIMEOUT")),
        ("command", "oragle config   (REPL: :config)", None),
    ]


def cmd_config(args):
    backend = args.backend if hasattr(args, "backend") else None
    try:
        rows = settings_report(backend)
    except llm.LLMError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for name, value, source in rows:
        line = f"{ansi.styled(name, ansi.BOLD)}: {value}"
        if source:
            line += ansi.styled(f"  [{source}]", ansi.GRAY)
        print(line)
    return 0


def _make_stream_printer(card_names, spin):
    """Line-buffered styled streaming: text deltas arrive in small pieces,
    but styling (bold/cards/rules) applies to complete lines, which arrive
    within a second of each other. The spinner stops at the first delta;
    `finish` flushes the last partial line."""
    buf = []
    started = False

    def hook(delta):
        nonlocal started
        if not started:
            started = True
            spin.stop()
        buf.append(delta)
        if "\n" in delta:
            pending = "".join(buf)
            buf.clear()
            lines = pending.split("\n")
            buf.append(lines.pop())
            for line in lines:
                print(ansi.render(line, card_names=card_names), flush=True)
                _mark_printed()

    def finish():
        """Stops the spinner, flushes the last partial line. Returns True
        if any text was streamed live — callers use that to know whether
        the answer is already on screen or must be printed at the end."""
        if not started:
            spin.stop()
        if buf:
            print(ansi.render("".join(buf), card_names=card_names), flush=True)
            buf.clear()
        return any_printed

    any_printed = False

    def _mark_printed():
        nonlocal any_printed
        any_printed = True

    return hook, finish, _mark_printed


RULE_REF_RE = re.compile(r"rules?\s+(\d{3}\.\d{1,3}[a-z]{0,2})")
MAX_HOP_RULES = 6
# Earlier turns' context blocks carried into later questions, capped so a
# long session cannot grow the prompt without bound.
MAX_SESSION_BLOCKS = 40
# Golden rules are always in context: universally applicable meta-rules
# that no other rule ever cites (checked: 0 chunks reference 101.1/101.2),
# so neither BM25 ranking nor the citation hop guarantees their presence
# when a can't-vs-can conflict is embedded in a specific question.
GOLDEN_ANCHORS = ("101.1", "101.2")


def answer_question(question, top=8, offline=False, backend=None, model=None,
                    spinner=None, interactive=False, stream_output=False,
                    history=None, prior_blocks=None):
    """One full `ask` pipeline run. Returns a result dict; used by the ask,
    bench, and interactive commands. Progress goes to stderr (or the given
    spinner). stream_output prints the answer live, line by line."""
    start = time.time()
    meta, chunks = store.load_rules()
    cleaned, blocks = _card_blocks(chunks, question, offline=offline,
                                   interactive=interactive)
    cleaned = cleaned or question

    seen = set()

    def add(rule):
        if rule["number"] not in seen:
            seen.add(rule["number"])
            blocks.append(_rule_block(rule))

    retrieved = search.search_rules(chunks, cleaned, top_k=top)
    for rule, _score in retrieved:
        add(rule)

    # Follow-up recall: a bare follow-up ("what about 2HG?", "why?")
    # retrieves poorly on its own. Also search with the session's
    # earlier questions so the topic rules resurface; the current
    # question's hits stay ranked first.
    hist_questions = [m["content"] for m in (history or [])
                      if m.get("role") == "user"]
    for rule, _score in search.search_rules(
            chunks, " ".join(hist_questions[-3:]),
            top_k=max(2, top // 2)):
        add(rule)

    by_num = {c["number"]: c for c in chunks}
    # Format anchors: preload CR rules for two-headed giant, commander,
    # etc. Matched on the original question (card mention detection may
    # have consumed those words out of the BM25 query, e.g. the card
    # 'Two-Headed Giant' vs. the format).
    anchor_numbers = [n for n in search.question_anchor_numbers(question)
                      if n in by_num]
    for number in (*GOLDEN_ANCHORS, *anchor_numbers):
        add(by_num[number])

    # Hybrid expansion, one hop: rules the retrieved rules cite (direct
    # number lookups, local and instant). This patches BM25's misses
    # (e.g. the legend rule) so the model rarely needs a search_rules
    # round-trip — the biggest latency multiplier.
    hops = 0
    for rule, _score in retrieved:
        for ref in RULE_REF_RE.findall(rule["text"]):
            if ref in seen or ref not in by_num:
                continue
            if hops >= MAX_HOP_RULES:
                break
            add(by_num[ref])
            hops += 1

    # Session context: carry earlier turns' blocks forward verbatim
    # (they were retrieved for a related question and are themselves
    # grounded). New retrieval stays first so the current question's
    # rules dominate; dedupe by header line, newest questions' blocks
    # preferred (REPL stores them oldest-first).
    carried = 0
    if prior_blocks:
        room = max(0, MAX_SESSION_BLOCKS - len(blocks))
        heads = {b.splitlines()[0] for b in blocks}
        for b in reversed(prior_blocks):
            head = b.splitlines()[0]
            if head in heads or carried >= room:
                continue
            heads.add(head)
            blocks.append(b)
            carried += 1

    if not blocks:
        return {
            "error": "Nothing retrieved from the rules for that question.",
            "elapsed": time.time() - start,
            "meta": meta,
        }

    spin = spinner or Spinner()
    n_rules = sum(1 for b in blocks if b.startswith("RULE"))
    n_cards = sum(1 for b in blocks if b.startswith("CARD"))
    n_rulings = sum(1 for b in blocks if b.startswith("OFFICIAL RULINGS"))
    anchor_note = (f", {len(anchor_numbers)} anchor rules"
                  if anchor_numbers else "")
    if history and carried:
        anchor_note += f", {carried} carried"
    print(f"→ {n_rules} rules ({hops} via citations{anchor_note}), "
          f"{n_cards} cards, {n_rulings} ruling sets in context",
          file=sys.stderr, flush=True)

    backend = backend or llm.resolve_backend()
    model = model or llm.default_model(backend)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(date=meta["effective_date"])
    tools = [SEARCH_RULES_TOOL]  # local: works offline
    if not offline:
        tools.append(SEARCH_CARDS_TOOL)
    card_names = re.findall(r"^CARD (.+)$",
                            "\n".join(blocks), re.MULTILINE)
    tool_runner = make_tool_runner(chunks, referenced=card_names)
    stream_hook, stream_finish, stream_mark = (
        _make_stream_printer(card_names, spin) if stream_output
        else (None, None, None))
    slow_after, fallback_model = _watchdog_settings(backend)
    stats = {}
    spin.start(f"asking {backend} ({model})")
    streamed_live = False
    try:
        backend, model, answer = llm.ask_llm(
            system_prompt, blocks, question,
            backend=backend, model=model,
            tools=tools, tool_runner=tool_runner,
            max_tool_rounds=3,
            progress=spin.note,
            stream=stream_hook, stats=stats,
            fallback_model=(fallback_model or None),
            first_answer_deadline=(slow_after if stream_output and slow_after
                                   else None),
            history=history,
        )
    finally:
        spin.stop()
        if stream_hook:
            streamed_live = stream_finish()
    total = time.time() - start
    print(f"→ done: {stats.get('rounds', 1)} model round(s), "
          f"{stats.get('model_seconds', 0):.0f}s model time, "
          f"{total:.1f}s total", file=sys.stderr, flush=True)
    return {
        "answer": answer,
        "backend": backend,
        "model": model,
        "meta": meta,
        "blocks": blocks,
        "context_ids": _context_ids(blocks),
        "elapsed": total,
        "stats": stats,
        "streamed": streamed_live,
    }


def _print_answer(res, verbose=False):
    """Styled output shared by `ask` and the interactive session. When the
    answer was streamed live, the body is already on screen; only the
    footer and context lines print."""
    card_names = re.findall(r"^CARD (.+)$",
                            "\n".join(res["blocks"]), re.MULTILINE)
    if not res.get("streamed"):
        print(ansi.render(res["answer"], card_names=card_names))
    print("\n" + ansi.styled(
        f"--- answer by {res['backend']} ({res['model']}); "
        f"CR effective {res['meta']['effective_date']} ---", ansi.GRAY))
    if verbose:
        print("\n" + ansi.styled("retrieved context", ansi.BOLD))
        for b in res["blocks"]:
            print(f"\n{ansi.render(b)}\n")
    else:
        print(ansi.render("Context used: " + ", ".join(res["context_ids"]),
                          card_names=card_names))


def cmd_ask(args):
    question = " ".join(args.question)
    res = answer_question(question, top=args.top, offline=args.offline,
                           backend=args.backend, model=args.model,
                           interactive=sys.stdin.isatty(),
                           stream_output=sys.stdout.isatty())
    if "error" in res:
        print(res["error"])
        return 1
    _print_answer(res, verbose=args.verbose)
    return 0


BENCH_HARD_RE = re.compile(r"^HARD:\s*", re.IGNORECASE)


def parse_bench(path):
    """bench.txt format: questions separated by blank lines; a 'HARD:'
    prefix marks difficulty."""
    text = Path(path).read_text(encoding="utf-8")
    items = []
    for block in re.split(r"\n\s*\n", text.strip()):
        block = " ".join(block.split())
        if not block:
            continue
        tag = None
        if BENCH_HARD_RE.match(block):
            tag = "HARD"
            block = BENCH_HARD_RE.sub("", block)
        items.append((tag, block))
    return items


def cmd_bench(args):
    items = parse_bench(args.file)
    if args.only:
        wanted = {int(x) for x in args.only.split(",")}
        items = [(t, q) for i, (t, q) in enumerate(items, 1) if i in wanted]
        base = sorted(wanted)
    else:
        base = list(range(1, len(items) + 1))
    if not items:
        print(f"No questions found in {args.file}.")
        return 1

    print(f"mtg bench: {len(items)} question(s) from {args.file}", flush=True)
    results = []
    for index, (tag, question) in zip(base, items):
        label = f"[{index}] " + ("(HARD) " if tag else "") + question
        print(f"\n{'=' * 70}\n{label}\n{'=' * 70}", flush=True)
        try:
            res = answer_question(question, top=args.top, offline=args.offline,
                                   backend=args.backend, model=args.model)
        except Exception as e:  # noqa: BLE001 - run all questions despite failures
            print(f"error: {e}", flush=True)
            res = {"error": str(e)}
        results.append({"tag": tag, "question": question, **{
            k: v for k, v in res.items() if k != "blocks"}})
        if "error" in res:
            print(f"error: {res['error']}", flush=True)
            continue
        print(res["answer"], flush=True)
        print(f"\n[context: {', '.join(res['context_ids'])}] "
              f"[took {res['elapsed']:.0f}s]", flush=True)

    report = Path(args.report)
    _write_bench_report(report, results)
    ok = [r for r in results if "answer" in r]
    total = sum(r.get("elapsed", 0) for r in results)
    print(f"\n{'=' * 70}\nDone: {len(ok)}/{len(results)} answered, "
          f"{total:.0f}s total. Report: {report}", flush=True)
    return 0


def _write_bench_report(path, results):
    answered = [r for r in results if "answer" in r]
    cr_date = answered[0]["meta"]["effective_date"] if answered else "?"
    backend = f"{answered[0]['backend']} ({answered[0]['model']})" if answered else "?"
    lines = [
        f"# mtg bench — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Backend: {backend}, CR effective {cr_date}, "
        f"{len(answered)}/{len(results)} answered.",
        "",
    ]
    for i, r in enumerate(results, 1):
        hard = " (HARD)" if r["tag"] else ""
        lines.append(f"## {i}.{hard} {r['question']}")
        if "error" in r:
            lines.append(f"\n**Error:** {r['error']}\n")
        else:
            lines.append(f"\n**Context:** {', '.join(r['context_ids'])}  ")
            stats = r.get("stats") or {}
            detail = ""
            if stats.get("rounds"):
                detail = (f", {stats['rounds']} model round(s), "
                          f"{stats.get('model_seconds', 0):.0f}s model time"
                          + f", {stats.get('tool_calls', 0)} tool calls"
                          if stats.get("rounds") else "")
            lines.append(f"**Time:** {r['elapsed']:.0f}s{detail}\n")
            lines.append(r["answer"])
        lines.append("\n---\n")
    path.write_text("\n".join(lines), encoding="utf-8")


REPL_COMMANDS = (
    "help", "quit", "new",
    "rule", "card", "search", "stats", "config",
    "backend", "model", "offline", "verbose",
)

REPL_HELP = """\
Lines starting with ':' are session commands (Tab completes them):
  :help                 this list
  :quit, :exit          leave (Ctrl-D or Ctrl-C at the prompt also work)
  :new                  reset the conversation: forget earlier turns
  :rule <number>        print a rule plus its subrules, e.g. :rule 704.5j
  :card <name>          oracle text + rulings, e.g. :card Lightning Helix
  :search <query>       BM25 rules search, no LLM: :search deathtouch trample
  :stats                index statistics
  :config               effective settings and where they come from
  :backend [name]       show or switch backend (mistral | lmstudio | ollama)
  :model [name]         show or set a model override
  :offline              toggle: local card DB only, no live Scryfall calls
  :verbose              toggle: print retrieved context with each answer
Anything else is a rules question, answered with citations
(same pipeline as `oragle ask`). Wrap card names in [[Name]] to
reference them explicitly — plain capitalized words are never treated
as cards; unbracketed card names are resolved by the model via card
search. Ambiguous names ([[Selvala]]) prompt for a choice. Questions
share context: follow-ups like "what about 2HG?" or "and the second
one?" keep earlier turns in scope; :new starts a clean conversation.

To leave, type :quit (or press Ctrl-D). Bare 'quit'/'exit' print a
reminder, and unknown :commands print this help."""


def _repl_help_text():
    lines = []
    for line in REPL_HELP.split("\n"):
        lines.append(re.sub(
            r"^(\s*)(:[a-z]+)",
            lambda m: m.group(1) + ansi.styled(m.group(2), ansi.BOLD, ansi.CYAN),
            line))
    return "\n".join(lines)


def _print_repl_help():
    print(_repl_help_text())


def _repl_dispatch(line, state):
    """Handle one ':' session command. Unknown commands print the help,
    so a ':' prefix never sends anything to the model. Returns 'quit' or None."""
    cmd, _, rest = line[1:].partition(" ")
    cmd, rest = cmd.lower(), rest.strip()
    if cmd in ("h", "help"):
        _print_repl_help()
    elif cmd in ("q", "quit", "exit"):
        return "quit"
    elif cmd == "new":
        state["history"] = []
        state["prior_blocks"] = []
        print("conversation reset")
    elif cmd == "rule":
        if not rest:
            print("usage: :rule 704.5j")
        else:
            cmd_rule(argparse.Namespace(number=rest))
    elif cmd == "card":
        if not rest:
            print("usage: :card Lightning Helix")
        else:
            cmd_card(argparse.Namespace(name=rest.split(), offline=state["offline"]))
    elif cmd == "search":
        if not rest:
            print("usage: :search deathtouch trample")
        else:
            cmd_search(argparse.Namespace(query=rest.split(), top=5))
    elif cmd == "stats":
        cmd_stats(argparse.Namespace())
    elif cmd == "config":
        try:
            rows = settings_report(state["backend"]
                                    or llm.resolve_backend(),
                                    state["model"])
        except llm.LLMError as e:
            print(f"error: {e}")
            return None
        for name, value, source in rows:
            line = f"{ansi.styled(name, ansi.BOLD)}: {value}"
            if source:
                line += ansi.styled(f"  [{source}]", ansi.GRAY)
            print(line)
    elif cmd == "backend":
        if rest:
            if rest not in ("mistral", "lmstudio", "ollama"):
                print(f"unknown backend '{rest}' (mistral | lmstudio | ollama)")
            else:
                state["backend"] = rest
                state["model"] = None
                print(f"backend: {rest}")
        else:
            print(f"backend: {state['backend'] or llm.resolve_backend()}")
    elif cmd == "model":
        if rest:
            state["model"] = rest
            print(f"model override: {rest}")
        else:
            print(f"model override: {state['model'] or '(backend default)'}")
    elif cmd == "offline":
        state["offline"] = not state["offline"]
        print(f"offline: {'on' if state['offline'] else 'off'}")
    elif cmd == "verbose":
        state["verbose"] = not state["verbose"]
        print(f"verbose context: {'on' if state['verbose'] else 'off'}")
    elif not cmd:
        _print_repl_help()
    else:
        print(ansi.styled(f"unknown command ':{cmd}'", ansi.YELLOW))
        _print_repl_help()
    return None


BARE_EXIT_RE = re.compile(r"^(?:quit|exit)[\s?!.]*$", re.IGNORECASE)


def bare_exit(line):
    """Only the bare words 'quit'/'exit' (trailing punctuation tolerated):
    show the how-to-leave reminder instead of prompting the model."""
    return bool(BARE_EXIT_RE.match(line.strip()))


def _repl_answer(question, state, interactive):
    """Run the ask pipeline for one REPL line. On success the question,
    answer, and retrieved context carry over to later questions."""
    try:
        res = answer_question(question, offline=state["offline"],
                              backend=state["backend"], model=state["model"],
                              interactive=interactive,
                              stream_output=interactive
                              and sys.stdout.isatty(),
                              history=list(state.get("history") or []),
                              prior_blocks=list(state.get("prior_blocks")
                                                or []))
    except KeyboardInterrupt:
        print("(interrupted)")
        return
    except Exception as e:  # noqa: BLE001 - keep the session alive
        print(f"error: {e}")
        return
    if "error" in res:
        print(res["error"])
        return
    _print_answer(res, verbose=state["verbose"])
    # Conversation memory: replay of grounded turns + carried blocks.
    # Keep the last few messages; llm.ask_llm caps the list again.
    history = state.setdefault("history", [])
    history.append({"role": "user", "content": question})
    history.append({"role": "assistant", "content": res["answer"]})
    del history[:-12]
    merged, heads = [], set()
    for b in [*state.get("prior_blocks", []), *res.get("blocks", [])]:
        head = b.splitlines()[0]
        if head in heads:
            continue
        heads.add(head)
        merged.append(b)
    state["prior_blocks"] = merged[-MAX_SESSION_BLOCKS:]
    if interactive:
        # the default after an answer is to keep every earlier turn and
        # block in scope — useful for follow-ups, expensive and muddy
        # for a new topic. Keep that choice visible.
        print(ansi.styled(
            "(this conversation continues with shared context — :new "
            "starts a fresh thread for a new topic)", ansi.DIM))
    if res.get("stats", {}).get("fallback_used"):
        fast = res["model"]
        if state["model"] is None:
            # a model that needed rescuing once will likely be slow again:
            # stay on the fallback for the rest of the session
            state["model"] = fast
            print(ansi.styled(
                f"staying on {fast} for this session "
                "(':model <name>' to change)", ansi.YELLOW))
        else:
            print(ansi.styled(
                f"note: answered by fallback {fast}", ansi.YELLOW))


def complete_repl_line(line, text):
    """Tab-completion candidates for the first word of a REPL line:
    ':' commands (plus bare quit helps). Only completes before the first
    space; arguments are left alone."""
    stripped = line.lstrip()
    if " " in stripped:
        return []
    cands = [":" + c for c in REPL_COMMANDS] + [":exit", ":q",
                                                "quit", "exit", "q", "help"]
    seen, ordered = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return [c for c in ordered if c.startswith(text)]


def cmd_repl(args):
    """Interactive session: bare `oragle` prompts for questions on stdin.
    Piped stdin is answered line by line without prompts."""
    interactive = sys.stdin.isatty()
    state = {"offline": False, "verbose": False, "backend": None,
             "model": None, "history": [], "prior_blocks": []}
    readline_ok = False
    try:  # input() history + line editing where available
        import readline

        def _completer(text, state_):
            matches = complete_repl_line(readline.get_line_buffer(), text)
            return matches[state_] if state_ < len(matches) else None

        readline.set_completer(_completer)
        readline.set_completer_delims(" \t")
        if "libedit" in (readline.__doc__ or ""):
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")
        readline_ok = True
    except ImportError:
        pass
    if interactive:
        try:
            meta, _chunks = store.load_rules()
            print()
            print(ansi.styled("oragle", ansi.BOLD, ansi.CYAN)
                  + ansi.styled(f" — MTG rules judge ", ansi.DIM, stream=sys.stdout)
                  + ansi.styled(f"CR effective {meta['effective_date']}",
                                ansi.DIM))
            print(ansi.styled("Type a rules question; enter a :command "
                             "(Tab completes), :quit to exit.", ansi.DIM))
        except store.NoRulesError as e:
            print(str(e), file=sys.stderr)
            return 2
    if not interactive:
        prompt = ""
    elif readline_ok and ansi.enabled():
        prompt = ansi.readline_safe(
            ansi.styled("oragle> ", ansi.BOLD, ansi.CYAN))
    else:
        prompt = ansi.styled("oragle> ", ansi.BOLD, ansi.CYAN)
    while True:
        try:
            line = input(prompt).strip()
        except EOFError:
            return 0
        except KeyboardInterrupt:
            return 0
        if not line:
            continue
        try:
            if line.startswith(":"):
                if _repl_dispatch(line, state) == "quit":
                    return 0
                continue
            if bare_exit(line):
                print(ansi.styled(
                    "To quit the session, type :quit (or press Ctrl-D). "
                    ":help lists all commands.", ansi.YELLOW))
                continue
            low = line.lower()
            if low in ("help", "?"):
                _print_repl_help()
                continue
            _repl_answer(line, state, interactive)
        except KeyboardInterrupt:
            print("(interrupted)")


def main(argv=None):
    envfile.load_env()  # .env files: real environment always wins
    parser = argparse.ArgumentParser(
        prog=None,  # follow argv[0]: oragle / mtg
        description="MTG rules assistant with citations")
    sub = parser.add_subparsers(dest="command", required=False)

    p = sub.add_parser("refresh", help="download, parse, and persist data")
    p.add_argument("target", nargs="?", choices=["rules", "cards"], default=None,
                   help="'rules' (CR text, fast, the default) or 'cards' (Scryfall bulk, ~25 MB)")
    p.add_argument("--url", help="optional CR .txt URL override (rules target)")
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("stats", help="show index stats")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("config", help="show effective settings and sources")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("search", help="keyword search rules")
    p.add_argument("query", nargs="+")
    p.add_argument("--top", type=int, default=5)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("rule", help="print one rule plus subrules")
    p.add_argument("number")
    p.set_defaults(func=cmd_rule)

    p = sub.add_parser("card", help="oracle text + rulings for a card")
    p.add_argument("name", nargs="+")
    p.add_argument("--offline", action="store_true",
                   help="use only the local card database (no network)")
    p.set_defaults(func=cmd_card)

    p = sub.add_parser("cards", help="search cards with Scryfall syntax (c<=2 colors:blue o:exile)")
    p.add_argument("query", nargs="+")
    p.add_argument("--top", type=int, default=10)
    p.set_defaults(func=cmd_cards)

    p = sub.add_parser("ask", help="answer a rules question with citations")
    p.add_argument("question", nargs="+")
    p.add_argument("--top", type=int, default=8, help="number of rules to retrieve")
    p.add_argument("--backend", choices=["mistral", "lmstudio", "ollama"])
    p.add_argument("--model", help="model override (or MTG_MODEL env)")
    p.add_argument("--offline", action="store_true",
                   help="cards from local DB only, skip freshness check")
    p.add_argument("--verbose", "-v", action="store_true", help="print full retrieved context")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("bench", help="run benchmark questions from a file through the ask pipeline")
    p.add_argument("file", nargs="?", default="bench.txt")
    p.add_argument("--only", help="comma-separated question numbers (1-based), e.g. '1,4'")
    p.add_argument("--top", type=int, default=8)
    p.add_argument("--report", default="bench-last.md")
    p.add_argument("--backend", choices=["mistral", "lmstudio", "ollama"])
    p.add_argument("--model", help="model override (or MTG_MODEL env)")
    p.add_argument("--offline", action="store_true")
    p.set_defaults(func=cmd_bench)

    args = parser.parse_args(argv)
    if args.command is None:  # bare `oragle` -> interactive session
        args.func = cmd_repl
    try:
        return args.func(args)
    except store.NoRulesError as e:
        print(str(e), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001 - CLI boundary
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
