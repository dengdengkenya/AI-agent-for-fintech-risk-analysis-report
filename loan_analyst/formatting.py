"""Number, Markdown and terminal formatting shared by facts, reports and the CLI."""

import re
import textwrap


def pct(value, digits=2):
    return "N/A" if value is None else f"{value:.{digits}%}"


def usd(value):
    return "N/A" if value is None else f"${value:,.0f}"


def number(value):
    """Integers with separators; fractional expectations with one decimal."""
    if value is None:
        return "N/A"
    return f"{value:,}" if isinstance(value, int) else f"{value:,.1f}"


def signed(value):
    if value is None:
        return "N/A"
    return f"{value:+,}" if isinstance(value, int) else f"{value:+,.1f}"


# Underscores inside words (score_band) never start emphasis, so only edge underscores are escaped.
_MARKDOWN = re.compile(r"[\\`*\[\]<>|#]|(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])")


def plain(text):
    """Collapse whitespace and escape Markdown/HTML so supplied text renders literally."""
    return _MARKDOWN.sub(lambda match: "\\" + match.group(0), " ".join(str(text).split()))


def table(headers, rows, align=None):
    """A Markdown table; columns are right-aligned unless an alignment row is given."""
    align = align or ["---:"] * len(headers)
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(align) + " |"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def wrap(text, indent="", width=100):
    """Wrap terminal text, keeping the first line's prefix and indenting the rest."""
    return textwrap.fill(" ".join(str(text).split()), width=width, initial_indent=indent,
                         subsequent_indent=" " * len(indent))
