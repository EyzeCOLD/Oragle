import io
import os
import unittest
from unittest import mock

from mtgjudge import ansi, cli


class AnsiTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("FORCE_COLOR", None)
        os.environ.pop("NO_COLOR", None)

    def test_disabled_passthrough(self):
        stream = io.StringIO()  # isatty False -> disabled
        text = "**bold** and rule 704.5j"
        self.assertEqual(ansi.render(text, stream=stream), text)

    def test_no_color_forces_off(self):
        os.environ["NO_COLOR"] = "1"
        os.environ["FORCE_COLOR"] = ""
        stream = mock.Mock()
        stream.isatty.return_value = True
        self.assertFalse(ansi.enabled(stream))
        self.assertEqual(ansi.render("**b**", stream=stream), "**b**")

    def test_force_color_enables(self):
        os.environ["FORCE_COLOR"] = "1"
        out = ansi.render("**bold** and (rule 704.5j) `dyk`", stream=io.StringIO())
        self.assertIn(ansi.BOLD, out)
        self.assertIn(ansi.CYAN, out)  # the rule number
        self.assertIn(ansi.RESET, out)
        self.assertNotIn("**", out)

    def test_headings_bullets_rules(self):
        os.environ["FORCE_COLOR"] = "1"
        out = ansi.render("### Title\n- item with 205.4d\n---\ntext",
                          stream=io.StringIO())
        lines = out.split("\n")
        self.assertIn("TITLE", lines[0])  # h3+ uppercased
        self.assertIn("• ", lines[1])
        self.assertIn("─", lines[2])
        out = ansi.render("# Title", stream=io.StringIO())
        self.assertIn("Title", out)  # h1 keeps its case

    def test_mana_cost_not_treated_as_rule_number(self):
        os.environ["FORCE_COLOR"] = "1"
        out = ansi.render("costs {2} and 2.5 dollars", stream=io.StringIO())
        self.assertNotIn(ansi.CYAN, out)

    def test_single_star_italics_render(self):
        # GLM writes *italics* constantly (bench2-last.md: *not*,
        # *Opalescence*, *Short answer:*) — these must not leak asterisks
        os.environ["FORCE_COLOR"] = "1"
        out = ansi.render("this is *not* the same", stream=io.StringIO())
        self.assertIn(ansi.ITALIC, out)
        self.assertNotIn("*", out)

    def test_bold_italic_triple_star(self):
        os.environ["FORCE_COLOR"] = "1"
        out = ansi.render("***both***", stream=io.StringIO())
        self.assertIn(ansi.BOLD + ansi.ITALIC, out)
        self.assertNotIn("*", out)

    def test_card_names_highlighted(self):
        os.environ["FORCE_COLOR"] = "1"
        out = ansi.render("Opalescence loses to Blood Moon per 205.4d",
                          card_names=["Opalescence", "Blood Moon"],
                          stream=io.StringIO())
        self.assertIn(ansi.MAGENTA, out)
        self.assertIn(ansi.CYAN, out)  # the rule number

    def test_card_inside_italics_keeps_italic_around(self):
        # the highlight must not reset the surrounding italics mid-run
        os.environ["FORCE_COLOR"] = "1"
        out = ansi.render("*make sure Opalescence counts*",
                          card_names=["Opalescence"], stream=io.StringIO())
        self.assertIn(ansi.ITALIC + ansi.MAGENTA, out)
        segments = [s for s in out.split(ansi.RESET) if "counts" in s]
        self.assertTrue(any(s.startswith(ansi.ITALIC) for s in segments),
                        out)

    def test_code_span_not_recolored_inside(self):
        os.environ["FORCE_COLOR"] = "1"
        out = ansi.render("`see 704.5j` plain", card_names=["Squee"],
                          stream=io.StringIO())
        first = out.split(ansi.RESET)[0]
        self.assertIn("see 704.5j", first)
        self.assertNotIn(ansi.CYAN, first)

    def test_disabled_ignores_card_names(self):
        out = ansi.render("*Opalescence*", card_names=["Opalescence"],
                          stream=io.StringIO())
        self.assertEqual(out, "*Opalescence*")


class BareExitTest(unittest.TestCase):
    def test_exit_and_quit_bare(self):
        for line in ("quit", "exit", "QUIT", "Exit", "quit?", "exit!"):
            self.assertTrue(cli.bare_exit(line), line)

    def test_anything_else_goes_to_the_model(self):
        for line in (
            "quit please", "how do I quit", "exit the battlefield",
            "when a creature leaves the battlefield, does it die?",
            "quitter", "exits", "Does Humility remove flying?",
        ):
            self.assertFalse(cli.bare_exit(line), line)


class ReplCompletionTest(unittest.TestCase):
    def test_first_word_commands(self):
        self.assertEqual(cli.complete_repl_line(":q", ":q"), [":quit", ":q"])
        self.assertEqual(cli.complete_repl_line(":ru", ":ru"), [":rule"])
        self.assertEqual(cli.complete_repl_line("qui", "qui"), ["quit"])
        self.assertIn(":help", cli.complete_repl_line(":", ":"))

    def test_no_completion_after_space(self):
        self.assertEqual(cli.complete_repl_line(":rule 7", "7"), [])

    def test_bogus_prefix_empty(self):
        self.assertEqual(cli.complete_repl_line(":z", ":z"), [])


if __name__ == "__main__":
    unittest.main()


class ReadlineSafeTest(unittest.TestCase):
    def test_ansi_codes_marked_nonprinting(self):
        out = ansi.readline_safe("\x1b[1moragle> \x1b[0m")
        self.assertEqual(out, "\001\x1b[1m\002oragle> \001\x1b[0m\002")

    def test_plain_text_untouched(self):
        self.assertEqual(ansi.readline_safe("oragle> "), "oragle> ")
