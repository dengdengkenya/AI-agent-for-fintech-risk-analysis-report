"""Command line entry point.

    python -m report_agent build-data
    python -m report_agent check
    python -m report_agent list
    python -m report_agent report --team risk
    python -m report_agent report --team marketing --request "重点看各渠道ROI和短信召回效果"
    python -m report_agent report --team risk --no-llm
    python -m report_agent report --team policy --request "如果把评分线提高到620，通过率和坏账率会怎样"
    python -m report_agent report --team policy --score-cutoff 620 --max-dti 40
    python -m report_agent load-lendingclub --frac 0.1
    python -m report_agent report --team policy --db outputs/lending_club.db --score-cutoff 700
"""

import argparse
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import sqlite3
import sys
import time

from .agent import ReportAgent
from .analyses import ANALYSES
from .config import PROJECT_ROOT, load_settings
from .lendingclub import load as load_lendingclub
from .llm import LLMError, OllamaClient
from .policy import Scenario
from .synthetic_data import build_database


def _is_old_synthetic(path) -> bool:
    """A synthetic database built before the applications table existed."""
    with sqlite3.connect(path) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "_meta" not in names or "applications" in names:
            return False
        meta = dict(conn.execute("SELECT key, value FROM _meta").fetchall())
    return "SYNTHETIC" in meta.get("notice", "")


def _ensure_data(settings, rebuild=False):
    if not rebuild and settings.db_path.exists() and _is_old_synthetic(settings.db_path):
        print("[data] synthetic database has no applications table; rebuilding")
        rebuild = True
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


def cmd_load_lendingclub(args, settings):
    db = Path(args.db) if args.db else PROJECT_ROOT / "outputs" / "lending_club.db"
    t = time.perf_counter()
    counts = load_lendingclub(Path(args.accepted), Path(args.rejected), db, args.frac, args.since, args.until)
    print(f"[data] wrote {db} in {time.perf_counter() - t:.0f}s: {counts}")
    print(f"Next: python -m report_agent report --team policy --db {db}")


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
    if args.db:
        settings = replace(settings, db_path=Path(args.db))
        if not settings.db_path.exists():
            print(f"Database not found: {settings.db_path}")
            return 2
    else:
        _ensure_data(settings)
    analyses = None
    if args.analyses:
        analyses = [k.strip() for k in args.analyses.split(",") if k.strip()]
        unknown = [k for k in analyses if k not in ANALYSES]
        if unknown:
            print(f"Unknown analyses: {', '.join(unknown)}. See: python -m report_agent list")
            return 2
    agent = ReportAgent(settings, use_llm=not args.no_llm)
    report = agent.run(args.team, args.request, analyses, Scenario(args.score_cutoff, args.max_dti))

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
    rep.add_argument("--team", choices=["risk", "policy", "marketing"], required=True)
    rep.add_argument("--request", default="", help="What the reader wants, in Chinese or English")
    rep.add_argument("--analyses", help="Comma-separated analysis keys; skips the planning step")
    rep.add_argument("--no-llm", action="store_true", help="Compute only, no model calls")
    rep.add_argument("--score-cutoff", type=float, help="New score cutoff for cutoff_simulation")
    rep.add_argument("--max-dti", type=float, help="New DTI limit in percent for cutoff_simulation")
    rep.add_argument("--db", help="SQLite database to report on (default: DEMO_DB, the synthetic data)")
    lc = sub.add_parser("load-lendingclub", help="Load the real Lending Club CSVs for the policy reports")
    lc.add_argument("--accepted", default=str(PROJECT_ROOT / "data" / "accepted_2007_to_2018Q4.csv.gz"))
    lc.add_argument("--rejected", default=str(PROJECT_ROOT / "data" / "rejected_2007_to_2018Q4.csv.gz"))
    lc.add_argument("--db", help="Output SQLite file (default outputs/lending_club.db)")
    lc.add_argument("--frac", type=float, default=1.0,
                    help="Random share of rows to keep from both files, e.g. 0.1 (keeps the approval rate)")
    lc.add_argument("--since", help="First application month to keep, YYYY-MM")
    lc.add_argument("--until", help="Last application month to keep, YYYY-MM")
    args = parser.parse_args(argv)

    settings = load_settings()
    handler = {"build-data": cmd_build_data, "list": cmd_list,
               "check": cmd_check, "report": cmd_report,
               "load-lendingclub": cmd_load_lendingclub}[args.command]
    try:
        return handler(args, settings) or 0
    except LLMError as e:
        print(f"LLM error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
