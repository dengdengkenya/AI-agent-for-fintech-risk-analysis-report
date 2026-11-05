"""Historical Lending Club analysis over MySQL aggregate snapshots.

The package is deliberately arranged as one obvious path, with a few larger,
cohesive files instead of many tiny interconnected ones:

  1. cli.py           parse the command line and choose a workflow
  2. settings.py      validate the user's choices
  3. workflow.py      report / ask / chat / render, top to bottom
  4. snapshot.py      the grouped SQL, Cube and Snapshot data structures
  5. database.py      MySQL connection and read-only transaction
  6. calculations.py  every number in the report, plus the model's tools
  7. tools.py         tool schemas, argument validation and dispatch
  8. analyst.py       fact ledger, answer checks and the model loop
  9. prompts.py       the text sent to the model
 10. ollama.py        the local model HTTP client
 11. report.py        every human-readable table and section
 12. support.py       small shared helpers (JSON, formatting, file writing)

A junior reader can start at workflow.py and follow the numbered comments in
``run_report``. The developer guide in docs/ walks through one report in detail.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
