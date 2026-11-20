import tempfile
import unittest
from pathlib import Path

from mtgjudge import carddb
from mtgjudge.carddb import _bulk_row_from_card, normalize_name

BULK_CARD = {
    "id": "abc123",
    "name": "Zndrsplt, Eye of Wisdom",
    "layout": "normal",
    "released_at": "2016-04-08",
    "card_faces": [],
    "mana_cost": "{4}{U}",
    "type_line": "Legendary Creature — Homunculus",
    "oracle_text": "Partner with Okaun.",
    "power": "1",
    "toughness": "4",
}

MDFC = {
    "id": "xyz789",
    "name": "Bemused Avuncular // Trench Gnome",
    "layout": "modal_dfc",
    "released_at": "2021-02-05",
    "card_faces": [
        {"name": "Bemused Avuncular", "mana_cost": "{1}{U}",
         "type_line": "Creature — Homunculus", "oracle_text": "Draw a card."},
        {"name": "Trench Gnome", "mana_cost": "{2}{B}",
         "type_line": "Creature — Gnome", "oracle_text": "Mill 2 cards."},
    ],
}


class NormalizeTest(unittest.TestCase):
    def test_diacritics_case_punct(self):
        self.assertEqual(normalize_name("Jeska's Will"), "jeskas will")
        self.assertEqual(normalize_name("Lim-Dûl the Necromancer"), "lim dul the necromancer")
        self.assertEqual(normalize_name("  FIRE // ICE  "), "fire ice")


class BulkRowTest(unittest.TestCase):
    def test_plain_card(self):
        row = _bulk_row_from_card(BULK_CARD)
        self.assertEqual(row["id"], "abc123")
        self.assertIn("zndrsplt eye of wisdom", row["lookup_all"])
        self.assertEqual(row["nospace"], row["lookup_all"].replace(" ", ""))
        self.assertEqual(row["oracle_text"], "Partner with Okaun.")

    def test_mdfc_faces_merged(self):
        row = _bulk_row_from_card(MDFC)
        self.assertIn("bemused avuncular", row["lookup_all"])
        self.assertIn("trench gnome", row["lookup_all"])
        self.assertEqual(row["mana_cost"], "{1}{U} // {2}{B}")
        self.assertIn("Homunculus //", row["type_line"])
        self.assertIn("// Creature — Gnome", row["type_line"])
        self.assertIn("Draw a card. // Mill 2 cards.", row["oracle_text"])


class LookupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self.tmp.name) / "cards.db"
        self.conn = carddb.connect(self.db_file)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)
        for card in (BULK_CARD, MDFC):
            carddb._insert_cards(self.conn, [_bulk_row_from_card(card)])
        # extra: apostrophe-free spelling should still match via nospace
        self.jeska = _bulk_row_from_card({
            "id": "j1", "name": "Jeska's Will", "layout": "normal",
            "released_at": "2021-04-23", "card_faces": [], "mana_cost": "{2}{R}",
            "type_line": "Sorcery", "oracle_text": "Exile top card.",
        })
        carddb._insert_cards(self.conn, [self.jeska])

    def lookup(self, name):
        return carddb.lookup_card(name, offline=True, db_file=self.db_file)

    def test_exact(self):
        self.assertEqual(self.lookup("Zndrsplt, Eye of Wisdom")["id"], "abc123")

    def test_case_punct_insensitive(self):
        self.assertEqual(self.lookup("zndrsplt eye of wisdom")["id"], "abc123")

    def test_second_face_name(self):
        self.assertEqual(self.lookup("Trench Gnome")["id"], "xyz789")

    def test_partial_name_not_found(self):
        # strict: word/fragment matches are never "fixed" to a card
        from mtgjudge.cards import CardNotFound
        with self.assertRaises(CardNotFound):
            self.lookup("avuncular gnome")
        with self.assertRaises(CardNotFound):
            self.lookup("avuncular")

    def test_five_word_name_found(self):
        conn = carddb.connect(self.db_file)
        carddb._insert_cards(conn, [
            _bulk_row_from_card({"id": "wsm", "name": "Wheel of Sun and Moon",
                                 "layout": "normal", "card_faces": [],
                                 "mana_cost": "{1}{W}{B}", "type_line": "Enchantment — Aura",
                                 "oracle_text": "Replacement stuff.", "released_at": "2008-05-02"}),
        ])
        conn.commit()
        conn.close()
        self.assertEqual(self.lookup("Wheel of Sun and Moon")["name"],
                         "Wheel of Sun and Moon")

    def test_single_word_fragments_rejected(self):
        # "gnom" is not a whole word anywhere; nothing matches it
        from mtgjudge.cards import CardNotFound
        with self.assertRaises(CardNotFound):
            self.lookup("gnom")

    def test_apostrophe_punctuation_leeway(self):
        # punctuation maps to spaces on both sides: typed ' == stored '
        self.assertEqual(self.lookup("Jeska's Will")["id"], "j1")

    def test_missing_apostrophe_matches(self):
        # apostrophes are deleted (not spaced) when normalizing, so the
        # user typing "Jeskas Will" finds "Jeska's Will" (2026-11-19)
        self.assertEqual(self.lookup("jeskas will")["id"], "j1")

    def test_not_found_raises(self):
        from mtgjudge.cards import CardNotFound
        with self.assertRaises(CardNotFound):
            self.lookup("Completely Made Up Card")

    def test_row_to_card_roundtrip(self):
        conn = carddb.connect(self.db_file)
        try:
            row = conn.execute("SELECT * FROM cards WHERE id = 'xyz789'").fetchone()
        finally:
            conn.close()
        card = carddb._row_to_card(row)
        self.assertEqual(card["name"], "Bemused Avuncular // Trench Gnome")
        self.assertTrue(card["rulings_uri"].endswith("/cards/xyz789/rulings"))

    def test_exact_face_beats_popular_prefix_match(self):
        # live-2026-11-19 bug: "Mountain" resolved to "Mountain Valley"
        # because the padded word match also hits name prefixes and
        # edhrec popularity put ranked cards above NULL-ranked basics.
        # Tier 0 (exact face name) must win.
        conn = carddb.connect(self.db_file)
        carddb._insert_cards(conn, [
            _bulk_row_from_card({"id": "mtn", "name": "Mountain",
                                 "layout": "normal", "card_faces": [],
                                 "mana_cost": "", "type_line": "Basic Land — Mountain",
                                 "oracle_text": "{T}: Add {R}.",
                                 "released_at": "1993-08-05"}),
            _bulk_row_from_card({"id": "mtv", "name": "Mountain Valley",
                                 "layout": "normal", "card_faces": [],
                                 "mana_cost": "", "type_line": "Land",
                                 "oracle_text": "", "released_at": "1996-10-01",
                                 "edhrec_rank": 4999}),
        ])
        conn.commit()
        conn.close()
        self.assertEqual(self.lookup("Mountain")["id"], "mtn")
        # exact names are not ambiguous: no disambiguation candidate list
        alts = carddb.lookup_alternates("Mountain", db_file=self.db_file)
        self.assertEqual([a["id"] for a in alts], ["mtn"])
        # singular miss -> "did you mean": edit distance finds the typo
        alts = carddb.lookup_alternates("Mountan", db_file=self.db_file)
        self.assertEqual([a["id"] for a in alts], ["mtn"])
        self.assertEqual(alts[0]["name"], "Mountain")


