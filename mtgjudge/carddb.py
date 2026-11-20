"""Local card database (SQLite), built from Scryfall's daily "Oracle Cards"
bulk dump (jsonl.gz, ~25 MB download, one row per Oracle id).

- Card lookups run against the local DB; live Scryfall is only used for
  cards missing locally (e.g. printed after the last refresh).
- Rulings are fetched per card on demand and cached in the DB.
- Freshness: at most one /bulk-data request per CHECK_INTERVAL_HOURS;
  if the upstream bulk dump is newer than ours, lookups print a notice
  suggesting `mtg refresh cards`. Network errors during the check are
  ignored (offline use is the point of the DB).
"""

import difflib
import gzip
import io
import json
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone

from . import cards, net
from .store import DATA_DIR

DB_FILE = DATA_DIR / "cards.db"
BULK_TYPE = "oracle_cards"
CHECK_INTERVAL_HOURS = 24
STALE_AFTER_DAYS = 14

# Legality columns kept per card (Scryfall's legalities keys, pruned to
# the formats worth a model's query). "not_legal" is the stored default.
LEGALITY_FORMATS = ("standard", "pioneer", "modern", "legacy", "vintage",
                    "pauper", "commander", "brawl", "historic", "alchemy",
                    "oathbreaker", "duel")
SCHEMA_VERSION = "2"

# Layouts deprioritized when multiple names tie in fuzzy matches.
NON_PAPER_LAYOUTS = ("token", "art_series", "emblem", "vanguard", "plane", "scheme", "double_faced_token")

