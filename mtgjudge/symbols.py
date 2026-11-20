"""Card symbol icons: mana symbols, hybrid/phyrexian costs, snow, tap.

Scryfall hosts each symbol as an SVG at
    https://svgs.scryfall.io/card-symbols/{code}.svg
where code is the brace content with the slash removed, keeping
Scryfall's casing (capital letters; numbers bare): {U} -> "U",
{2/U} -> "2U", {W/P} -> "WP", {T} -> "T", {Q} -> "Q", {S} -> "S",
{20} -> "20". The web frontend asks the local server for
/symbol/{code}.svg (img tags cannot send Authorization headers, so the
endpoint is open like /health); the server caches each icon in the
data dir, so after the first fetch the frontend renders fully offline.
"""
import re

from . import net
from .store import DATA_DIR

SYMBOL_DIR = DATA_DIR / "symbols"

SYMBOL_URL = "https://svgs.scryfall.io/card-symbols/{}.svg"

# codes are safe path fragments: lowercase alphanumerics only
_CODE_OK = re.compile(r"[0-9A-Za-z]{1,6}")


def get_symbol(code, offline=False):
    """SVG bytes for a symbol code, cache-first: one Scryfall fetch
    per symbol ever, then served from disk. Raises FileNotFoundError
    for unknown/bad codes (offline or not) and net.NetError when the
    fetch fails, letting the caller 404."""
    if not _CODE_OK.fullmatch(code):
        raise FileNotFoundError(code)
    SYMBOL_DIR.mkdir(parents=True, exist_ok=True)
    path = SYMBOL_DIR / f"{code}.svg"
    if path.exists():
        return path.read_bytes()
    if offline:
        raise FileNotFoundError(code)
    data = net._open(SYMBOL_URL.format(code), timeout=30,
                      accept="image/svg+xml")
    path.write_bytes(data)
    return data
