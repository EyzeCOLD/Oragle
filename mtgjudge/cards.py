"""Scryfall card and rulings lookup. No API key; be polite (small delay)."""

import re
import time

from . import net

SCRYFALL_BASE = "https://api.scryfall.com"


class CardNotFound(RuntimeError):
    pass


def fetch_card(name, exact=False):
    try:
        return net.get_json(
            f"{SCRYFALL_BASE}/cards/named?{'exact' if exact else 'fuzzy'}={net.quote(name)}",
            timeout=30,
        )
    except net.NetError as e:
        if e.status in (404, 400):
            raise CardNotFound(f"No card found for '{name}'.") from e
        raise


def search_cards(query, limit=10):
    """Scryfall-syntax card search (c<=2 ci:blue o:exile legal:pauper fo:...).
    Returns (cards, total_matches)."""
    try:
        data = net.get_json(
            f"{SCRYFALL_BASE}/cards/search?q={net.quote(query)}", timeout=30
        )
    except net.NetError as e:
        if e.status == 404:
            return [], 0
        raise
    return data.get("data", [])[:limit], data.get("total_cards", 0)


def format_search_brief(found, total):
    """Compact listing used as tool results for the model: name, cost, type,
    one-line oracle text. Few results (a card-resolution lookup) get a
    bigger text budget than broad searches."""
    limit = 600 if len(found) <= 3 else 220
    lines = [f"{total} card(s) match. Showing {len(found)}:"]
    for card in found:
        card = _merged_fields(card)
        text = " ".join((card.get("oracle_text") or "").split())
        if len(text) > limit:
            text = text[:limit - 3] + "..."
        lines.append(
            f"- {card['name']} {card.get('mana_cost', '')} | "
            f"{card.get('type_line', '')} | {text or '(no rules text)'}"
        )
    return "\n".join(lines)


def fetch_rulings(card):
    time.sleep(0.15)
    data = net.get_json(card["rulings_uri"], timeout=30)
    return data.get("data", [])


def _merged_fields(card):
    """Multi-face cards ( Adventures, MDFCs) carry text on card_faces."""
    faces = card.get("card_faces") or []
    if not faces:
        return card
    merged = dict(card)
    merged["mana_cost"] = " // ".join(p for p in [card.get("mana_cost")] +
                                      [f.get("mana_cost") for f in faces] if p) or card.get("mana_cost", "")
    merged["type_line"] = (card.get("type_line")
                           or " // ".join(p for p in [f.get("type_line") for f in faces] if p) or "")
    merged["oracle_text"] = (card.get("oracle_text")
                             or "\n//\n".join(p for p in [f.get("oracle_text") for f in faces] if p) or "")
    return merged


def format_card(card):
    card = _merged_fields(card)
    lines = [f"{card['name']} {card.get('mana_cost', '')}".strip()]
    lines.append(card.get("type_line", ""))
    if card.get("power"):
        lines.append(f"P/T: {card['power']}/{card['toughness']}")
    lines.append(card.get("oracle_text", "") or "(no rules text)")
    return "\n".join(lines)


def format_rulings(rulings):
    if not rulings:
        return ""
    lines = []
    for r in rulings:
        lines.append(f"- ({r['published_at']}) {r['comment']}")
    return "\n".join(lines)


CARD_REF_RE = re.compile(r"\[\[\s*([^\]]+?)\s*\]\]")


def extract_card_refs(question):
    """Split question into (card names, question without card refs)."""
    names = [m.strip() for m in CARD_REF_RE.findall(question)]
    cleaned = CARD_REF_RE.sub(lambda m: m.group(1).strip(), question)
    return names, cleaned
