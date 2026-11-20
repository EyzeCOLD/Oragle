import unittest

from mtgjudge import deck


class ParseTest(unittest.TestCase):
    def test_count_prefix(self):
        entries = deck._lines_to_entries(["4 Lightning Bolt"])
        self.assertEqual(entries, [(4, "Lightning Bolt")])

    def test_x_prefix(self):
        entries = deck._lines_to_entries(["4x Lightning Bolt"])
        self.assertEqual(entries, [(4, "Lightning Bolt")])

    def test_x_suffix(self):
        entries = deck._lines_to_entries(["Lightning Bolt x4"])
        self.assertEqual(entries, [(4, "Lightning Bolt")])

    def test_set_suffixes(self):
        for line in ["4 Fire // Ice (ARB)",
                     "4 Fire // Ice [ARB]",
                     "4 Fire // Ice (ARB) 45",
                     "4 Fire // Ice [ARB] #45"]:
            self.assertEqual(deck._lines_to_entries([line]),
                             [(4, "Fire // Ice")], line)

    def test_headers_comments_blanks_ignored(self):
        lines = ["Deck",
                 "// a comment",
                 "1 Karn, Gilded Guardian",
                 "",
                 "Sideboard",
                 "# comment",
                 "2 Fire // Ice"]
        entries = deck._lines_to_entries(lines)
        self.assertEqual(entries, [(1, "Karn, Gilded Guardian"),
                                   (2, "Fire // Ice")])

    def test_split_cards_survive_comment_detection(self):
        # "//" only means comment at the START of a line (MTGO export
        # convention); split cards carry their "//" mid-name, usually
        # behind a count, and must never be eaten
        entries, rest = deck.extract_deck(
            "combo?\n"
            "1 Brazen Borrower // Petty Theft\n"
            "4 Fire // Ice (ARB)\n"
            "2 Fire // Ice\n"
            "// this is a real MTGO comment\n"
            "3 Brazen Borrower // Petty Theft")
        self.assertEqual(entries,
                         [(4, "Brazen Borrower // Petty Theft"),
                          (6, "Fire // Ice")])
        # comments live inside the deck region, so they are stripped
        # along with it
        self.assertNotIn("real MTGO comment", rest)
        # x-suffix split lines parse whole as well
        entries, _rest = deck.extract_deck(
            "<deck>\nBrazen Borrower // Petty Theft x2\n"
            "Fire // Ice x4\n4 Island\n</deck>")
        self.assertEqual(entries[0],
                         (2, "Brazen Borrower // Petty Theft"))

    def test_countless_split_line_inside_markers_ignored(self):
        # count-less name lines are not deck material (this is what
        # keeps prose safe); markers wrap a deck, they don't lower the
        # bar for what a card line is
        entries, _rest = deck.extract_deck(
            "<deck>\nBrazen Borrower // Petty Theft\n4 Island\n"
            "2 Dark Ritual\n</deck>")
        self.assertEqual([n for _c, n in entries],
                         ["Island", "Dark Ritual"])

    def test_prose_line_not_a_card(self):
        self.assertIsNone(deck._classify("what happens when I attack?"))
        # a bare name without a count is NOT a decklist line (this is
        # what keeps ordinary multi-line questions from being eaten)
        self.assertIsNone(deck._classify("Lightning Bolt"))


