"""Decklist parsing: paste a deck in any of the usual plain-text
formats and every card is resolved against the local DB in one pass —
no model tool rounds fetching cards one by one.

Recognized (deterministic, zero-token — the philosophy of this repo):

- counts: "4 Lightning Bolt", "4x Bolt", "Lightning Bolt x4"
- MTGO / Scryfall export: numbered lines, optional " (SET) 123" or
  " [SET] #123" collectors'/set suffixes, "//" or "#" comment lines
- MTG Arena export: "Deck" / "Commander" / "Sideboard" / "Companion"
  section headers, blank lines between sections
- explicit <deck> ... </deck> markers (what the web frontend emits)

Detection outside markers is conservative: a run of >= 3 counted card
lines (a header or blank line may appear inside the run) so an ordinary
multi-line question never trips it.
"""

import re

# A single decklist line, in either count order. Captures are cleaned up
# afterwards; the trailing set/collector noise is stripped from the name.
COUNT_PREFIX_RE = re.compile(r"^\s*(\d{1,3})\s*[xX]?\s+(.+?)\s*$")
COUNT_SUFFIX_RE = re.compile(r"^\s*(.+?)\s*[xX]\s*(\d{1,3})\s*$")
# "Lightning Bolt (M11) 123", "Fire // Ice [ARB] #45" and friends
SET_SUFFIX_RE = re.compile(
    r"\s+(?:\[[^\]]*\]|\([A-Za-z0-9]{2,5}\))\s*(?:[#\s]?\d{1,3})?\s*$")

SECTION_HEADERS = {
    "deck", "decklist", "main", "mainboard", "main deck", "commander",
    "sideboard", "maybeboard", "companion", "board",
}
COMMENT_RE = re.compile(r"^\s*(?://|#)")
DECK_MARKER_RE = re.compile(r"<deck>(.*?)</deck>", re.DOTALL)

MIN_DECK_LINES = 3


def _clean_name(raw):
    name = SET_SUFFIX_RE.sub("", raw.strip())
    return name.strip().strip("\"\u201c\u201d")


def _classify(line):
    """One line of pasted text -> one of:
    ('header', section), ('comment', None), ('blank', None),
    ('card', (count, name)), or None when it is not decklist material."""
    stripped = line.strip()
    if not stripped:
        return ("blank", None)
    if COMMENT_RE.match(stripped):
        return ("comment", None)
    head = stripped.rstrip(":").strip().lower()
    if head in SECTION_HEADERS:
        return ("header", head.rstrip(":"))
    m = COUNT_PREFIX_RE.match(stripped)
    if m:
        return ("card", (int(m.group(1)), _clean_name(m.group(2))))
    m = COUNT_SUFFIX_RE.match(stripped)
    if m and _clean_name(m.group(1)):
        return ("card", (int(m.group(2)), _clean_name(m.group(1))))
    return None


def _lines_to_entries(lines):
    """Parse within a confirmed deck region: card lines become entries,
    everything else is separator noise. Returns [((count, name))...]."""
    entries = []
    for line in lines:
        kind = _classify(line)
        if kind and kind[0] == "card" and kind[1][1]:
            entries.append(kind[1])
    return entries


def extract_deck(question):
    """Pull a decklist out of a question.

    Returns (entries, remaining_question): entries is a list of
    (count, card_name) preserving order (duplicates kept — callers
    merge), and remaining_question has the decklist replaced by a
    short "(decklist: N cards)" note so rules search focuses on the
    actual question. When no decklist is found the entries list is
    empty and the question comes back unchanged.
    """
    # explicit markers first: the web frontend and power users
    m = DECK_MARKER_RE.search(question)
    if m:
        entries = _lines_to_entries(m.group(1).splitlines())
        if entries:
            merged = _merge(entries)
            question = (question[:m.start()].rstrip()
                        + "\n" + _summary(merged)
                        + question[m.end():])
            return merged, question.strip()
        return [], question

    # heuristic: the longest run of >= 3 deck-material lines
    lines = question.splitlines()
    best = None  # (start, end, entries)
    i = 0
    n = len(lines)
    while i < n:
        if _classify(lines[i]) is None:
            i += 1
            continue
        j, cards_seen = i, 0
        while j < n:
            kind = _classify(lines[j])
            if kind is None:
                break
            if kind[0] == "card":
                cards_seen += 1
            j += 1
        if cards_seen >= MIN_DECK_LINES:
            entries = _lines_to_entries(lines[i:j])
            if best is None or len(entries) > len(best[2]):
                best = (i, j, entries)
        i = max(j, i + 1)
    if best is None:
        return [], question
    start, end, entries = best
    merged = _merge(entries)
    new_lines = (lines[:start]
                 + [_summary(merged)]
                 + lines[end:])
    return merged, "\n".join(new_lines)


def _merge(entries):
    merged, order = {}, []
    for count, name in entries:
        key = name.casefold()
        if key not in merged:
            merged[key] = {"count": 0, "name": name}
            order.append(key)
        merged[key]["count"] += count
    return [(merged[k]["count"], merged[k]["name"]) for k in order]


def _summary(entries):
    total = sum(c for c, _n in entries)
    return (f"(decklist: {len(entries)} unique cards, "
            f"{total} cards total)")
