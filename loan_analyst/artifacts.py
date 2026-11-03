"""Atomic file writes: complete artifacts replace old ones together, or not at all."""

from pathlib import Path
import tempfile


def write_files(directory, files):
    """Stage every file before replacing any; a failure leaves earlier outputs untouched."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        for filename, contents in files.items():
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, newline="\n",
                                             prefix=".artifact-", suffix=".tmp", delete=False) as stream:
                staged.append((Path(stream.name), directory / filename))
                stream.write(contents)
        for temporary, destination in staged:
            temporary.replace(destination)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