class ExtractTest(unittest.TestCase):
    def test_arena_export_inside_question(self):
        question = ("any infinite combos here?\n"
                    "Deck\n"
                    "4 Priest of Titania\n"
                    "20 Mountain (ROE) 187\n"
                    "Staff of Domination x1\n"
                    "4 Elvish Archers\n"
                    "\n"
                    "Sideboard\n"
                    "2 Fire // Ice\n"
                    "why does this work?")
        entries, rest = deck.extract_deck(question)
        names = [n for _c, n in entries]
        self.assertIn("Priest of Titania", names)
        self.assertIn("Mountain", names)
        self.assertIn("Staff of Domination", names)
        self.assertIn("Fire // Ice", names)
        self.assertEqual(entries[0][0], 4)
        self.assertIn("any infinite combos here?", rest)
        self.assertIn("(decklist:", rest)
        self.assertIn("why does this work?", rest)
        self.assertNotIn("Priest of Titania", rest)

    def test_duplicates_merged(self):
        question = ("look:\n1 Bolt\n2 Bolt\n1 Bolt")
        entries, _rest = deck.extract_deck(question)
        self.assertEqual(entries, [(4, "Bolt")])

    def test_threshold_not_reached(self):
        question = ("combo check: is Lightning Helix + Grizzly Bears a " 
                    "thing?\ndoes it loop?")
        entries, rest = deck.extract_deck(question)
        self.assertEqual(entries, [])
        self.assertEqual(rest, question)

    def test_explicit_markers(self):
        question = ("does this work?\n<deck>\nSans count line here\n"
                    "1 Zndrsplt, Eye of Wisdom\n4 Island\n"
                    "</deck>\nthanks")
        entries, rest = deck.extract_deck(question)
        names = [n for _c, n in entries]
        self.assertEqual(names, ["Zndrsplt, Eye of Wisdom", "Island"])
        self.assertIn("does this work?", rest)
        self.assertIn("thanks", rest)
        self.assertNotIn("Zndrsplt", rest)

    def test_marker_overrides_line_rules(self):
        # inside markers, a header-only or odd line is just noise, not a
        # reason to reject the whole deck
        question = "<deck>\nheader-ish noise\n2 Swamp\n4 Dark Ritual\n</deck>"
        entries, _rest = deck.extract_deck(question)
        self.assertEqual([n for _c, n in entries],
                         ["Swamp", "Dark Ritual"])

    def test_no_markers_no_deck(self):
        entries, rest = deck.extract_deck("what is trample?")
        self.assertEqual(entries, [])
        self.assertEqual(rest, "what is trample?")


import unittest
from unittest import mock

from mtgjudge import deck


def _stub_card(name):
    return {"id": "id-" + name, "name": name, "mana_cost": "{1}",
            "type_line": "Thing", "oracle_text": "text"}


