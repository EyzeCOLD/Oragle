import unittest

from mtgjudge import search
from mtgjudge.cr import Rule
from mtgjudge.cards import extract_card_refs


def chunk(number, text, section=""):
    return Rule(number=number, text=text, chapter="", section=section)


CHUNKS = [
    chunk("700.4", "The term dies means is put into a graveyard from the battlefield."),
    chunk("702.2b", "Deathtouch damage destroys a creature with toughness greater than 0."),
    chunk("508.1", "First the active player declares attackers."),
    chunk("510.1a", "Combat damage assignment order of blockers."),
    chunk("117.5", "Costs are paid with mana."),
]


class BM25Test(unittest.TestCase):
    def test_dies_finds_definition(self):
        results = search.search_rules(CHUNKS, "what does dies mean", top_k=3)
        self.assertEqual(results[0][0]["number"], "700.4")

    def test_deathtouch(self):
        results = search.search_rules(CHUNKS, "deathtouch destroys creature", top_k=2)
        self.assertEqual(results[0][0]["number"], "702.2b")

    def test_no_match_returns_empty(self):
        self.assertEqual(search.search_rules(CHUNKS, "xyzzy", top_k=2), [])

    def test_rule_number_query_boosted(self):
        results = search.search_rules(CHUNKS, "117.5", top_k=1)
        self.assertEqual(results[0][0]["number"], "117.5")


class CardRefTest(unittest.TestCase):
    def test_extraction(self):
        names, cleaned = extract_card_refs(
            "does [[ Lightning Helix ]] kill [[Grizzly Bears]] ?"
        )
        self.assertEqual(names, ["Lightning Helix", "Grizzly Bears"])
        self.assertEqual(cleaned, "does Lightning Helix kill Grizzly Bears ?")


if __name__ == "__main__":
    unittest.main()


class SlangExpansionTest(unittest.TestCase):
    def test_new_synonyms_add_rules_vocabulary(self):
        for slang, must in [
            ("bounce", "return"), ("flicker", "exile"),
            ("tutor", "library"), ("boardwipe", "destroy"),
            ("sick", "sickness"), ("etb", "battlefield"),
            ("fizzle", "countered"), ("sac", "sacrifice"),
            ("cmdr", "commander"), ("opp", "opponent"),
        ]:
            expanded = search.expand_query(slang)
            self.assertIn(must, expanded, slang)

    def test_original_terms_kept(self):
        # 'does' is a stopword (correctly dropped); slang and its
        # expansion must both survive
        expanded = search.expand_query("does bounce work")
        for term in ("bounce", "work", "return", "hand"):
            self.assertIn(term, expanded)


class FormatAnchorTest(unittest.TestCase):
    def test_two_headed_giant(self):
        self.assertEqual(search.question_anchor_numbers(
            "In a Two-Headed Giant game, what happens?"),
            ["810.8a", "805.1"])

    def test_shorthand_counts(self):
        self.assertEqual(search.question_anchor_numbers("can my cmdr die?"),
                         ["903.8", "903.10a"])

    def test_no_format_words_no_anchors(self):
        self.assertEqual(search.question_anchor_numbers(
            "does Lightning Helix kill Grizzly Bears?"), [])


class GoldenAnchorTest(unittest.TestCase):
    """101.2 (can't beats can) is never cited by other rules, so retrieval
    alone can't guarantee its presence; cli ships it as a golden anchor."""

    def setUp(self):
        try:
            from mtgjudge import store
            store.load_rules()
        except Exception:
            self.skipTest("rules data not downloaded yet")

    def test_golden_rules_exist_in_index(self):
        from mtgjudge import cli, store
        meta, chunks = store.load_rules()
        numbers = {c["number"] for c in chunks}
        for number in cli.GOLDEN_ANCHORS:
            self.assertIn(number, numbers)

    def test_golden_rules_are_meta_rules(self):
        # the invariant that justifies always-on inclusion: they are about
        # rule-vs-rule conflicts in general, not specific interactions
        from mtgjudge import store
        meta, chunks = store.load_rules()
        by_number = {c["number"]: c for c in chunks}
        self.assertIn("can", by_number["101.2"]["text"])
        self.assertIn("card", by_number["101.1"]["text"])
