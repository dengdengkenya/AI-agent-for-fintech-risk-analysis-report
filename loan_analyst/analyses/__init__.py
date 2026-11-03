"""Analysis plug-ins. Each module exposes TOOLS plus pure analyze/facts/section functions.

To add an analysis: write a module whose TOOLS holds tools.Tool objects (schema plus a
handler returning ToolOutput), then list it in MODULES. It becomes available to the
analyst, the `tools` and `call` commands, and (with a renderer in report.py) to reports.
"""

from ..tools import ToolRegistry
from . import cutoffs, overview, rejected, segments
from .definitions import DEFINITIONS

MODULES = (overview, cutoffs, segments, rejected)


def build_registry(modules=MODULES):
    return ToolRegistry(tool for module in modules for tool in module.TOOLS)


__all__ = ["DEFINITIONS", "MODULES", "build_registry"]
