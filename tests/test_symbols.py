import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mtgjudge import net, symbols


class GetSymbolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "symbols"
        patcher = mock.patch.object(symbols, "SYMBOL_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_bad_code_rejected_without_network(self):
        with self.assertRaises(FileNotFoundError):
            symbols.get_symbol("../etc/passwd")
        with self.assertRaises(FileNotFoundError):
            symbols.get_symbol("wu.svg")  # dots are not symbol codes
        self.assertFalse(self.dir.exists())

    def test_fetchesOnce_then_cache_hits(self):
        calls = []
        with mock.patch.object(net, "_open",
                               side_effect=lambda url, **kw: (
                                   calls.append(url),
                                   b"<svg>icon</svg>")[1]):
            first = symbols.get_symbol("UR")
            second = symbols.get_symbol("UR")
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1, "second call must hit the cache")
        self.assertTrue(calls[0].endswith("/card-symbols/UR.svg"))
        self.assertEqual((self.dir / "UR.svg").read_bytes(),
                         b"<svg>icon</svg>")

    def test_offline_without_cache_misses(self):
        with self.assertRaises(FileNotFoundError):
            symbols.get_symbol("t", offline=True)
        # but a previously fetched symbol serves offline
        (self.dir / "t.svg").write_bytes(b"<svg>tap</svg>")
        self.assertEqual(symbols.get_symbol("t", offline=True),
                         b"<svg>tap</svg>")

    def test_network_failure_propagates(self):
        err = net.NetError("HTTP 404 for symbol")
        with mock.patch.object(net, "_open", side_effect=err), \
             self.assertRaises(net.NetError):
            symbols.get_symbol("zz")


if __name__ == "__main__":
    unittest.main()
