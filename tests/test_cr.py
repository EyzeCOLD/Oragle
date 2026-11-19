import unittest

from mtgjudge import cr
from mtgjudge.cr import Rule, match_rule, parse_cr, rule_sort_key

FIXTURE = """Magic: The Gathering Comprehensive Rules

These rules are effective as of November 6, 2026.

Contents

1. Game Concepts
100. General

1. Game Concepts
100. General

100.1. These Magic rules apply to any Magic game with two or more players.
100.1a A two-player game is a game that begins with only two players.
100.1b A multiplayer game.
Example: An example paragraph attaches to the previous rule.

2. Parts of a Card

201. Name

201.1. A card's name.
201.2. Name stats.
201.2a Details.

702. Keyword Abilities

702.2. Deathtouch
702.2a Deathtouch is a static ability.
702.2b Damage rule.
702.20. Dummy
702.20a Dummy subrule.

700.4. Dies means moved from battlefield to graveyard.

Glossary

Ability
Some card has an ability.

Credits

Designer
A person.
"""


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.meta, self.chunks = parse_cr(FIXTURE)

    def test_effective_date(self):
        self.assertEqual(self.meta["effective_date"], "November 6, 2026")

    def test_chunk_count_and_numbers(self):
        numbers = [c["number"] for c in self.chunks]
        self.assertEqual(
            numbers,
            [
                "100.1", "100.1a", "100.1b",
                "201.1", "201.2", "201.2a",
                "702.2", "702.2a", "702.2b", "702.20", "702.20a",
                "700.4",
            ],
        )

    def test_glossary_not_included(self):
        self.assertFalse(any("Some card has an ability" in c["text"] or "A person." in c["text"] for c in self.chunks))

    def test_headings_kept(self):
        rule_702 = self.chunks[6]
        self.assertEqual(rule_702["section"], "Keyword Abilities")
        self.assertEqual(rule_702["chapter"], "Parts of a Card")

    def test_example_attached(self):
        rule = self.chunks[2]  # 100.1b
        self.assertIn("Example:", rule["text"])
        self.assertIn("\n", rule["text"])

    def test_parent_chunk_excludes_subrules(self):
        parents = [c for c in self.chunks if c["number"] == "702.2"]
        self.assertEqual(len(parents), 1)
        self.assertEqual(parents[0]["text"], "Deathtouch")


class MatchTest(unittest.TestCase):
    def setUp(self):
        self.chunks = [
            Rule(number=n, text="", chapter="", section="")
            for n in ["702.2", "702.2a", "702.2b", "702.20", "702.20a", "702.3", "100.1"]
        ]

    def test_subrules_included_702_20_excluded(self):
        got = [c["number"] for c in match_rule(self.chunks, "702.2")]
        self.assertEqual(got, ["702.2", "702.2a", "702.2b"])

    def test_missing_rule_raises(self):
        with self.assertRaises(KeyError):
            match_rule(self.chunks, "999.9")

    def test_sort_order(self):
        keys = sorted(["702.20", "702.2b", "702.2", "702.3"], key=rule_sort_key)
        self.assertEqual(keys, ["702.2", "702.2b", "702.3", "702.20"])

    def test_trailing_dot_tolerated(self):
        got = [c["number"] for c in match_rule(self.chunks, "100.1.")]
        self.assertEqual(got, ["100.1"])


class KeywordAbilitiesTest(unittest.TestCase):
    def test_section_702_names(self):
        _, chunks = parse_cr(FIXTURE)
        # 702.2 Deathtouch and 702.20 Dummy are both single-name 702 rules
        kws = cr.keyword_abilities(chunks)
        self.assertEqual(kws.get("deathtouch"), "702.2")
        self.assertEqual(kws.get("dummy"), "702.20")


if __name__ == "__main__":
    unittest.main()
