import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from mtgjudge.cli import _choose_card, _context_ids, parse_bench


DEFAULT = {"id": "1", "name": "Selvala, Explorer Returned", "mana_cost": "{1}{G}{G}",
           "type_line": "Legendary Creature — Elf Scout", "released_at": "2024-02-09"}
ALT = {"id": "2", "name": "Selvala's Charge", "mana_cost": "{3}{G}",
       "type_line": "Instant", "released_at": "2014-06-06"}


def quiet_choose(answer):
    """_choose_card with its menu output captured."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        def prompt(_p):
            return answer() if callable(answer) else answer
        return _choose_card("Selvala", DEFAULT, [ALT], prompt=prompt), out.getvalue()


class ChooseCardTest(unittest.TestCase):
    def test_enter_defaults_to_first(self):
        chosen, menu = quiet_choose("")
        self.assertEqual(chosen["id"], "1")
        self.assertIn("Selvala, Explorer Returned", menu)  # menu lists options
        self.assertIn("Selvala's Charge", menu)

    def test_number_picks_alternate(self):
        chosen, _ = quiet_choose("2")
        self.assertEqual(chosen["id"], "2")

    def test_zero_skips(self):
        chosen, _ = quiet_choose("0")
        self.assertIsNone(chosen)

    def test_garbage_falls_back_to_default(self):
        chosen, _ = quiet_choose("banana")
        self.assertEqual(chosen["id"], "1")

    def test_eof_keeps_default(self):
        chosen, _ = quiet_choose(lambda _p="": (_ for _ in ()).throw(EOFError))
        self.assertEqual(chosen["id"], "1")

FIXTURE = """Does Lier let you play adventure spells from the graveyard?

Can I draw 5 if I play two copies of the five color Karn?

HARD:
How does Selvala work with Panglacial Wurm?
"""


class ParseBenchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bench.txt"
        self.path.write_text(FIXTURE, encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def test_blocks_tags_and_normalization(self):
        items = parse_bench(self.path)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0], (None, "Does Lier let you play adventure spells from the graveyard?"))
        self.assertEqual(items[1][0], None)
        self.assertIn("Karn", items[1][1])
        self.assertEqual(items[2], ("HARD", "How does Selvala work with Panglacial Wurm?"))

    def test_wrapped_lines_joined(self):
        # HARD question is wrapped across lines in the file
        tag, q3 = parse_bench(self.path)[2]
        self.assertEqual(tag, "HARD")
        self.assertNotIn("\n", q3)


class ContextIdsTest(unittest.TestCase):
    def test_dedup_preserves_order(self):
        blocks = [
            "RULE 704.5j [x]\ntext",
            "CARD Lier\ninfo",
            "RULE 704.5j [x]\ntext",
            "OFFICIAL RULINGS for Lier\nbling",
        ]
        self.assertEqual(_context_ids(blocks), ["704.5j", "Lier"])


if __name__ == "__main__":
    unittest.main()