_LEGALITY_COLS = ",\n".join(f"    legality_{f} TEXT" for f in LEGALITY_FORMATS)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS cards (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    lookup_all  TEXT NOT NULL,   -- normalized, all face names, space padded
    nospace     TEXT NOT NULL,   -- lookup_all with spaces removed
    mana_cost   TEXT,
    type_line   TEXT,
    oracle_text TEXT,
    power       TEXT,
    toughness   TEXT,
    loyalty     TEXT,
    layout      TEXT,
    released_at TEXT,
    edhrec_rank INTEGER,
    cmc          REAL,
    colors       TEXT,
    color_identity TEXT,
    keywords     TEXT,
    oracle_all   TEXT,
{_LEGALITY_COLS}
);
CREATE INDEX IF NOT EXISTS idx_cards_lookup_all ON cards(lookup_all);
CREATE TABLE IF NOT EXISTS rulings (
    card_id TEXT NOT NULL,
    published_at TEXT NOT NULL,
    source TEXT NOT NULL,
    comment TEXT NOT NULL,
    PRIMARY KEY (card_id, published_at, source)
);
"""


def normalize_name(s):
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    # Apostrophes are deleted (their halves join): "Jeska's Will" and
    # "Jeskas Will" normalize identically. The other separators below
    # become spaces instead, and that breaks the misspelled-apostrophe
    # spelling, so they get special treatment first.
    s = s.replace("'", "").replace("\u2019", "")
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return s.strip()


def connect(db_file=None):
    db_file = db_file or DB_FILE
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Migration for DBs built before edhrec_rank existed (keeps rulings cache).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cards)")}
    if "edhrec_rank" not in cols:
        conn.execute("ALTER TABLE cards ADD COLUMN edhrec_rank INTEGER")
    return conn


def _row_to_card(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "mana_cost": row["mana_cost"] or "",
        "type_line": row["type_line"] or "",
        "oracle_text": row["oracle_text"] or "",
        "power": row["power"],
        "toughness": row["toughness"],
        "loyalty": row["loyalty"],
        "layout": row["layout"],
        "released_at": row["released_at"],
        "edhrec_rank": row["edhrec_rank"],
        "rulings_uri": f"https://api.scryfall.com/cards/{row['id']}/rulings",
        "from_local_db": True,
    }


def _canon_colors(card):
    """Colors of all faces, canonical WUBRG order, e.g. 'WU' ('' = none)."""
    letters = set(card.get("colors") or [])
    for face in card.get("card_faces") or []:
        letters.update(face.get("colors") or [])
    return "".join(c for c in "WUBRG" if c in letters)


def _bulk_row_from_card(card):
    """Flatten a bulk JSONL card object to DB row shape (multi-face merged)."""
    faces = card.get("card_faces") or []
    names = [card.get("name")] + [f.get("name") for f in faces]
    mana_parts = [card.get("mana_cost")] if card.get("mana_cost") else []
    mana_parts += [f.get("mana_cost") for f in faces]
    type_parts = [f.get("type_line") for f in faces]
    text_parts = [f.get("oracle_text") for f in faces]
    oracle_text = (card.get("oracle_text")
                   or " // ".join(p for p in text_parts if p) or "")
    legal = card.get("legalities") or {}
    keywords = card.get("keywords") or []
    row = {
        "id": card["id"],
        "name": card.get("name"),
        "lookup_all": " " + "  ".join(normalize_name(n) for n in names if n) + " ",
        "nospace": "",
        "mana_cost": " // ".join(p for p in mana_parts if p) or "",
        "type_line": card.get("type_line") or " // ".join(p for p in type_parts if p) or "",
        "oracle_text": oracle_text,
        "power": card.get("power"),
        "toughness": card.get("toughness"),
        "loyalty": card.get("loyalty") or next((f.get("loyalty") for f in faces if f.get("loyalty")), None),
        "layout": card.get("layout"),
        "released_at": card.get("released_at"),
        "edhrec_rank": card.get("edhrec_rank"),
        "cmc": card.get("cmc"),
        "colors": _canon_colors(card),
        "color_identity": "".join(c for c in "WUBRG"
                                  if c in (card.get("color_identity") or [])),
        "keywords": " " + " ".join(keywords) + " " if keywords else "",
        "oracle_all": " " + " ".join(oracle_text.split()) + " " if oracle_text else "",
        **{f"legality_{f}": legal.get(f, "not_legal") for f in LEGALITY_FORMATS},
    }
    row["nospace"] = row["lookup_all"].replace(" ", "")
    return row

def bulk_entry():
    """Single API call: current info about the Oracle Cards bulk dump."""
    data = net.get_json("https://api.scryfall.com/bulk-data", timeout=30)
    for entry in data.get("data", []):
        if entry.get("type") == BULK_TYPE:
            return entry
    raise RuntimeError("Oracle Cards bulk entry not found on Scryfall.")


def build_from_bulk(entry, db_file=None, progress=None):
    """Stream the bulk jsonl.gz into SQLite (replaces any previous import).
    Returns (card_count)."""
    compressed = net._open(entry["jsonl_download_uri"], timeout=600, accept=None)
    conn = connect(db_file)
    try:
        if not has_query_schema(conn):
            # Pre-v2 table shape (no queryable columns): the import is a
            # full replace anyway, so drop and recreate the cards table;
            # rulings (separate table, stable Scryfall ids) survive.
            conn.execute("DROP TABLE IF EXISTS cards")
            conn.executescript(SCHEMA)
        # Full replace: the dump is the source of truth for card content.
        # Rulings (keyed by stable Scryfall ids) survive the re-import.
        conn.execute("DELETE FROM cards")
        raw = gzip.GzipFile(fileobj=io.BytesIO(compressed))
        buf = []
        count = 0
        for line in io.TextIOWrapper(raw, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            buf.append(_bulk_row_from_card(json.loads(line)))
            if len(buf) >= 2000:
                _insert_cards(conn, buf)
                count += len(buf)
                buf = []
                if progress:
                    progress(count)
        if buf:
            _insert_cards(conn, buf)
            count += len(buf)
        _set_meta(conn, "bulk_updated_at", entry["updated_at"])
        _set_meta(conn, "bulk_downloaded_at", datetime.now(timezone.utc).isoformat())
        _set_meta(conn, "card_count", str(count))
        _set_meta(conn, "bulk_check_at", datetime.now(timezone.utc).isoformat())
        _set_meta(conn, "schema_version", SCHEMA_VERSION)
    finally:
        conn.close()
    return count


def _insert_cards(conn, rows):
    cols = ("id, name, lookup_all, nospace, mana_cost, type_line, "
            "oracle_text, power, toughness, loyalty, layout, released_at, "
            "edhrec_rank, cmc, colors, color_identity, keywords, oracle_all, "
            + ", ".join(f"legality_{f}" for f in LEGALITY_FORMATS))
    bindings = ", ".join(f":{c}" for c in (
        "id", "name", "lookup_all", "nospace", "mana_cost", "type_line",
        "oracle_text", "power", "toughness", "loyalty", "layout",
        "released_at", "edhrec_rank", "cmc", "colors", "color_identity",
        "keywords", "oracle_all",
        *(f"legality_{f}" for f in LEGALITY_FORMATS)))
    conn.executemany(
        f"INSERT OR REPLACE INTO cards ({cols}) VALUES ({bindings})",
        rows,
    )
    conn.commit()


def _get_meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()


_NON_PAPER_SQL = "(" + ", ".join("?" * len(NON_PAPER_LAYOUTS)) + ")"
_LOOKUP_ORDER = "(edhrec_rank IS NULL), edhrec_rank, length(lookup_all)"


def _exact_face_rows(conn, name, limit=1):
    """Rows whose face name is exactly the (normalized) query. Faces in
    lookup_all are space-padded and double-space separated, so a face
    match is: the whole string, or delimited by double spaces."""
    q = normalize_name(name)
    return conn.execute(
        f"SELECT * FROM cards WHERE (lookup_all = ' ' || ? || ' '"
        f" OR lookup_all LIKE (' ' || ? || '  %')"
        f" OR lookup_all LIKE ('%  ' || ? || '  %')"
        f" OR lookup_all LIKE ('%  ' || ? || ' '))"
        f" AND layout NOT IN {_NON_PAPER_SQL}"
        f" ORDER BY {_LOOKUP_ORDER} LIMIT ?",
        (q, q, q, q, *NON_PAPER_LAYOUTS, limit),
    ).fetchall()


def lookup_card(name, offline=False, db_file=None):
    """Local DB first (fast, offline-safe); Scryfall live as fallback for
    cards newer than the last bulk import. Strict by design: only an
    exact face name matches (normalize_name handles case, whitespace,
    accents and punctuation) — anything else is reported not found
    instead of silently resolved to a different card."""
    if (db_file or DB_FILE).exists():
        conn = connect(db_file)
        try:
            row = _lookup_local(conn, name)
        finally:
            conn.close()
        if row:
            return _row_to_card(row)
    if offline:
        raise cards.CardNotFound(
            f"'{name}' not found in the local card database (run without "
            "--offline to fetch from Scryfall, or `mtg refresh cards`)."
        )
    return cards.fetch_card(name, exact=True)


def _lookup_local(conn, name):
    # Strict: only a face named exactly this (after normalize_name leeway:
    # case, surrounding whitespace, accents, dashes, commas, apostrophes —
    # "Troll of Khazad Dum" finds "Troll of Khazad-dûm"). Prefix and word
    # matches silently "fixed" names before ("Mountain" -> "Mountain
    # Valley", "avuncular gnome" -> "Trench Gnome"); user decision
    # 2026-11-19: never guess — an unknown name is reported as not found.
    rows = _exact_face_rows(conn, name)
    return rows[0] if rows else None


_FACE_CACHE = {}  # db key -> (normalized faces, rows) for suggestions


def _face_cache(db_file):
    """All (normalized face, row) pairs for edit-distance suggestions,
    cached on the DB file's identity: splitting ~90k rows on every miss
    would be slow, and misses cluster (typos, half-remembered names)."""
    path = db_file or DB_FILE
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _FACE_CACHE.get("key") == key:
        return _FACE_CACHE["value"]
    conn = connect(db_file)
    pairs = []
    try:
        for row in conn.execute(
                f"SELECT * FROM cards WHERE layout NOT IN {_NON_PAPER_SQL}",
                NON_PAPER_LAYOUTS):
            for token in (row["lookup_all"] or "").strip().split("  "):
                token = token.strip()
                if token:
                    pairs.append((token, row))
    finally:
        conn.close()
    _FACE_CACHE.clear()
    _FACE_CACHE["key"] = key
    _FACE_CACHE["value"] = pairs
    return pairs


def lookup_alternates(name, db_file=None, limit=5):
    """Local candidate cards for a name that lookup_card did not find —
    explicit refs like [[Selvala]] are never silently fixed to one of
    these; they are shown to the user (interactive chooser / "did you
    mean" in the NOT FOUND block) so the miss stays visible. Two passes:
    face names containing the query as words (sub-names like
    [[Selvala]]), then close spellings by edit distance (typos like
    [[Lighting Helix]]). Empty when the database is missing."""
    if not (db_file or DB_FILE).exists():
        return []
    q = normalize_name(name)
    if not q:
        return []
    conn = connect(db_file)
    try:
        exact = _exact_face_rows(conn, name, limit=limit)
        if exact:
            return [_row_to_card(r) for r in exact]
        rows = conn.execute(
            f"SELECT * FROM cards WHERE lookup_all LIKE ? "
            f"AND layout NOT IN {_NON_PAPER_SQL} "
            f"ORDER BY {_LOOKUP_ORDER} LIMIT ?",
            (f"% {q} %", *NON_PAPER_LAYOUTS, limit),
        ).fetchall()
        if rows:
            return [_row_to_card(r) for r in rows]
    finally:
        conn.close()
    # No sub-name hits: suggest close spellings ("did you mean"). The
    # cutoff keeps suggestions conservative — no wild guessing for
    # generic or nonsense words.
    pairs = _face_cache(db_file) or []
    out, seen = [], set()
    for face in difflib.get_close_matches(q, [f for f, _ in pairs],
                                          n=9, cutoff=0.85):
        for f, row in pairs:
            if f == face and row["id"] not in seen:
                seen.add(row["id"])
                out.append(_row_to_card(row))
        if len(out) >= 3:
            break
    return out


def get_rulings(card, offline=False, db_file=None):
    """Cached rulings; fetch from Scryfall once per card."""
    card_id = card["id"]
    conn = connect(db_file)
    try:
        if _has_rulings(conn, card_id):
            rows = conn.execute(
                "SELECT published_at, source, comment FROM rulings "
                "WHERE card_id = ? ORDER BY published_at",
                (card_id,),
            ).fetchall()
            return [dict(r) for r in rows]
    finally:
        conn.close()
    if offline:
        return []
    try:
        rulings = cards.fetch_rulings(card)
    except net.NetError:
        return []
    conn = connect(db_file)
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO rulings (card_id, published_at, source, comment) "
            "VALUES (?, ?, ?, ?)",
            [(card_id, r["published_at"], r["source"], r["comment"]) for r in rulings],
        )
        conn.commit()
    finally:
        conn.close()
    return rulings


def _has_rulings(conn, card_id):
    row = conn.execute(
        "SELECT 1 FROM rulings WHERE card_id = ? LIMIT 1", (card_id,)
    ).fetchone()
    return row is not None


# ---- model-facing SQL tool ---------------------------------------------

QUERY_TIME_LIMIT_S = 2.0
QUERY_BUDGET = 4000          # chars of serialized rows before truncation
CELL_TRUNC = 600              # long texts (oracle_all!) get cut per cell


def has_query_schema(conn=None):
    """True when the cards table carries the queryable columns (v2 import
    via `oragle refresh cards`); False for pre-v2 databases."""
    own = conn is None
    conn = conn or connect()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(cards)")}
        return "colors" in cols and "oracle_all" in cols
    finally:
        if own:
            conn.close()


def run_sql(sql, db_file=None, budget=QUERY_BUDGET):
    """One read-only SQL statement over the card database, for the
    query_local_cards tool. Safety is by construction, not prompt-
    pleading, because the model is an adversarial user by definition:
    the file opens mode=ro (OS-level: the handle cannot write),
    PRAGMA query_only blocks anything a ro handle could still reach,
    and an authorizer denies every action but SELECT/READ/FUNCTION/
    RECURSIVE — so no writes, no ATTACH of other databases, no exotic
    statements. A progress-handler deadline interrupts runaway scans.
    Output is budgeted, not row-capped: aggregates return entirely;
    wide results truncate with a note that teaches narrower SELECTs.
    Returns a string (JSON row per line), never raises.
    """
    db_file = db_file or DB_FILE
    if not db_file.exists():
        return ("error: no local card database yet. Build it with "
                "`oragle refresh cards` (or `oragle setup`).")
    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    except sqlite3.Error as e:  # pragma: no cover
        return f"error: cannot open card database: {e}"
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        if not has_query_schema(conn):
            return ("error: local card database predates the queryable "
                    "schema. Rebuild it: `oragle refresh cards`.")
        sqlite3.enable_callback_tracebacks(False)

        def _auth(action, *_args):
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                          sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        conn.set_authorizer(_auth)

        deadline = time.monotonic() + QUERY_TIME_LIMIT_S

        def _tick():
            return 1 if time.monotonic() > deadline else 0

        conn.set_progress_handler(_tick, 5000)
        try:
            cur = conn.execute(sql)
        except sqlite3.Error as e:
            return f"error: {e}"
        if cur.description is None:
            return "ok (no result rows)"
        cols = [d[0] for d in cur.description]
        lines, size, truncated = [], 0, False
        while not truncated:
            rows = cur.fetchmany(64)
            if not rows:
                break
            for row in rows:
                obj = {}
                for col in cols:
                    v = row[col]
                    if isinstance(v, str) and len(v) > CELL_TRUNC:
                        v = v[:CELL_TRUNC - 3] + "..."
                    obj[col] = v
                line = json.dumps(obj)
                lines.append(line)
                size += len(line)
                if size > budget:
                    truncated = True
                    break
        if truncated:
            lines.append(f"result truncated at {len(lines)} rows (~{size} "
                         "chars): add a LIMIT or project fewer columns")
        return "\n".join(lines) or "0 rows"
    finally:
        conn.close()


def freshness_notice(force_check=False, offline=False, db_file=None):
    """Return a warning string if the local bulk data lags Scryfall.
    Performs at most one network check per CHECK_INTERVAL_HOURS."""
    db_file = db_file or DB_FILE
    if offline or not db_file.exists():
        return None
    conn = connect(db_file)
    try:
        check_at = _get_meta(conn, "bulk_check_at")
        bulk_updated = _get_meta(conn, "bulk_updated_at")
        due = True
        if check_at:
            checked = datetime.fromisoformat(check_at)
            due = (datetime.now(timezone.utc) - checked).total_seconds() > CHECK_INTERVAL_HOURS * 3600
        if not due and not force_check:
            return None
        try:
            entry = bulk_entry()
        except (net.NetError, RuntimeError):
            _set_meta(conn, "bulk_check_at", datetime.now(timezone.utc).isoformat())
            return None
        _set_meta(conn, "bulk_check_at", datetime.now(timezone.utc).isoformat())
        if entry.get("updated_at") != bulk_updated:
            local = _get_meta(conn, "bulk_downloaded_at") or "?"
            return (
                "Card database may be stale (local copy from "
                f"{local[:10]}, Scryfall bulk updated "
                f"{entry['updated_at'][:10]}). Consider `mtg refresh cards`."
            )
        return None
    finally:
        conn.close()
