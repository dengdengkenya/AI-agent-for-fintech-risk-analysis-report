"""Small helpers shared by several parts of the program.

Nothing in this file knows about loans. It contains the everyday plumbing:
JSON parsing and hashing, number formatting, Markdown tables/escaping, and
writing output files safely. Keeping it in one place means a reader does not
have to jump between three tiny modules to find a helper.
"""

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re
import tempfile
import textwrap

from . import PROJECT_ROOT


# ---------------------------------------------------------------------------
# JSON and hashing
# ---------------------------------------------------------------------------

def _reject_duplicate_keys(pairs):
    """Turn duplicate JSON keys into an error instead of silently keeping one."""
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _reject_nonfinite_number(_value):
    raise ValueError("Nonfinite JSON number")


def read_json(text):
    """Parse JSON with unique keys and finite numbers only."""
    return json.loads(text, object_pairs_hook=_reject_duplicate_keys,
                      parse_constant=_reject_nonfinite_number)


def canonical(value):
    """Compact, sorted JSON used when hashing or comparing two values."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def sha256_json(value):
    """A stable fingerprint of a JSON-safe value."""
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Display formatting
# ---------------------------------------------------------------------------

def pct(value, digits=2):
    """Format a fraction (0.2) as a percentage (20.00%). None means unavailable."""
    return "N/A" if value is None else f"{value:.{digits}%}"


def usd(value):
    """Format a dollar amount with thousands separators."""
    return "N/A" if value is None else f"${value:,.0f}"


def number(value):
    """Format a count as an integer, or a fractional expectation with one decimal."""
    if value is None:
        return "N/A"
    return f"{value:,}" if isinstance(value, int) else f"{value:,.1f}"


def signed(value):
    """Like number(), but always show a leading + or -."""
    if value is None:
        return "N/A"
    return f"{value:+,}" if isinstance(value, int) else f"{value:+,.1f}"


# Underscores inside a word (score_band) must not become Markdown emphasis, so
# only underscores at the edges of a word are escaped.
_MARKDOWN = re.compile(r"[\\`*\[\]<>|#]|(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])")


def plain(text):
    """Flatten whitespace and escape Markdown/HTML so supplied text is shown literally."""
    return _MARKDOWN.sub(lambda match: "\\" + match.group(0), " ".join(str(text).split()))


def table(headers, rows, align=None):
    """Render a Markdown table. Columns are right-aligned unless align is supplied."""
    align = align or ["---:"] * len(headers)
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(align) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def wrap(text, indent="", width=100):
    """Wrap terminal text, keeping the first-line prefix and indenting later lines."""
    return textwrap.fill(" ".join(str(text).split()), width=width, initial_indent=indent,
                         subsequent_indent=" " * len(indent))


# ---------------------------------------------------------------------------
# Time and files
# ---------------------------------------------------------------------------

def utc_now():
    """Current UTC time as a sortable string. A test can replace this clock."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_files(directory, files):
    """Write several files, replacing the old ones only after all are staged.

    Each replacement is atomic, but the group is not a filesystem transaction:
    a failure during replacement can leave a mix of old and new files.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        for filename, contents in files.items():
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                             newline="\n", prefix=".artifact-", suffix=".tmp",
                                             delete=False) as stream:
                staged.append((Path(stream.name), directory / filename))
                stream.write(contents)
        for temporary, destination in staged:
            temporary.replace(destination)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def display_path(path):
    """Show a project-relative path when possible, so reports carry no home folders."""
    path = Path(path).resolve()
    if path.is_relative_to(PROJECT_ROOT):
        return path.relative_to(PROJECT_ROOT).as_posix()
    return path.name
