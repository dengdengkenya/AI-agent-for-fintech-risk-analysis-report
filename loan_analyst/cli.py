"""Command line: python -m loan_analyst <command> [options]. Every command has --help."""

import argparse
import json
import math
import sys

from .analyses import DEFINITIONS, build_registry
from .cube import display_path
from .pipeline import (RunOptions, Services, obtain_snapshot, preview_queries, run_ask, run_chat, run_render,
                       run_report, toolbox)
from .report import DEFAULT_SECTIONS, check_sections
from .settings import (DatabaseConfig, ModelConfig, Scenario, SettingsError, check_month, load_config,
                       project_path)

DESCRIPTION = """Historical Lending Club score-cutoff analysis from MySQL, with a local tool-calling analyst.

common workflows:
  report                      read MySQL once, run every analysis, add analyst commentary
  report --snapshot latest    rebuild from the last saved snapshot without scanning MySQL
  ask "QUESTION"              answer one question; the analyst chooses and runs tools
  chat                        interactive analyst session over one snapshot
  render DIR                  re-render a report after reviewing ollama_analysis.json
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


def _database(parser, defaults):
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


def _snapshot(parser):
    parser.add_argument("--snapshot", metavar="PATH|latest",
                        help="Reuse a saved MySQL snapshot instead of scanning: a snapshot JSON path, or 'latest'")


def _scenario(parser, defaults):
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


def _model(parser, defaults):
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
                       help="Rounds in which the analyst may call tools; 0 only interprets (default %(default)s)")
    group.add_argument("--max-tool-calls", type=positive_int, default=values.get("max_tool_calls", 8),
                       help="Total tool calls the analyst may make (default %(default)s)")


def _output(parser, default):
    parser.add_argument("--output-dir", type=project_path, default=project_path(default),
                        help="Folder for this command's files, relative to the project root")


def build_parser(defaults=None):
    defaults = defaults or {}
    report_defaults = defaults.get("report", {})
    parser = argparse.ArgumentParser(prog="python -m loan_analyst", description=DESCRIPTION,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="TOML defaults file (default: loan_analyst.toml when present)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    report = commands.add_parser("report", help="Write report.md plus evidence files")
    _database(report, defaults)
    _snapshot(report)
    _scenario(report, defaults)
    _model(report, defaults)
    _output(report, report_defaults.get("output_dir", "outputs/framework_report"))
    report.add_argument("--sections", type=sections, default=report_defaults.get("sections", list(DEFAULT_SECTIONS)),
                        help="Comma-separated report sections in order: " + ",".join(DEFAULT_SECTIONS))
    report.add_argument("--no-ai", action="store_true", help="Explicitly write only the calculated MySQL report")
    report.add_argument("--print-sql", action="store_true", help="Print the aggregate SQL without connecting")

    for name, help_text, folder in (("ask", "Answer one question with the tool-calling analyst", "outputs/ask"),
                                    ("chat", "Interactive analyst session", "outputs/chat")):
        command = commands.add_parser(name, help=help_text)
        if name == "ask":
            command.add_argument("question", help="Question in plain English, in quotes")
        _database(command, defaults)
        _snapshot(command)
        _scenario(command, defaults)
        _model(command, defaults)
        _output(command, folder)

    render = commands.add_parser("render", help="Re-render report.md from a report folder's saved artifacts")
    render.add_argument("directory", type=project_path, help="Report folder, e.g. outputs/framework_report")
    render.add_argument("--sections", type=sections, default=report_defaults.get("sections", list(DEFAULT_SECTIONS)))

    commands.add_parser("tools", help="Print the analyst's tool catalog as JSON")
    call = commands.add_parser("call", help="Run one tool and print JSON facts and results")
    call.add_argument("name", help="Tool name from the `tools` command")
    call.add_argument("arguments", nargs="?", default="{}", help="JSON object, or '-' to read it from stdin")
    _database(call, defaults)
    _snapshot(call)
    _scenario(call, defaults)

    sql = commands.add_parser("sql", help="Print the aggregate SQL without connecting")
    _database(sql, defaults)
    snapshot = commands.add_parser("snapshot", help="Read MySQL once and save a reusable snapshot")
    _database(snapshot, defaults)
    return parser


def _options(parser, args):
    """Build validated settings; any rule violation is a usage error (exit status 2)."""
    try:
        database = DatabaseConfig(host=args.host, port=args.port, user=args.user, database=args.database,
                                  accepted_table=args.accepted_table, rejected_table=args.rejected_table,
                                  query_timeout=args.query_timeout)
        if not hasattr(args, "cutoffs"):
            return RunOptions(database, None)
        scenario = Scenario(cutoffs=args.cutoffs, baseline=args.baseline, start_month=args.start_month,
                            end_month=args.end_month, max_loss_rate=args.max_loss_rate,
                            min_outcomes=args.min_outcomes, assume_comparable_scores=args.assume_comparable_scores,
                            rejected_loss_rate=args.rejected_loss_rate, take_up_rate=args.take_up_rate)
        model = None
        if hasattr(args, "ollama_url") and not getattr(args, "no_ai", False):
            model = ModelConfig(url=args.ollama_url, model=args.ollama_model, timeout=args.ollama_timeout,
                                keep_alive=args.ollama_keep_alive, num_ctx=args.num_ctx,
                                max_tool_rounds=args.max_tool_rounds, max_tool_calls=args.max_tool_calls)
        extra = {"sections": check_sections(args.sections)} if hasattr(args, "sections") else {}
        if hasattr(args, "output_dir"):
            extra["output_dir"] = args.output_dir
        snapshot = args.snapshot if args.snapshot in (None, "latest") else str(project_path(args.snapshot))
        return RunOptions(database, scenario, model, snapshot=snapshot, **extra)
    except SettingsError as error:
        parser.error(str(error))


def _defaults(argv, services):
    """Load --config, or loan_analyst.toml at the project root when it exists."""
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
    registry, context, _ = toolbox(snapshot, options.scenario)
    arguments = sys.stdin.read() if args.arguments == "-" else args.arguments
    envelope = registry.call(args.name, arguments, context)
    if envelope["ok"]:
        output = envelope["output"]
        envelope = {"ok": True, "tool": envelope["tool"], "arguments": envelope["arguments"], "title": output.title,
                    "facts": output.facts, "result": output.result}
    services.out(json.dumps(envelope, indent=2, allow_nan=False))
    return 0 if envelope["ok"] else 1


def main(argv=None, services=None):
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
        services.out(json.dumps({"tools": build_registry().definitions(), "definitions": DEFINITIONS}, indent=2))
        return 0
    options = None if args.command == "render" else _options(parser, args)
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
