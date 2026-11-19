"""Minimal .env loading. Files are plain KEY=VALUE lines; '#' comments and
optional single/double quotes are tolerated. Values from the real
environment always win. Loaded from $MTG_ENV_FILE, then ./.env, then the
.env next to the installed package (works for `oragle` run from anywhere),
then $MTG_DATA_DIR/.env.
"""

import os
from pathlib import Path


def _parse(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value
    return out


def _repo_env():
    """The .env in the project checkout this package is installed from
    (pyproject dir for an editable/uv tool install, e.g. oragle)."""
    return Path(__file__).resolve().parent.parent / ".env"


def _candidates():
    yield os.environ.get("MTG_ENV_FILE")
    yield "./.env"
    yield _repo_env()
    yield Path.home() / ".local/share/mtg-rules-judge/.env"


FILE_ORIGIN = {}  # key -> path it came from (config command reporting)


def load_env():
    """Populate os.environ from .env files. Returns the paths loaded."""
    loaded = []
    for candidate in _candidates():
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if not path.is_file():
            continue
        for key, value in _parse(path.read_text(encoding="utf-8")).items():
            if key not in os.environ:
                os.environ[key] = value
                FILE_ORIGIN.setdefault(key, str(path))
        loaded.append(str(path))
    return loaded


def origin(key):
    """Where a setting came from: 'environment', the file path, or None
    when the value is a built-in default."""
    if key in os.environ:
        return FILE_ORIGIN.get(key, "environment")
    return None
