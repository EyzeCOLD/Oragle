"""BM25 keyword search over rule chunks."""

import math
import re

TOKEN_RE = re.compile(r"[a-z0-9]+")
RULE_NUM_IN_QUERY_RE = re.compile(r"\b(\d{1,3}\.\d+(?:\.\d+)*[a-z]?)\b")

STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "is", "are", "was", "be", "being",
    "and", "or", "it", "its", "that", "this", "for", "on", "as", "with",
    "if", "i", "you", "my", "me", "does", "do", "can", "what", "when",
    "how", "not", "they", "their",
}

# Player slang -> Comprehensive Rules vocabulary. Applied to query terms.
QUERY_SYNONYMS = {
    "kill": ["destroy", "lethal", "dies"],
    "kills": ["destroy", "lethal", "dies"],
    "killed": ["destroy", "lethal", "dies"],
    "dies": ["destroy", "graveyard"],
    "die": ["destroy", "graveyard"],
    "died": ["destroy", "graveyard"],
    "remove": ["exile"],
    "removed": ["exile"],
    "removal": ["exile", "destroy"],
    "bounce": ["return", "hand"],
    "bounces": ["return", "hand"],
    "bounced": ["return", "hand"],
    "bouncing": ["return", "hand"],
    "flicker": ["exile", "return", "battlefield"],
    "flickers": ["exile", "return", "battlefield"],
    "flickering": ["exile", "return", "battlefield"],
    "blink": ["exile", "return", "battlefield"],
    "blinks": ["exile", "return", "battlefield"],
    "blinking": ["exile", "return", "battlefield"],
    "wipe": ["destroy", "all"],
    "wipes": ["destroy", "all"],
    "wiped": ["destroy", "all"],
    "boardwipe": ["destroy", "all"],
    "tutor": ["library", "search"],
    "tutors": ["library", "search"],
    "tutored": ["library", "search"],
    "tutoring": ["library", "search"],
    "cantrip": ["draw", "card"],
    "ramp": ["mana", "land"],
    "ramping": ["mana", "land"],
    "burn": ["damage"],
    "burns": ["damage"],
    "burning": ["damage"],
    "ping": ["damage"],
    "pings": ["damage"],
    "pumping": ["power", "toughness"],
    "pump": ["power", "toughness"],
    "pumped": ["power", "toughness"],
    "buffd": [],
    "buff": ["power", "toughness"],
    "buffs": ["power", "toughness"],
    "nerf": ["power", "toughness"],
    "nerfs": ["power", "toughness"],
    "chump": ["block", "blocker"],
    "chumping": ["block", "blocker"],
    "chumpblock": ["block", "blocker"],
    "swing": ["attack"],
    "swings": ["attack"],
    "swinging": ["attack"],
    "sick": ["sickness"],
    "etb": ["enters", "battlefield"],
    "etbs": ["enters", "battlefield"],
    "ltb": ["leaves", "battlefield"],
    "sac": ["sacrifice"],
    "saced": ["sacrifice"],
    "sacked": ["sacrifice"],
    "loot": ["draw", "discard"],
    "looting": ["draw", "discard"],
    "fizzle": ["countered", "target"],
    "fizzles": ["countered", "target"],
    "fizzled": ["countered", "target"],
    "counterspell": ["counter", "spell"],
    "respond": ["priority", "stack"],
    "responds": ["priority", "stack"],
    "responding": ["priority", "stack"],
    "response": ["priority"],
    "reanimate": ["graveyard", "battlefield"],
    "reanimated": ["graveyard", "battlefield"],
    "reanimation": ["graveyard", "battlefield"],
    "scoop": ["concede"],
    "scoops": ["concede"],
    "scooping": ["concede"],
    "opp": ["opponent"],
    "opps": ["opponent"],
    "cmdr": ["commander"],
    "gainlife": ["life"],
    "play": ["cast", "battlefield"],
    "played": ["cast", "battlefield"],
    "proc": ["trigger"],
    "procs": ["trigger"],
    "wheels": ["discard", "draw"],
    "copies": ["copy"],
}

