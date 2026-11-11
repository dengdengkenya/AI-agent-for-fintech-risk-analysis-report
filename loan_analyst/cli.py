"""Command line: turn arguments into validated settings, then call one workflow.

Reading order: main() -> _options() -> the matching run_* function in workflow.py.
Calculations live in calculations.py; this file only parses and dispatches.
Every command supports --help.
"""

import argparse
import json
import math
import sys

from .calculations import DEFINITIONS, build_registry
from .evals import DEFAULT_SUITE, run_eval
from .mcp_server import run_mcp
from .report import DEFAULT_SECTIONS, check_sections
from .settings import (TOOL_MODES, DatabaseConfig, ModelConfig, Scenario, SettingsError, check_month,
                       load_config, project_path)
from .support import display_path
from .tools import ToolContext
from .workflow import (RunOptions, Services, obtain_snapshot, preview_queries, run_ask,
                       run_chat, run_render, run_report, run_trace)

DESCRIPTION = """Historical Lending Club score-cutoff analysis from MySQL, with a local tool-calling analyst.

common workflows:
  report                      read MySQL once, run every analysis, add analyst commentary
  report --snapshot latest    rebuild from the last saved snapshot without scanning MySQL
  ask "QUESTION"              answer one question; the analyst chooses and runs tools
  chat                        interactive analyst session over one snapshot
  render DIR                  re-render a report after reviewing ollama_analysis.json
  report --resume             continue an interrupted report without repeating model calls
  eval --snapshot latest      score the analyst on evals/cases.toml to compare models or prompts
  trace DIR                   show where a report's time and tokens went
"""


def month(value):
    try:
        return check_month("month", value)
    except SettingsError:
        raise argparse.ArgumentTypeError("Use YYYY-MM, for example 2015-01") from None


def fraction(value):
    try:
        number = float(value)
    except ValueError:
        number = math.nan
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise argparse.ArgumentTypeError("Use a fraction from 0 to 1 (0.08 means 8%)")
    return number


def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError("Use a positive integer")
    return number


def sections(value):
    return [part.strip() for part in value.split(",") if part.strip()]


def _add_database_arguments(parser, defaults):
    values = defaults.get("database", {})
    group = parser.add_argument_group("MySQL source (password from MYSQL_PASSWORD or a terminal prompt)")
    group.add_argument("--host", default=values.get("host", "localhost"))
    group.add_argument("--port", type=int, default=values.get("port", 3306))
    group.add_argument("--user", default=values.get("user", "root"))
    group.add_argument("--database", "--schema", default=values.get("database", "demo"))
    group.add_argument("--accepted-table", default=values.get("accepted_table", "accepted"))
    group.add_argument("--rejected-table", default=values.get("rejected_table", "rejected"))
    group.add_argument("--query-timeout", type=int, default=values.get("query_timeout", 600),
                       help="Maximum seconds per aggregate SELECT (default %(default)s)")


def _add_snapshot_argument(parser):
    parser.add_argument("--snapshot", metavar="PATH|latest",
                        help="Reuse a saved MySQL snapshot instead of scanning: a snapshot JSON path, "
                             "or 'latest'")


def _add_scenario_arguments(parser, defaults):
    values = defaults.get("scenario", {})
    group = parser.add_argument_group("scenario (defaults for every analysis; the analyst may vary cohort, "
                                      "cutoffs and ceiling)")
    cutoffs = values.get("cutoffs", "600,640,660,680,700,720,740")
    group.add_argument("--cutoffs", default=",".join(map(str, cutoffs)) if isinstance(cutoffs, list) else cutoffs,
                       help="Comma-separated inclusive minimum scores (default %(default)s)")
    group.add_argument("--baseline", type=int, default=values.get("baseline", 660),
                       help="Comparison cutoff; illustrative default 660, not current policy")
    group.add_argument("--start-month", type=month, default=values.get("start_month"))
    group.add_argument("--end-month", type=month, default=values.get("end_month"))
    group.add_argument("--max-loss-rate", type=fraction, default=values.get("max_loss_rate"),
                       help="Optional historical loss-proxy ceiling, e.g. 0.08")
    group.add_argument("--min-outcomes", type=int, default=values.get("min_outcomes", 100),
                       help="Minimum complete terminal outcomes to display a rate (default %(default)s)")
    group.add_argument("--assume-comparable-scores", action="store_true",
                       default=values.get("assume_comparable_scores", False),
                       help="Explicit hypothetical assumption: rejected scores equal the FICO scale")
    group.add_argument("--rejected-loss-rate", type=fraction, default=values.get("rejected_loss_rate"),
                       help="Assumed lifetime principal loss rate on new rejected lending")
    group.add_argument("--take-up-rate", type=fraction, default=values.get("take_up_rate", 1.0),
                       help="Assumed share of screened rejected demand funded (default 1)")


