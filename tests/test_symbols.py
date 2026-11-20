import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mtgjudge import net, symbols


class BakedSetTest(unittest.TestCase):
    def test_standard_symbols_serve_without_network(self):
        # the baked set means the deploy never fetches common icons
        for code in ("T", "WU", "WP", "2U", "20", "S", "C", "X"):
            svg = symbols.get_symbol(code, offline=True)
            self.assertTrue(svg.startswith(b"<svg"), code)

    def test_baked_code_beats_cache(self):
        # a baked icon is served from the package, not the data dir,
        # even when a (never-happening) stale file sits in the cache
        with tempfile.TemporaryDirectory() as tmp:
            cached = Path(tmp) / "T.svg"
            cached.write_bytes(b"stale")
            with mock.patch.object(symbols, "SYMBOL_DIR", Path(tmp)):
                svg = symbols.get_symbol("T", offline=True)
            self.assertTrue(svg.startswith(b"<svg"))


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
            symbols.get_symbol("WU.svg")  # dots are not symbol codes
        self.assertFalse(self.dir.exists())

    def test_unbaked_fetchesOnce_then_cache_hits(self):
        calls = []

        def fake_open(url, **kw):
            calls.append(url)
            return b"<svg>icon</svg>"

        # a code that exists nowhere: bake, cache, network
        with mock.patch.object(symbols, "BAKED_DIR", self.dir / "none"), \
             mock.patch.object(net, "_open", side_effect=fake_open):
            first = symbols.get_symbol("ZZ")
            second = symbols.get_symbol("ZZ")
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1, "second call must hit the cache")
        self.assertTrue(calls[0].endswith("/card-symbols/ZZ.svg"))
        self.assertEqual((self.dir / "ZZ.svg").read_bytes(),
                         b"<svg>icon</svg>")

    def test_offline_without_cache_misses(self):
        with mock.patch.object(symbols, "BAKED_DIR", self.dir / "none"), \
             self.assertRaises(FileNotFoundError):
            symbols.get_symbol("ZZ", offline=True)

    def test_network_failure_propagates(self):
        err = net.NetError("HTTP 404 for symbol")
        with mock.patch.object(symbols, "BAKED_DIR", self.dir / "none"), \
             mock.patch.object(net, "_open", side_effect=err), \
             self.assertRaises(net.NetError):
            symbols.get_symbol("ZZ")


if __name__ == "__main__":
    unittest.main()
