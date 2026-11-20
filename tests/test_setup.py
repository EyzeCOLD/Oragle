"""`oragle setup`: builds rules + cards in one run, checks the provider,
and guides instead of crashing when pieces are missing."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mtgjudge import cli, cr, store


class SetupCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _run(self, env=None):
        out = io.StringIO()
        env = dict(env or {})
        env.setdefault("MISTRAL_API_KEY", "")
        env.setdefault("MTG_API_BASE", "")
        with mock.patch.object(store, "DATA_DIR", self.tmp), \
             mock.patch.dict(os.environ, env, clear=True), \
             contextlib.redirect_stdout(out), \
             mock.patch.object(cli.carddb, "bulk_entry",
                              return_value={"updated_at": "2026-11-20",
                                            "compressed_size": 1e6}), \
             mock.patch.object(cli.carddb, "build_from_bulk",
                              return_value=3):
            code = cli.cmd_setup(type("A", (), {"backend": None})())
        return code, out.getvalue()

    def test_full_setup_with_provider(self):
        # rules parsing + provider resolution mocked; data flow is real
        with mock.patch.object(cr, "download_cr", return_value=("u", "t")), \
             mock.patch.object(cr, "parse_cr",
                               return_value=({"effective_date": "X"},
                                             [{"number": "1", "text": "t",
                                               "chapter": "", "section": "",
                                               "num": 1}])), \
             mock.patch.object(store, "save_rules",
                               side_effect=lambda m, c: self.tmp / "r.json"), \
             mock.patch.object(store, "stats",
                               return_value={"rules": 1, "chapters": 1,
                                             "biggest_chapter": "C",
                                             "biggest_chapter_rules": 1}):
            code, out = self._run(env={"MISTRAL_API_KEY": "k"})
        self.assertEqual(code, 0)
        self.assertIn("[1/3]", out)
        self.assertIn("[3/3]", out)
        self.assertIn("Done. Try it", out)
        self.assertIn("oragle ask", out)

    def test_no_provider_guides_not_crashes(self):
        with mock.patch.object(cr, "download_cr", return_value=("u", "t")), \
             mock.patch.object(cr, "parse_cr",
                               return_value=({"effective_date": "X"}, [])), \
             mock.patch.object(store, "save_rules",
                               side_effect=lambda m, c: self.tmp / "r.json"), \
             mock.patch.object(store, "stats",
                               return_value={"rules": 1, "chapters": 1,
                                             "biggest_chapter": "C",
                                             "biggest_chapter_rules": 1}):
            code, out = self._run(env={"MISTRAL_API_KEY": "",
                                      "MTG_API_BASE": ""})
        self.assertEqual(code, 1)
        self.assertIn("cp .env.example .env", out)
        # data was still built before the provider gate
        self.assertIn("[2/3]", out)


if __name__ == "__main__":
    unittest.main()
