"""Generate a real MySQL report with local Ollama/Qwen analysis.

Credentials come from MYSQL_PASSWORD or a local terminal prompt.
There is no sample or synthetic fallback.
"""

import argparse
from dataclasses import fields
from datetime import datetime, timezone
from getpass import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from time import perf_counter
from uuid import uuid4

import ollama_analysis
import score_cutoff_simulation as report
import score_cutoff_sql
from score_cutoff_tools import ConfigurationError, DatabaseConfig, ScoreCutoffTools

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "framework_report"
QUERY_FIELDS = ("cutoffs", "baseline", "start_month", "end_month")
SETTING_FIELDS = (
    "database", "accepted_table", "rejected_table", "cutoffs", "baseline",
    "start_month", "end_month", "max_loss_rate", "min_outcomes",
    "assume_comparable_scores", "rejected_loss_rate", "take_up_rate",
)


def positive_int(value):
    try:
        number = int(value)
        if number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("Use a positive integer")


def configure_parser(parser):
    parser.add_argument("--ollama-url", type=ollama_analysis.local_url, default="http://localhost:11434",
                        help="Local loopback Ollama endpoint")
    parser.add_argument("--ollama-model", default="qwen3:4b", help="Installed local model tag")
    parser.add_argument("--ollama-timeout", type=positive_int, default=300,
                        help="Maximum seconds per analysis request; at most one validation retry")
    parser.add_argument("--no-ai", action="store_true",
                        help="Explicitly generate only the deterministic MySQL report")


def parse_args(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    return report.parse_args(["--output-dir", str(DEFAULT_OUTPUT_DIR), *arguments],
                             configure_parser=configure_parser)


def connect_with_password(config):
    """Supply credentials locally; never put them in tool arguments or evidence."""
    password = os.environ.get("MYSQL_PASSWORD")
    if password is None:
        if not sys.stdin.isatty():
            raise ConfigurationError(
                "Set MYSQL_PASSWORD locally or use an interactive terminal for the password prompt"
            )
        password = getpass(f"MySQL password for {config.user}@{config.host}: ")
    return score_cutoff_sql.connect_mysql(config, password)


def tool_result(tools, name, arguments):
    response = tools.call_tool(name, arguments)
    if not response["ok"]:
        error = response["error"]
        raise RuntimeError(f"{error['code']}: {error['message']}")
    return response["result"]


def write_outputs(directory, files):
    """Stage complete artifacts before replacement; failed analysis never touches them."""
    directory.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        for filename, contents in files.items():
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                             prefix=".report-", suffix=".tmp", delete=False) as stream:
                staged.append((Path(stream.name), directory / filename))
                stream.write(contents)
        for temporary, destination in staged:
            temporary.replace(destination)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    args = parse_args(argv)
    started = perf_counter()
    try:
        config = DatabaseConfig(**{field.name: getattr(args, field.name) for field in fields(DatabaseConfig)})
        tools = ScoreCutoffTools(config, connection_factory=connect_with_password)
        parameters = {key: getattr(args, key) for key in QUERY_FIELDS}
        queries = tool_result(tools, "preview_score_cutoff_queries", parameters)["queries"]
        if args.print_sql:
            print(json.dumps(queries, indent=2, allow_nan=False))
            return 0

        model_metadata = None
        if not args.no_ai:
            print(f"Checking local Ollama model {args.ollama_model}...", flush=True)
            model_metadata = ollama_analysis.preflight(args)
        print(f"Reading {config.database}.{config.accepted_table} and {config.database}.{config.rejected_table} "
              "in one read-only MySQL snapshot; large tables can take minutes...", flush=True)
        snapshot_started_at = datetime.now(timezone.utc).isoformat()
        data = tool_result(tools, "get_score_cutoff_aggregates", parameters)
        if data.get("mode") != "historical_mysql":
            raise RuntimeError("Report requires live MySQL aggregates")
        snapshot_completed_at = datetime.now(timezone.utc).isoformat()
        results = report.analyze(data["aggregates"], args)
        source = {
            "mode": "historical_mysql", "settings": {key: getattr(args, key) for key in SETTING_FIELDS},
            "aggregates": data["aggregates"], "results": results, "queries": queries,
        }
        evidence_hash = hashlib.sha256(json.dumps(source, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()
        run_id = str(uuid4())
        if args.no_ai:
            analysis = {"status": "disabled", "reason": "Explicit --no-ai option"}
        else:
            print("Generating local Qwen interpretation of the calculated MySQL results...", flush=True)
            analysis = ollama_analysis.generate_analysis(results, args, model_metadata)
        analysis.update({"run_id": run_id, "source_evidence_sha256": evidence_hash})
        evidence = {
            **source, "run_id": run_id, "source_evidence_sha256": evidence_hash,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "snapshot_started_at_utc": snapshot_started_at, "snapshot_completed_at_utc": snapshot_completed_at,
            "ai_analysis": {key: analysis[key] for key in ("status", "run_id", "source_evidence_sha256")},
            "elapsed_seconds": perf_counter() - started,
        }
        markdown = report.render_report(results, args)
        marker = "## Accepted loans: historical cutoff replay"
        markdown = markdown.replace(marker, ollama_analysis.render_analysis(analysis) + marker, 1)
        markdown += (f"\nRun ID: `{run_id}`. Database snapshot read: {snapshot_started_at} to "
                     f"{snapshot_completed_at}. This is the read time, not the repayment performance cutoff.\n\n"
                     "`ollama_analysis.json` retains the local model request, cited facts, validated response, "
                     "model digest and source evidence hash, or an explicit disabled status.\n")
        write_outputs(args.output_dir, {
            "aggregates.json": json.dumps(evidence, indent=2, allow_nan=False),
            "ollama_analysis.json": json.dumps(analysis, indent=2, ensure_ascii=False, allow_nan=False),
            "report.md": markdown,
        })
        print(f"MySQL report: {args.output_dir / 'report.md'}")
        print(f"Aggregate evidence: {args.output_dir / 'aggregates.json'}")
        print(f"Local model evidence: {args.output_dir / 'ollama_analysis.json'}")
        print(f"Completed in {perf_counter() - started:.1f}s. Reruns replace these three output files.")
        return 0
    except KeyboardInterrupt:
        print("Report generation interrupted; no database data was changed", file=sys.stderr)
        return 130
    except Exception as error:
        detail = ("Database operation failed; check access, schema and query timeout"
                  if type(error).__module__.startswith("mysql") else str(error))
        print(f"Report generation failed: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