# Format words in a question -> CR rules worth having in context regardless
# of BM25 ranking. Keys are token tuples matched by set-inclusion on the
# original question; values are rule numbers verified against the CR.
# Chosen deliberately small: anchors must be load-bearing, not chapter dumps.
FORMAT_ANCHORS = {
    ("two", "headed", "giant"): ["810.8a", "805.1"],
    ("2hg",): ["810.8a", "805.1"],
    ("archenemy",): ["805.1"],
    ("commander",): ["903.8", "903.10a"],
    ("oathbreaker",): ["903.8"],
}


def question_anchor_numbers(question):
    """Rule numbers preloaded for format vocabulary in the question
    (deterministic; rescues these rules even when BM25 ranking misses
    them). Matches on raw lowercase tokens, not the BM25 token list,
    because anchor words like 'two' are stopword-filtered there."""
    raw = {t for t in TOKEN_RE.findall(question.lower())}
    tokens = set(raw)
    for t in raw:  # shorthand counts: 'cmdr' -> commander anchor
        for synonym in QUERY_SYNONYMS.get(t, []):
            tokens.update(synonym.split())
    out = []
    for words, numbers in FORMAT_ANCHORS.items():
        if all(w in tokens for w in words):
            out.extend(numbers)
    return list(dict.fromkeys(out))


def tokenize(text):
    return [t for t in TOKEN_RE.findall(text.lower()) if t not in STOPWORDS]


def expand_query(text):
    """Tokenize plus slang expansion. Keeps original terms; adds rules-
    vocabulary synonyms so 'does X kill Y' reaches 704/destroy rules."""
    tokens = tokenize(text)
    extra = []
    for token in tokens:
        extra.extend(QUERY_SYNONYMS.get(token, []))
    return tokens + [t for t in extra if t not in tokens]


class BM25:
    def __init__(self, documents, k1=1.5, b=0.75):
        self.k1 = k1
        self.b = b
        self.docs = [tokenize(d) for d in documents]
        self.doc_len = [len(d) for d in self.docs]
        self.avgdl = sum(self.doc_len) / len(self.doc_len) if self.doc_len else 0.0
        self.df = {}
        for doc in self.docs:
            for term in set(doc):
                self.df[term] = self.df.get(term, 0) + 1
        self.n = len(self.docs)

    def idf(self, term):
        df = self.df.get(term, 0)
        if df == 0:
            return 0.0
        return math.log((self.n - df + 0.5) / (df + 0.5) + 1.0)

    def score(self, query_tokens, index):
        doc = self.docs[index]
        dl = self.doc_len[index]
        score = 0.0
        doc_term_counts = {}
        for t in doc:
            doc_term_counts[t] = doc_term_counts.get(t, 0) + 1
        for term in set(query_tokens):
            tf = doc_term_counts.get(term, 0)
            if tf == 0:
                continue
            idf = self.idf(term)
            denom = tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
            score += idf * tf * (self.k1 + 1) / denom
        return score

    def search(self, query, top_k=5):
        query_tokens = expand_query(query)
        if not query_tokens and not RULE_NUM_IN_QUERY_RE.search(query):
            return []
        scores = []
        for i in range(self.n):
            s = self.score(query_tokens, i)
            if s > 0:
                scores.append((i, s))
        scores.sort(key=lambda x: (-x[1], x[0]))
        return scores[:top_k]


def build_index(chunks):
    """Indexed text = rule number + headings + body. Headings add signal
    ('dies' queries should hit section headings too)."""
    documents = [
        f"{c['number']} {c['chapter']} {c['section']} {c['text']}".strip()
        for c in chunks
    ]
    return BM25(documents)


def search_rules(chunks, query, top_k=5):
    """Return [(rule, score)] ranked; rule numbers mentioned verbatim get a
    large boost so 'mtg search 702.2' finds exactly that rule."""
    index = build_index(chunks)
    results = [(chunks[i], score) for i, score in index.search(query, top_k=top_k)]
    mentioned = RULE_NUM_IN_QUERY_RE.findall(query)
    hits = []
    for number in mentioned:
        for c in chunks:
            if c["number"] == number.rstrip("."):
                hits.append(c)
                break
    if hits:
        merged = list(hits)
        for r, _ in results:
            if all(r["number"] != h["number"] for h in merged):
                merged.append(r)
        results = [(c, 10.0 + i) for i, c in enumerate(merged[:top_k])]
    return results[:top_k]
