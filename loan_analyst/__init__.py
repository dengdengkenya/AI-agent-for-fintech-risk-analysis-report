"""Historical Lending Club analysis over MySQL aggregate snapshots, with a local tool-calling analyst.

Layers, each usable on its own:
  settings   validated configuration shared by every entry point and config files
  db         MySQL credentials, connections, read-only transactions, table metadata
  cube       one grouped scan per table; cohorts, score bands and snapshot files
  analyses   pluggable deterministic analyses, each exposing model-callable tools
  tools      tool registry: JSON schemas, argument validation, sanitized dispatch
  evidence   citable fact ledger and validation of model statements
  llm        loopback-only Ollama client
  agent      tool-calling analyst loop and structured, evidence-checked answers
  report     composable Markdown report sections
  pipeline   report, ask, chat and render workflows built from replaceable services
  cli        command line: python -m loan_analyst --help
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
