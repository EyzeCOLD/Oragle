"""Card symbol icons: mana symbols, hybrid/phyrexian costs, snow, tap.

Scryfall hosts each symbol as an SVG at
    https://svgs.scryfall.io/card-symbols/{code}.svg
where code is the brace content with the slash removed, keeping
Scryfall's casing (capital letters; numbers bare): {U} -> "U",
{2/U} -> "2U", {W/P} -> "WP", {T} -> "T", {Q} -> "Q", {S} -> "S",
{20} -> "20".

Lookup order: the icon set baked into the package (see
mtgjudge/web/symbols/, downloaded from the Scryfall symbology API;
per the Scryfall/WotC Fan Content Policy usage guidelines the app
credits Scryfall), then a per-data-dir cache (fetch fallback for
anything newer), then one live fetch. After a deploy the standard
set never touches the network. The web frontend asks the local
server for /symbol/{code}.svg (img tags cannot send Authorization
headers, so the endpoint is open like /health).
"""
import re
from pathlib import Path

from . import net
from .store import DATA_DIR

# icons baked into the package at build time
BAKED_DIR = Path(__file__).parent / "web" / "symbols"
SYMBOL_DIR = DATA_DIR / "symbols"  # fetched-on-demand cache

SYMBOL_URL = "https://svgs.scryfall.io/card-symbols/{}.svg"

# codes are safe path fragments: alphanumerics only
_CODE_OK = re.compile(r"[0-9A-Za-z]{1,6}")


def get_symbol(code, offline=False):
    """SVG bytes for a symbol code. Baked set first (no network), then
    the data-dir cache, then one Scryfall fetch which lands in the
    cache. Raises FileNotFoundError for unknown/bad codes (the caller
    404s and the frontend falls back to the literal {text})."""
    if not _CODE_OK.fullmatch(code):
        raise FileNotFoundError(code)
    baked = BAKED_DIR / f"{code}.svg"
    if baked.exists():
        return baked.read_bytes()
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