class ReimportTest(unittest.TestCase):
    """Re-running `mtg refresh cards` must not hit UNIQUE(card_id)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self.tmp.name) / "cards.db"
        self.addCleanup(self.tmp.cleanup)

    def test_reimport_replaces(self):
        row = _bulk_row_from_card(BULK_CARD)
        conn = carddb.connect(self.db_file)
        self.addCleanup(conn.close)
        carddb._insert_cards(conn, [dict(row)])
        row["oracle_text"] = "Updated text."
        carddb._insert_cards(conn, [dict(row)])  # must replace, not raise
        n = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        self.assertEqual(n, 1)
        text = conn.execute("SELECT oracle_text FROM cards").fetchone()[0]
        self.assertEqual(text, "Updated text.")


class RulingsCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self.tmp.name) / "cards.db"
        self.addCleanup(self.tmp.cleanup)
        conn = carddb.connect(self.db_file)
        carddb._insert_cards(conn, [_bulk_row_from_card(BULK_CARD)])
        conn.execute(
            "INSERT INTO rulings (card_id, published_at, source, comment) VALUES "
            "('abc123', '2022-01-01T00:00:00Z', 'wotc', 'A ruling.')")
        conn.commit()
        conn.close()

    def test_cached_ruling_returned_offline(self):
        card = carddb.lookup_card("Zndrsplt, Eye of Wisdom", offline=True,
                                  db_file=self.db_file)
        rulings = carddb.get_rulings(card, offline=True, db_file=self.db_file)
        self.assertEqual(len(rulings), 1)
        self.assertEqual(rulings[0]["comment"], "A ruling.")


class LookupAlternatesTest(unittest.TestCase):
    """Explicit [[Name]] references are the only trigger; ambiguous ones
    prompt interactively from these candidate lists."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self.tmp.name) / "cards.db"
        self.addCleanup(self.tmp.cleanup)
        fixture_cards = [
            {"id": "sv1", "name": "Selvala's Charge", "layout": "normal",
             "card_faces": [], "mana_cost": "{3}{G}", "type_line": "Instant",
             "oracle_text": "", "released_at": "2014-06-06"},
            {"id": "sv2", "name": "Selvala, Eager Trailblazer", "layout": "normal",
             "card_faces": [], "mana_cost": "{1}{G}{G}",
             "type_line": "Legendary Creature — Elf Scout",
             "oracle_text": "", "released_at": "2024-04-19"},
            {"id": "sv3", "name": "Selvala, Explorer Returned", "layout": "normal",
             "card_faces": [], "mana_cost": "{1}{G}{G}",
             "type_line": "Legendary Creature — Elf Scout",
             "oracle_text": "", "released_at": "2024-02-09"},
            {"id": "lh", "name": "Lightning Helix", "layout": "normal",
             "card_faces": [], "mana_cost": "{R}{W}", "type_line": "Instant",
             "oracle_text": "Deals 3 damage. You gain 3 life.",
             "released_at": "2005-10-07"},
        ]
        conn = carddb.connect(self.db_file)
        carddb._insert_cards(conn, [_bulk_row_from_card(c) for c in fixture_cards])
        conn.commit()
        conn.close()

    def test_ambiguous_name_lists_candidates(self):
        alts = carddb.lookup_alternates("Selvala", db_file=self.db_file)
        names = [a["name"] for a in alts]
        # "Selvala's Charge" is NOT a candidate anymore: the apostrophe
        # normalizes away, so "selvalas" is a different word than
        # "selvala". The two legendary Selvalas are what's meant by
        # [[Selvala]].
        self.assertEqual(len(names), 2)
        self.assertIn("Selvala, Eager Trailblazer", names)
        self.assertIn("Selvala, Explorer Returned", names)

    def test_unique_name_single_candidate(self):
        alts = carddb.lookup_alternates("lightning helix", db_file=self.db_file)
        self.assertEqual([a["id"] for a in alts], ["lh"])

    def test_strict_lookup_reports_not_found(self):
        # lookup never guesses; ambiguous/disambiguated names raise.
        # The interactive chooser works off the alternates list instead.
        from mtgjudge.cards import CardNotFound
        with self.assertRaises(CardNotFound):
            carddb.lookup_card("Selvala", offline=True, db_file=self.db_file)
        alts = carddb.lookup_alternates("Selvala", db_file=self.db_file)
        self.assertEqual(len(alts), 2)

    def test_typo_falls_back_to_close_matches(self):
        # word-match finds nothing for a misspelling; the edit-distance
        # pass suggests the real card for the "did you mean" notice
        alts = carddb.lookup_alternates("Lighting Helix", db_file=self.db_file)
        self.assertEqual([a["id"] for a in alts], ["lh"])

    def test_missing_db_gives_no_alternates(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(carddb.lookup_alternates(
                "Selvala", db_file=Path(tmp) / "nothing.db"), [])


if __name__ == "__main__":
    unittest.main()
