import os
import tempfile
import unittest
from pathlib import Path

from mtgjudge import envfile, llm


class ParseTest(unittest.TestCase):
    def test_basic_forms(self):
        parsed = envfile._parse(
            "# comment\n"
            "A=1\n"
            "B = spaced \n"
            "C=\"quoted value\"\n"
            "D='single'\n"
            "IGNOREME\n"
        )
        self.assertEqual(parsed, {"A": "1", "B": "spaced", "C": "quoted value", "D": "single"})


class LoadTest(unittest.TestCase):
    KEY = "MTGJUDGE_TEST_ENVFILE_KEY"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env_file = Path(self.tmp.name) / "env"
        self.env_file.write_text(f"{self.KEY}=test-key-123\n", encoding="utf-8")
        os.environ["MTG_ENV_FILE"] = str(self.env_file)
        os.environ.pop(self.KEY, None)
        self.addCleanup(os.environ.pop, "MTG_ENV_FILE", None)
        self.addCleanup(os.environ.pop, self.KEY, None)

    def test_loads_and_returns_paths(self):
        loaded = envfile.load_env()
        # Other files (e.g. a real ./.env) may load too; ours must be among them.
        self.assertIn(str(self.env_file), loaded)
        self.assertEqual(os.environ[self.KEY], "test-key-123")

    def test_real_environment_wins(self):
        os.environ[self.KEY] = "from-shell"
        envfile.load_env()
        self.assertEqual(os.environ[self.KEY], "from-shell")


class ContentShapeTest(unittest.TestCase):
    """zai-glm models return content as block lists; plain models as strings."""

    def test_plain_string(self):
        self.assertEqual(llm._content_to_text("  Blue "), "Blue")

    def test_block_list_skips_thinking(self):
        content = [
            {"type": "thinking", "thinking": [{"type": "text", "text": "hmm"}], "closed": True},
            {"type": "text", "text": "Blue"},
        ]
        self.assertEqual(llm._content_to_text(content), "Blue")

    def test_block_list_joins_texts(self):
        content = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        self.assertEqual(llm._content_to_text(content), "a\nb")

    def test_no_text_raises(self):
        with self.assertRaises(llm.LLMError):
            llm._content_to_text([{"type": "thinking", "closed": True}])


if __name__ == "__main__":
    unittest.main()


class OriginTest(unittest.TestCase):
    def test_origin_environment_vs_file_vs_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text("MTG_FAKE_KEY_XYZ=fromfile\n", encoding="utf-8")
            os.environ.pop("MTG_FAKE_KEY_XYZ", None)
            envfile.FILE_ORIGIN.clear()
            old = os.environ.get("MTG_ENV_FILE")
            os.environ["MTG_ENV_FILE"] = str(path)
            try:
                envfile.load_env()
                self.assertEqual(
                    envfile.origin("MTG_FAKE_KEY_XYZ"), str(path))
                self.assertIsNone(envfile.origin("MTG_NOT_SET_ANYWHERE"))
                self.assertEqual(
                    envfile.origin("MTG_ENV_FILE"), "environment")
            finally:
                os.environ.pop("MTG_FAKE_KEY_XYZ", None)
                os.environ.pop("MTG_ENV_FILE", None)
                if old:
                    os.environ["MTG_ENV_FILE"] = old
