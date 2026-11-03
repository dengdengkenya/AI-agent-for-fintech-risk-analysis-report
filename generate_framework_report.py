"""Compatibility entry point: the same as `python -m loan_analyst report [options]`.

Reads MySQL once (or a saved --snapshot), runs every analysis, lets the local Qwen
analyst investigate with tools, and writes outputs/framework_report/. See README.md.
"""

import sys

from loan_analyst.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["report", *sys.argv[1:]]))
