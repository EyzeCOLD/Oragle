"""Comprehensive Rules: download and parse into rule-number chunks.

Chunking decision: each rule number (100.1, 100.1a, 702.2c) is its own chunk.
A parent rule's chunk contains ONLY its own text, never its subrules.
Example paragraphs attach to the rule they follow. See PLAN.md.
"""

import re

from . import net

RULES_PAGE = "https://magic.wizards.com/en/rules"
FALLBACK_URLS = [
    "https://media.wizards.com/2026/downloads/MagicCompRules%2020261106.txt",
]

RULE_RE = re.compile(
    r"^(?P<num>\d+\.\d+(?:\.\d+)*)(?P<suffix>[a-z]*)\.?[ \t]+(?P<text>\S.*)$"
)
HEADING_RE = re.compile(r"^(?P<num>\d+)\.\s+(?P<title>[A-Z0-9].*[^.])$")
EFFECTIVE_RE = re.compile(r"effective as of (.+?)\.", re.IGNORECASE)


class Rule(dict):
    """{number, text, chapter, section}"""

    @property
    def number(self):
        return self["number"]


def discover_cr_url():
    try:
        page = net.get_text(RULES_PAGE, timeout=30)
    except net.NetError:
        return None
    m = re.search(r'href="(https?://[^"]+?\.txt)"', page)
    if m:
        return m.group(1).replace(" ", "%20")
    return None


def download_cr(url=None):
    candidates = ([url] if url else []) + ([discover_cr_url()] if not url else []) + FALLBACK_URLS
    last_error = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return candidate, net.get_text(candidate, timeout=120)
        except net.NetError as e:
            last_error = e
    raise RuntimeError(f"Could not download the Comprehensive Rules: {last_error}")


def parse_cr(text):
    """Return (metadata, chunks). chunks is a list of Rule dicts."""
    date_match = EFFECTIVE_RE.search(text[:3000])
    meta = {"effective_date": date_match.group(1).strip() if date_match else None}
    chunks = []
    chapter = section = None
    for line in text.splitlines():
        line = line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if chunks and stripped == "Glossary":
            break
        rule_match = RULE_RE.match(stripped)
        if rule_match:
            chunks.append(
                Rule(
                    number=rule_match["num"] + rule_match["suffix"],
                    text=rule_match["text"].strip(),
                    chapter=chapter or "",
                    section=section or "",
                )
            )
            continue
        heading_match = HEADING_RE.match(stripped)
        if heading_match:
            if int(heading_match["num"]) < 100:
                chapter = heading_match["title"].strip()
            else:
                section = heading_match["title"].strip()
            continue
        if chunks and stripped.startswith("Example:"):
            chunks[-1]["text"] += "\n" + stripped
    return meta, chunks


NUM_KEY_RE = re.compile(r"^([\d.]+)([a-z]*)$")


def rule_sort_key(number):
    base, letter = NUM_KEY_RE.match(number).groups()
    return tuple(int(p) for p in base.split(".")) + (letter,)


def _is_child(number, parent):
    if not number.startswith(parent) or len(number) <= len(parent):
        return False
    tail = number[len(parent):]
    # 702.2a and 702.12.1 are children of 702.2; 702.20 is not.
    return tail[0].isalpha() or tail[0] == "."


def match_rule(chunks, number):
    """Exact rule plus all of its subrules, in rule order."""
    number = number.strip().rstrip(".")
    direct = [c for c in chunks if c["number"] == number]
    if not direct:
        raise KeyError(number)
    sub = [c for c in chunks if _is_child(c["number"], number)]
    return sorted(direct + sub, key=lambda c: rule_sort_key(c["number"]))


def keyword_abilities(chunks):
    """Map keyword ability name -> rule number, from section 702 headers.

    In section 702 each rule 702.N's text is exactly the keyword's name
    (e.g. '702.2. Deathtouch'); its definition lives in the subrules.
    """
    keywords = {}
    for c in chunks:
        if c["section"] != "Keyword Abilities":
            continue
        parts = c["number"].split(".")
        if len(parts) == 2 and parts[1].isdigit() and int(parts[1]) >= 2:
            name = c["text"].strip()
            if name and "\n" not in name and len(name.split()) <= 4:
                keywords.setdefault(name.lower(), c["number"])
    return keywords
