"""ANSI styling for terminal output.

Colors are enabled when the stream is a TTY; NO_COLOR disables, FORCE_COLOR
forces (https://no-color.org/ conventions). All helpers are no-ops otherwise,
so the same format strings work piped, in files, and in tests.

`render` formats markdown-ish LLM output: emphasis, inline code, rule
numbers, and card-name highlights. Span tokens are parsed in one pass so
nested highlights (a card name inside *italics*) combine their styles
instead of resetting each other mid-word.
"""

import os
import re
import sys

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
ITALIC = "\x1b[3m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
BLUE = "\x1b[34m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"
GRAY = "\x1b[90m"
BRIGHT_CYAN = "\x1b[96m"

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
BULLET_RE = re.compile(r"^(\s*)[-*]\s+(.*)$")
HR_RE = re.compile(r"^\s*-{3,}\s*$")
RULE_NUM_RE = re.compile(r"(?<![\w.])(\d{3}\.\d{1,2}[a-z]{0,3})(?![\d])")
# order matters: ***bold italic*** before **bold** before *italic*
INLINE_RE = re.compile(
    r"(\*\*\*[^*]+?\*\*\*|\*\*[^*]+?\*\*|\*[^*\n]+?\*|`[^`\n]+`)")


def enabled(stream=None):
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    stream = stream or sys.stdout
    return hasattr(stream, "isatty") and stream.isatty() \
        and os.environ.get("TERM") != "dumb"


def styled(text, *codes, stream=None):
    if not codes or not enabled(stream):
        return text
    return "".join(codes) + text + RESET


def _leaf(text, attrs, stream, sub_re):
    """Plain text at the bottom of the token tree: card names (sub_re,
    magenta) and rule numbers (cyan) get their style merged with attrs."""
    out, pos = [], 0
    for m in (sub_re.finditer(text) if sub_re else []):
        out.append(styled(text[pos:m.start()], *attrs, stream=stream))
        is_card = "card" in m.re.groupindex and m.group("card") is not None
        out.append(styled(m.group(0), *attrs,
                           MAGENTA if is_card else CYAN, stream=stream))
        pos = m.end()
    out.append(styled(text[pos:], *attrs, stream=stream))
    return "".join(out)


def _inline(text, attrs, stream, sub_re):
    out, pos = [], 0
    for m in INLINE_RE.finditer(text):
        out.append(_leaf(text[pos:m.start()], attrs, stream, sub_re))
        token = m.group(0)
        if token.startswith("`"):
            # code spans are dimmed as-is, no recoloring inside
            out.append(styled(token[1:-1], *attrs, DIM, stream=stream))
        elif token.startswith("***"):
            out.append(_inline(token[3:-3], (*attrs, BOLD, ITALIC),
                               stream, sub_re))
        elif token.startswith("**"):
            out.append(_inline(token[2:-2], (*attrs, BOLD), stream, sub_re))
        else:
            out.append(_inline(token[1:-1], (*attrs, ITALIC), stream, sub_re))
        pos = m.end()
    out.append(_leaf(text[pos:], attrs, stream, sub_re))
    return "".join(out)


def _card_sub_re(card_names, stream):
    """Merged highlight pattern for _leaf: referenced card names
    (longest first, magenta) plus rule numbers (cyan)."""
    if not enabled(stream):
        return None
    names = sorted({n.strip() for n in card_names if n and n.strip()},
                   key=len, reverse=True)
    card_alt = ("(?P<card>" + "|".join(re.escape(n) for n in names) + ")|"
                if names else "")
    return re.compile(
        card_alt + "(?P<rule>" + RULE_NUM_RE.pattern + ")", re.IGNORECASE)


def render(text, stream=None, card_names=()):
    """Style markdown-ish LLM output for a terminal: emphasis, `code`,
    rule numbers, card-name highlights, headings, bullets, rules."""
    if not enabled(stream):
        return text
    sub_re = _card_sub_re(card_names, stream)
    out = []
    for raw in text.split("\n"):
        line = raw
        m = HEADING_RE.match(line)
        if m:
            n = len(m.group(1))
            title = m.group(2).strip().rstrip("#").strip()
            out.append(styled(title.upper() if n > 2 else title,
                              BOLD, BRIGHT_CYAN, stream=stream))
            continue
        if HR_RE.match(line):
            out.append(styled("─" * 58, DIM, stream=stream))
            continue
        m = BULLET_RE.match(line)
        if m and not line.lstrip().startswith("-" * 3):
            out.append(m.group(1) + styled("• ", CYAN, stream=stream)
                       + _inline(m.group(2), (), stream, sub_re))
            continue
        out.append(_inline(line, (), stream, sub_re))
    return "\n".join(out)


ANSI_CODES_RE = re.compile(r"\x1b\[[0-9;]*m")


def readline_safe(text):
    """Wrap ANSI codes in readline's non-printing markers (\001/\002) so
    input() + readline computes the correct prompt width. Without this,
    a styled prompt breaks readline's column math and long input lines
    wrap over themselves at the terminal edge."""
    return ANSI_CODES_RE.sub(lambda m: "\001" + m.group(0) + "\002", text)


def rule_header(number, heading, stream=None):
    """`RULE number — heading` line for rule lookups."""
    return styled(f"RULE {number}", BOLD, CYAN, stream=stream) + \
        styled(f" — {heading}", DIM, stream=stream)


def card_name(name, stream=None):
    return styled(name, BOLD, stream=stream)