def _add_model_arguments(parser, defaults):
    values = defaults.get("model", {})
    group = parser.add_argument_group("local model (loopback Ollama only)")
    group.add_argument("--ollama-url", default=values.get("url", "http://localhost:11434"))
    group.add_argument("--ollama-model", default=values.get("model", ModelConfig.model),
                       help="Installed local model tag (default %(default)s)")
    group.add_argument("--ollama-timeout", type=positive_int, default=values.get("timeout", 300),
                       help="Maximum seconds per model request (default %(default)s)")
    group.add_argument("--ollama-keep-alive", default=values.get("keep_alive", "5m"),
                       help="How long Ollama keeps the model loaded (default %(default)s)")
    group.add_argument("--num-ctx", type=positive_int, default=values.get("num_ctx", 16384),
                       help="Model context window in tokens; tool rounds need room (default %(default)s)")
    group.add_argument("--max-tool-rounds", type=int, default=values.get("max_tool_rounds", 4),
                       help="Rounds in which the analyst may call tools; 0 only interprets "
                            "(default %(default)s)")
    group.add_argument("--max-tool-calls", type=positive_int, default=values.get("max_tool_calls", 8),
                       help="Total tool calls the analyst may make (default %(default)s)")
    group.add_argument("--tool-mode", choices=TOOL_MODES, default=values.get("tool_mode", "native"),
                       help="How the analyst requests tools: the model's native tool calls, or a JSON list "
                            "constrained by a schema (default %(default)s)")
    group.add_argument("--critic", action=argparse.BooleanOptionalAction, default=values.get("critic", True),
                       help="Have the model review each statement against its cited facts (default on)")
    group.add_argument("--max-model-calls", type=positive_int, default=values.get("max_model_calls", 40),
                       help="Safety limit on model requests per report or question (default %(default)s)")


def _add_output_argument(parser, default):
    parser.add_argument("--output-dir", type=project_path, default=project_path(default),
                        help="Folder for this command's files, relative to the project root")


