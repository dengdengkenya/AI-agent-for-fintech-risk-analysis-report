"""Command line entry point.

    python -m report_agent build-data
    python -m report_agent check
    python -m report_agent list
    python -m report_agent report --team risk
    python -m report_agent report --team marketing --request "重点看各渠道ROI和短信召回效果"
    python -m report_agent report --team risk --no-llm
"""

import argparse
from datetime import datetime
import sys
import time

from .agent import ReportAgent
from .analyses import ANALYSES
from .config import load_settings
from .llm import LLMError, OllamaClient
from .synthetic_data import build_database


def _ensure_data(settings, rebuild=False):
    if rebuild or not settings.db_path.exists():
        t = time.perf_counter()
        counts = build_database(settings.db_path)
        print(f"[data] built SYNTHETIC demo database {settings.db_path} in "
              f"{time.perf_counter() - t:.1f}s: {counts}")


def cmd_build_data(args, settings):
    _ensure_data(settings, rebuild=True)


def cmd_list(args, settings):
    for a in ANALYSES.values():
        print(f"{a.team:10} {a.key:20} {a.title}：{a.description}")


def cmd_check(args, settings):
    client = OllamaClient(settings)
    print(f"Ollama host: {settings.ollama_host}\nModel:       {settings.ollama_model}")
    models = client.list_models()
    print(f"Installed models: {', '.join(models) or '(none)'}")
    if settings.ollama_model not in models and f"{settings.ollama_model}:latest" not in models:
        print(f"Model not installed. Run: ollama pull {settings.ollama_model}  (or set OLLAMA_MODEL)")
        return 1
    reply = client.chat("你是一个简洁的助手。", "用一句话介绍Vintage分析。", num_predict=80)
    print(f"Test reply: {reply.text}\nTiming: {reply.summary()}")
    return 0


def cmd_report(args, settings):
    _ensure_data(settings)
    analyses = None
    if args.analyses:
        analyses = [k.strip() for k in args.analyses.split(",") if k.strip()]
        unknown = [k for k in analyses if k not in ANALYSES]
        if unknown:
            print(f"Unknown analyses: {', '.join(unknown)}. See: python -m report_agent list")
            return 2
    agent = ReportAgent(settings, use_llm=not args.no_llm)
    report = agent.run(args.team, args.request, analyses)

    settings.output_dir.mkdir(parents=True, exist_ok=True)
    path = settings.output_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{args.team}.md"
    path.write_text(report.markdown, encoding="utf-8")

    print("\n=== Timing ===")
    for t in report.timings:
        print(f"{t.name:28} {t.seconds:8.2f}s  {t.detail}")
    if report.unverified_numbers:
        print(f"\nNumbers to check by hand (not found in computed results): "
              f"{', '.join(report.unverified_numbers)}")
    print(f"\nReport written to {path}")
    return 0


def main(argv=None):
    # Windows consoles default to a legacy code page; reports are Chinese.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m report_agent", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build-data", help="(Re)build the synthetic demo database")
    sub.add_parser("list", help="List available analyses")
    sub.add_parser("check", help="Check the Ollama server and model, and time a short reply")
    rep = sub.add_parser("report", help="Generate a report")
    rep.add_argument("--team", choices=["risk", "marketing"], required=True)
    rep.add_argument("--request", default="", help="What the reader wants, in Chinese or English")
    rep.add_argument("--analyses", help="Comma-separated analysis keys; skips the planning step")
    rep.add_argument("--no-llm", action="store_true", help="Compute only, no model calls")
    args = parser.parse_args(argv)

    settings = load_settings()
    handler = {"build-data": cmd_build_data, "list": cmd_list,
               "check": cmd_check, "report": cmd_report}[args.command]
    try:
        return handler(args, settings) or 0
    except LLMError as e:
        print(f"LLM error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
