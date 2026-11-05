"""Stage complete artifacts, then replace each destination file."""

from pathlib import Path
import tempfile


def write_files(directory, files):
    """Leave existing files alone if staging fails; replace files only after staging.

    Each replacement is atomic, but the group is not a filesystem transaction:
    a failure during replacement can leave a mix of old and new files.
    """
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