def build_parser(defaults=None):
    """Describe commands and flags, using TOML values as overridable defaults."""
    defaults = defaults or {}
    report_defaults = defaults.get("report", {})
    parser = argparse.ArgumentParser(prog="python -m loan_analyst", description=DESCRIPTION,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="TOML defaults file (default: loan_analyst.toml when present)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    report = commands.add_parser("report", help="Write report.md plus evidence files")
    _add_database_arguments(report, defaults)
    _add_snapshot_argument(report)
    _add_scenario_arguments(report, defaults)
    _add_model_arguments(report, defaults)
    _add_output_argument(report, report_defaults.get("output_dir", "outputs/framework_report"))
    report.add_argument("--sections", type=sections,
                        default=report_defaults.get("sections", list(DEFAULT_SECTIONS)),
                        help="Comma-separated report sections in order: " + ",".join(DEFAULT_SECTIONS))
    report.add_argument("--no-ai", action="store_true",
                        help="Explicitly write only the calculated MySQL report")
    report.add_argument("--print-sql", action="store_true",
                        help="Print the aggregate SQL without connecting")
    report.add_argument("--resume", nargs="?", const="latest", metavar="RUN_ID",
                        help="Continue an interrupted report run (default: the latest) from its saved "
                             "snapshot and model replies; MySQL is not queried")

    for name, help_text, folder in (("ask", "Answer one question with the tool-calling analyst", "outputs/ask"),
                                    ("chat", "Interactive analyst session", "outputs/chat")):
        command = commands.add_parser(name, help=help_text)
        if name == "ask":
            command.add_argument("question", help="Question in plain English, in quotes")
        _add_database_arguments(command, defaults)
        _add_snapshot_argument(command)
        _add_scenario_arguments(command, defaults)
        _add_model_arguments(command, defaults)
        _add_output_argument(command, folder)

    render = commands.add_parser("render", help="Re-render report.md from a report folder's saved artifacts")
    render.add_argument("directory", type=project_path, help="Report folder, e.g. outputs/framework_report")
    render.add_argument("--sections", type=sections,
                        default=report_defaults.get("sections", list(DEFAULT_SECTIONS)))

    evaluate = commands.add_parser("eval", help="Score the analyst on a question suite over a saved snapshot")
    evaluate.add_argument("--suite", type=project_path, default=DEFAULT_SUITE,
                          help="TOML suite of cases (default evals/cases.toml)")
    evaluate.add_argument("--repeat", type=positive_int, default=1,
                          help="Run every case this many times (default %(default)s)")
    evaluate.add_argument("--compare-with", type=project_path, metavar="RESULTS_JSON",
                          help="An earlier eval results.json to show changes against")
    _add_database_arguments(evaluate, defaults)
    _add_snapshot_argument(evaluate)
    _add_scenario_arguments(evaluate, defaults)
    _add_model_arguments(evaluate, defaults)
    _add_output_argument(evaluate, "outputs/evals")

    trace = commands.add_parser("trace", help="Print the step timeline (time, tokens) saved with a report")
    trace.add_argument("directory", type=project_path, help="Report folder, e.g. outputs/framework_report")

    commands.add_parser("tools", help="Print the analyst's tool catalog as JSON")
    call = commands.add_parser("call", help="Run one tool and print JSON facts and results")
    call.add_argument("name", help="Tool name from the `tools` command")
    call.add_argument("arguments", nargs="?", default="{}",
                      help="JSON object, or '-' to read it from stdin")
    _add_database_arguments(call, defaults)
    _add_snapshot_argument(call)
    _add_scenario_arguments(call, defaults)

    mcp = commands.add_parser("mcp", help="Serve the analysis tools to a local MCP client over stdio, "
                                          "from a saved snapshot")
    _add_database_arguments(mcp, defaults)
    _add_snapshot_argument(mcp)
    _add_scenario_arguments(mcp, defaults)

    sql = commands.add_parser("sql", help="Print the aggregate SQL without connecting")
    _add_database_arguments(sql, defaults)
    snapshot = commands.add_parser("snapshot", help="Read MySQL once and save a reusable snapshot")
    _add_database_arguments(snapshot, defaults)
    return parser


def _options(parser, args):
    """Build validated settings; any rule violation is a usage error (exit status 2)."""
    try:
        database = DatabaseConfig(
            host=args.host,
            port=args.port,
            user=args.user,
            database=args.database,
            accepted_table=args.accepted_table,
            rejected_table=args.rejected_table,
            query_timeout=args.query_timeout,
        )
        # SQL preview and snapshot capture do not run any analysis.
        if not hasattr(args, "cutoffs"):
            return RunOptions(database=database, scenario=None)

        scenario = Scenario(
            cutoffs=args.cutoffs,
            baseline=args.baseline,
            start_month=args.start_month,
            end_month=args.end_month,
            max_loss_rate=args.max_loss_rate,
            min_outcomes=args.min_outcomes,
            assume_comparable_scores=args.assume_comparable_scores,
            rejected_loss_rate=args.rejected_loss_rate,
            take_up_rate=args.take_up_rate,
        )
        model = None
        if hasattr(args, "ollama_url") and not getattr(args, "no_ai", False):
            model = ModelConfig(
                url=args.ollama_url,
                model=args.ollama_model,
                timeout=args.ollama_timeout,
                keep_alive=args.ollama_keep_alive,
                num_ctx=args.num_ctx,
                max_tool_rounds=args.max_tool_rounds,
                max_tool_calls=args.max_tool_calls,
                max_model_calls=args.max_model_calls,
                tool_mode=args.tool_mode,
                critic=args.critic,
            )

        # Keep RunOptions defaults for flags that this command does not expose.
        extra = {}
        if hasattr(args, "sections"):
            extra["sections"] = check_sections(args.sections)
        if hasattr(args, "output_dir"):
            extra["output_dir"] = args.output_dir
        if getattr(args, "resume", None):
            extra["resume"] = args.resume
        snapshot = args.snapshot
        if snapshot not in (None, "latest"):
            snapshot = str(project_path(snapshot))
        return RunOptions(database=database, scenario=scenario, model=model, snapshot=snapshot, **extra)
    except SettingsError as error:
        parser.error(str(error))


def _defaults(argv, services):
    """Load --config, or loan_analyst.toml at the project root when it exists."""
    # Read only --config first: its contents supply defaults to the full parser.
    early = argparse.ArgumentParser(add_help=False)
    early.add_argument("--config")
    known, _ = early.parse_known_args(argv)
    path = project_path(known.config) if known.config else services.default_config
    if path is None or (not known.config and not path.is_file()):
        return {}
    try:
        defaults = load_config(path)
    except OSError:
        raise SettingsError(f"Cannot read config file {display_path(path)}") from None
    services.log(f"Using defaults from {display_path(path)}")
    return defaults


def _call(options, args, services):
    snapshot = obtain_snapshot(options.database, options.snapshot, services)
    registry = build_registry()
    context = ToolContext(snapshot, options.scenario)

    arguments = sys.stdin.read() if args.arguments == "-" else args.arguments
    envelope = registry.call(args.name, arguments, context)
    if envelope["ok"]:
        output = envelope["output"]
        envelope = {
            "ok": True,
            "tool": envelope["tool"],
            "arguments": envelope["arguments"],
            "title": output.title,
            "facts": output.facts,
            "result": output.result,
        }
    services.out(json.dumps(envelope, indent=2, allow_nan=False))
    return 0 if envelope["ok"] else 1


def main(argv=None, services=None):
    """Parse input, dispatch one command, and return its process exit code."""
    services = services or Services()
    argv = list(sys.argv[1:] if argv is None else argv)

    try:
        defaults = _defaults(argv, services)
    except SettingsError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    parser = build_parser(defaults)
    args = parser.parse_args(argv)

    if args.command == "tools":
        services.out(json.dumps({"tools": build_registry().definitions(), "definitions": DEFINITIONS},
                                indent=2))
        return 0

    options = None if args.command in ("render", "trace") else _options(parser, args)

    try:
        if args.command == "sql" or (args.command == "report" and args.print_sql):
            services.out(json.dumps(preview_queries(options.database), indent=2, allow_nan=False))
            return 0
        if args.command == "snapshot":
            obtain_snapshot(options.database, None, services)
            return 0
        if args.command == "report":
            return run_report(options, services)
        if args.command == "ask":
            return run_ask(options, args.question, services)
        if args.command == "chat":
            return run_chat(options, services)
        if args.command == "render":
            return run_render(args.directory, check_sections(args.sections), services)
        if args.command == "eval":
            return run_eval(options, services, suite=args.suite, repeat=args.repeat,
                            baseline=args.compare_with)
        if args.command == "mcp":
            return run_mcp(options, services)
        if args.command == "trace":
            return run_trace(args.directory, services)
        if args.command == "call":
            return _call(options, args, services)
    except KeyboardInterrupt:
        print("Interrupted; no database data was changed", file=sys.stderr)
        return 130
    except Exception as error:
        # Connector messages can quote source text or credentials; never print them.
        detail = ("Database operation failed; check access, schema and query timeout"
                  if type(error).__module__.startswith("mysql") else str(error))
        print(f"{args.command.capitalize()} failed: {detail}", file=sys.stderr)
        return 1
