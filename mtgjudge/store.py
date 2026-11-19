"""On-disk persistence for parsed rules. Data directory is fixed:
$MTG_DATA_DIR if set, else ~/.local/share/mtg-rules-judge.
"""

import json
import os
from pathlib import Path

from .cr import Rule

DATA_DIR = Path(os.environ.get("MTG_DATA_DIR") or Path.home() / ".local/share/mtg-rules-judge")
RULES_FILE = DATA_DIR / "rules.json"


class NoRulesError(RuntimeError):
    pass


def save_rules(meta, chunks):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": meta,
        "chunks": [dict(c) for c in chunks],
    }
    tmp = RULES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(RULES_FILE)
    return RULES_FILE


def load_rules():
    if not RULES_FILE.exists():
        raise NoRulesError(
            f"No rules index at {RULES_FILE}. Run `mtg refresh` (or `uv run mtg refresh`) first."
        )
    payload = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    chunks = [Rule(c) for c in payload["chunks"]]
    return payload["meta"], chunks


def stats(chunks):
    by_chapter = {}
    for c in chunks:
        by_chapter[c["chapter"]] = by_chapter.get(c["chapter"], 0) + 1
    biggest = max(by_chapter.items(), key=lambda kv: kv[1]) if by_chapter else ("?", 0)
    return {
        "rules": len(chunks),
        "chapters": len(by_chapter),
        "biggest_chapter": biggest[0],
        "biggest_chapter_rules": biggest[1],
    }
