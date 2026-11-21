import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mtgjudge.cli import (_answer_similarity, _choose_card, _context_ids,
                          parse_bench)


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


class AnswerSimilarityTest(unittest.TestCase):
    def test_identical_answers(self):
        self.assertEqual(_answer_similarity(["yes", "yes"]), 1.0)

    def test_disjoint_answers(self):
        self.assertEqual(_answer_similarity(["yes", "no"]), 0.0)

    def test_partial_overlap_between_0_and_1(self):
        sim = _answer_similarity(["the legend rule applies", "the legend rule wins"])
        self.assertGreater(sim, 0.0)
        self.assertLess(sim, 1.0)

    def test_fewer_than_two_is_1(self):
        self.assertEqual(_answer_similarity(["only one"]), 1.0)

    def test_empty_answers_do_not_divide_by_zero(self):
        self.assertEqual(_answer_similarity(["", ""]), 1.0)


class RepeatBenchTest(unittest.TestCase):
    """cmd_bench --repeat: N runs per question, a consistency line, and a
    grouped report — all with the pipeline monkeypatched offline."""

    def setUp(self):
        from mtgjudge import cli
        self.cli = cli
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bench = Path(self.tmp.name) / "bench.txt"
        self.bench.write_text("What is rebound?\n", encoding="utf-8")
        self.report = Path(self.tmp.name) / "report.md"
        self.calls = []

        def fake_answer(question, top=8, offline=False, backend=None, model=None,
                        **_):
            self.calls.append(question)
            # alternate branches so consistency metrics have data
            n = len(self.calls)
            return {
                "answer": "rebound lets you cast it next upkeep" if n % 2 else
                          "rebound exiles the spell and recasts it",
                "backend": "fake", "model": "fake",
                "meta": {"effective_date": "November 6, 2026"},
                "blocks": [], "context_ids": ["702.87a"] if n % 2 else ["702.87"],
                "elapsed": 1.0 + n, "stats": {"rounds": 1},
            }

        self._patcher = mock.patch.object(cli, "answer_question", fake_answer)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _run(self, repeat):
        import contextlib
        args = SimpleNamespace(file=str(self.bench), only=None, repeat=repeat,
                               top=8, report=str(self.report), backend=None,
                               model=None, offline=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.cli.cmd_bench(args)
        return rc, out.getvalue()

    def test_repeat_runs_each_question_n_times(self):
        rc, output = self._run(3)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls), 3)
        self.assertIn("3 run(s) each", output)
        self.assertIn("run 1/3", output)
        self.assertIn("Consistency per question", output)
        self.assertIn("same context in 2/3", output)

    def test_repeat_report_groups_runs(self):
        _, _output = self._run(2)
        text = self.report.read_text(encoding="utf-8")
        self.assertIn("**Consistency:**", text)
        self.assertIn("### run 1", text)
        self.assertIn("### run 2", text)
        self.assertEqual(text.count("---\n"), 1)  # one section for the one question

    def test_single_repeat_keeps_legacy_report_shape(self):
        self._run(1)
        text = self.report.read_text(encoding="utf-8")
        self.assertNotIn("### run", text)
        self.assertNotIn("**Consistency:**", text)


class DecklistBenchBlockTest(unittest.TestCase):
    def test_decklist_lines_survive_parsing(self):
        """A bench block with a multi-line decklist keeps line structure,
        so the ask pipeline's decklist detector sees the counted lines."""
        from mtgjudge import deck
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "bench.txt"
        path.write_text(
            "Which cards in this decklist combo?\n"
            "4 Kiki-Jiki, Mirror Breaker\n"
            "4 Pestermite\n"
            "2 Panharmonicon\n",
            encoding="utf-8")
        tag, question = parse_bench(path)[0]
        self.assertIsNone(tag)
        self.assertIn("\n", question)
        entries, rest = deck.extract_deck(question)
        self.assertEqual(len(entries), 3)
        self.assertIn("combo", rest)
        self.assertNotIn("Kiki", rest)


if __name__ == "__main__":
    unittest.main()
