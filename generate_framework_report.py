"""Generate a Markdown report and evidence JSON through the callable SQL tools.

Synthetic example (no MySQL or Ollama):
    .venv/Scripts/python.exe generate_framework_report.py --demo
Live MySQL (MYSQL_PASSWORD or an interactive password prompt):
    .venv/Scripts/python.exe generate_framework_report.py

Accepts the existing report's CLI options. Outputs default to
outputs/framework_report, relative to this project. Reruns replace the two files.
This runner reuses, and does not modify, score_cutoff_simulation.py.
"""

from dataclasses import fields
from datetime import datetime, timezone
from getpass import getpass
import json
import os
from pathlib import Path
import sys
from time import perf_counter

import score_cutoff_simulation as report
import score_cutoff_sql
from score_cutoff_tools import ConfigurationError, DatabaseConfig, ScoreCutoffTools


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "framework_report"
QUERY_FIELDS = ("cutoffs", "baseline", "start_month", "end_month")
SETTING_FIELDS = (
    "database", "accepted_table", "rejected_table", "cutoffs", "baseline",
    "start_month", "end_month", "max_loss_rate", "min_outcomes",
    "assume_comparable_scores", "rejected_loss_rate", "take_up_rate", "demo",
)


def parse_args(argv=None):
    """Reuse the report's validation, with a separate default output directory."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    # A user's later --output-dir overrides this default in argparse.
    return report.parse_args(["--output-dir", str(DEFAULT_OUTPUT_DIR), *arguments])


def connect_with_password(config):
    """Supply credentials locally; never put them in tool arguments or evidence."""
    password = os.environ.get("MYSQL_PASSWORD")
    if password is None:
        if not sys.stdin.isatty():
            raise ConfigurationError(
                "Set MYSQL_PASSWORD locally or use an interactive terminal for the password prompt; "
                "use --demo for synthetic data"
            )
        password = getpass(f"MySQL password for {config.user}@{config.host}: ")
    return score_cutoff_sql.connect_mysql(config, password)


def tool_result(tools, name, arguments):
    """Unwrap the dispatcher without bypassing its validation or error redaction."""
    response = tools.call_tool(name, arguments)
    if not response["ok"]:
        error = response["error"]
        raise RuntimeError(f"{error['code']}: {error['message']}")
    return response["result"]


def main(argv=None):
    args = parse_args(argv)
    started = perf_counter()
    try:
        config = DatabaseConfig(**{field.name: getattr(args, field.name)
                                   for field in fields(DatabaseConfig)})
        tools = ScoreCutoffTools(config, demo=args.demo, connection_factory=connect_with_password)
        parameters = {key: getattr(args, key) for key in QUERY_FIELDS}

        queries = {}
        if args.print_sql or not args.demo:
            queries = tool_result(tools, "preview_score_cutoff_queries", parameters)["queries"]
        if args.print_sql:
            print(json.dumps(queries, indent=2, allow_nan=False))
            return 0

        if not args.demo:
            print("Reading accepted and rejected aggregates in MySQL; large tables can take minutes...",
                  flush=True)
        # One aggregate call supplies both the report and its saved evidence.
        data = tool_result(tools, "get_score_cutoff_aggregates", parameters)
        results = report.analyze(data["aggregates"], args)
        evidence = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "mode": data["mode"],
            "settings": {key: getattr(args, key) for key in SETTING_FIELDS},
            "aggregates": data["aggregates"],
            "results": results,
            "queries": queries,
            "elapsed_seconds": perf_counter() - started,
        }
        # Complete rendering and serialization before touching existing output files.
        markdown = report.render_report(results, args)
        evidence_json = json.dumps(evidence, indent=2, allow_nan=False)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        report_path = args.output_dir / "report.md"
        evidence_path = args.output_dir / "aggregates.json"
        evidence_path.write_text(evidence_json, encoding="utf-8")
        report_path.write_text(markdown, encoding="utf-8")
        label = "SYNTHETIC DEMO" if args.demo else "Historical MySQL aggregate"
        print(f"{label} report: {report_path}")
        print(f"Aggregate evidence: {evidence_path}")
        print(f"Completed in {perf_counter() - started:.1f}s. "
              "Reruns replace these two output files.")
        return 0
    except KeyboardInterrupt:
        print("Report generation interrupted; no database data was changed", file=sys.stderr)
        return 130
    except Exception as error:
        # Database exceptions normally arrive as sanitized dispatcher errors.
        detail = ("Database operation failed; check access, schema and query timeout"
                  if type(error).__module__.startswith("mysql") else str(error))
        print(f"Report generation failed: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