class CardBlocksIntegrationTest(unittest.TestCase):
    """The ask pipeline hook: a pasted deck becomes CARD blocks with
    copy counts, in one pass, without LLM tool rounds."""

    QUESTION = ("any infinite combos?\n"
                "4 Priest of Titania\n"
                "20 Mountain (ROE) 187\n"
                "1 Staff of Domination\n")

    def test_deck_becomes_card_blocks(self):
        from mtgjudge import cli
        with mock.patch.multiple(
                "mtgjudge.cli.carddb",
                lookup_card=mock.Mock(side_effect=lambda name, offline: {
                    "Priest of Titania": _stub_card("Priest of Titania"),
                    "Mountain": _stub_card("Mountain"),
                    "Staff of Domination": _stub_card("Staff of Domination"),
                }[name])), \
             mock.patch("mtgjudge.cards.format_card",
                        side_effect=lambda card: "oracle of " + card["name"]):
            cleaned, blocks = cli._card_blocks([], self.QUESTION,
                                               offline=True)
        self.assertEqual(len(blocks), 3)
        first = blocks[0]
        self.assertTrue(first.startswith("CARD Priest of Titania\n"))
        self.assertIn("4 copies in the decklist", first)
        self.assertIn("oracle of Priest of Titania", first)
        self.assertNotIn("Priest of Titania", cleaned)
        self.assertIn("any infinite combos?", cleaned)
        self.assertIn("(decklist: 3 unique cards", cleaned)

    def test_missing_deck_card_reported(self):
        question = "does it work?\n4 Totally Made Up\n2 Also Fake\n9 Nope No"
        from mtgjudge import cli, cards as cards_mod
        with mock.patch.object(
                cli.carddb, "lookup_card",
                side_effect=cards_mod.CardNotFound("not found")), \
             mock.patch("mtgjudge.cards.format_card",
                        side_effect=lambda c: "oracle") as _fmt:
            cleaned, blocks = cli._card_blocks([], question, offline=True)
        self.assertEqual(blocks, [])
        self.assertIn("(decklist: 3 unique cards", cleaned)

    def test_ambiguous_ref_prompts_interactively(self):
        # strict lookup (2026-11-19): [[Selvala]] does not resolve to any
        # single card. Interactive mode offers the chooser; the pick wins
        # instead of a silent guess.
        from mtgjudge import cli, cards as cards_mod
        selvalas = [_stub_card("Selvala, Eager Trailblazer"),
                    _stub_card("Selvala, Explorer Returned")]
        with mock.patch.object(
                cli.carddb, "lookup_card",
                side_effect=cards_mod.CardNotFound("nope")), \
             mock.patch.object(cli.carddb, "lookup_alternates",
                               return_value=selvalas), \
             mock.patch.object(cli, "_choose_card",
                               return_value=selvalas[1]) as chooser, \
             mock.patch("mtgjudge.cards.format_card", return_value="oracle"):
            cleaned, blocks = cli._card_blocks(
                [], "does [[Selvala]] work?", offline=True, interactive=True)
        chooser.assert_called_once_with("Selvala", selvalas[0], selvalas[1:5])
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0].startswith("CARD Selvala, Explorer Returned"))

    def test_ambiguous_ref_non_interactive_reports_candidates(self):
        # web / `ask`: no silent fixing ever; the NOT FOUND block lists
        # similar names so the model can ask the user to clarify.
        from mtgjudge import cli, cards as cards_mod
        selvalas = [_stub_card("Selvala, Eager Trailblazer"),
                    _stub_card("Selvala, Explorer Returned")]
        with mock.patch.object(
                cli.carddb, "lookup_card",
                side_effect=cards_mod.CardNotFound("nope")), \
             mock.patch.object(cli.carddb, "lookup_alternates",
                               return_value=selvalas):
            cleaned, blocks = cli._card_blocks(
                [], "does [[Selvala]] work?", offline=True)
        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        self.assertTrue(block.startswith("CARD Selvala\nNOT FOUND"))
        self.assertIn("Did you mean one of:", block)
        self.assertIn("Selvala, Eager Trailblazer", block)
        self.assertIn("Selvala, Explorer Returned", block)

    def test_typo_ref_single_suggestion(self):
        # "did you mean" for singular misses: no ambiguity, just a typo
        from mtgjudge import cli, cards as cards_mod
        helix = _stub_card("Lightning Helix")
        with mock.patch.object(
                cli.carddb, "lookup_card",
                side_effect=cards_mod.CardNotFound("nope")), \
             mock.patch.object(cli.carddb, "lookup_alternates",
                               return_value=[helix]):
            cleaned, blocks = cli._card_blocks(
                [], "does [[Lighting Helix]] target?", offline=True)
        self.assertEqual(len(blocks), 1)
        self.assertIn("Did you mean: Lightning Helix?", blocks[0])

    def test_deck_miss_note_suggests(self):
        notes = []
        from mtgjudge import cli, cards as cards_mod
        with mock.patch.object(
                cli.carddb, "lookup_card",
                side_effect=cards_mod.CardNotFound("nope")), \
             mock.patch.object(cli.carddb, "lookup_alternates",
                               return_value=[_stub_card("Mountain")]):
            cli._card_blocks([], "combo?\n4 Mountan\n2 Nope\n1 Nada\n",
                             offline=True, emit=notes.append)
        self.assertTrue(any("did you mean Mountain?" in n for n in notes))


if __name__ == "__main__":
    unittest.main()
