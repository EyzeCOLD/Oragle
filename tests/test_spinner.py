import io
import time
import unittest

from mtgjudge.spinner import Spinner


class SpinnerTest(unittest.TestCase):
    def test_disabled_start_is_noop_but_notes_print(self):
        out = io.StringIO()
        spin = Spinner(stream=out, enabled=False)
        spin.start("asking mistral")
        spin.note("→ search_rules(query='legend rule')")
        spin.stop()
        self.assertIn("→ search_rules(query='legend rule')\n", out.getvalue())
        self.assertEqual(out.getvalue().count("\r"), 0)

    def test_enabled_note_prints_and_resumes(self):
        out = io.StringIO()
        spin = Spinner(stream=out, enabled=True)
        spin.start("asking mistral")
        time.sleep(0.25)  # let a few frames render
        spin.note("→ timed out")
        time.sleep(0.25)
        spin.stop()
        content = out.getvalue()
        self.assertIn("\r", content)          # animated spinner frames
        self.assertIn("→ timed out\n", content)
        self.assertNotIn("None", content)

    def test_note_without_running_start_just_prints(self):
        out = io.StringIO()
        spin = Spinner(stream=out, enabled=True)
        spin.note("plain event")
        self.assertEqual(out.getvalue(), "  plain event\n")


if __name__ == "__main__":
    unittest.main()
