"""The four workflows: report, ask, chat and render.

Start with ``run_report``. It is the whole application in one screenful:
check the model, obtain a snapshot, run the standard analyses, record the
evidence, let the analyst add commentary, then write the output files.

Other entry points in this file:
  run_ask      one question, printed and saved as a transcript
  run_chat     the same, repeated interactively over one snapshot
  run_render   rebuild report.md from saved artifacts after a wording review
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from time import perf_counter
from uuid import uuid4

from . import PROJECT_ROOT, prompts
from .analyst import (ASK_SECTIONS, REPORT_SECTIONS, AnalysisError, Analyst, ChatSession,
                      Ledger, validate_answer)
from .calculations import DEFINITIONS, build_registry
from .database import ConfigurationError, password_connection_factory
from .ollama import ModelError, OllamaClient
from .report import DEFAULT_SECTIONS, answer_markdown, answer_text, render_report
from .settings import DatabaseConfig, ModelConfig, Scenario, SettingsError
from .snapshot import (SOURCES, SchemaError, build_query, load_snapshot, read_snapshot,
                       save_snapshot)
from .support import display_path, read_json, sha256_json, utc_now, write_files
from .tools import ToolContext

REPORT_FORMAT = "loan-analyst-report/1"

# The analyses every report runs. A segment absent from the source is skipped.
REPORT_CALLS = (
    ("describe_data", {}),
    ("compare_score_cutoffs", {}),
    ("segment_performance", {"dimension": "score_band"}),
    ("segment_performance", {"dimension": "issue_year"}),
    ("segment_performance", {"dimension": "grade"}),
    ("segment_performance", {"dimension": "term"}),
    ("rejected_applications", {"by": "score_era"}),
)


class DatabaseError(RuntimeError):
    """A sanitized database failure: connector messages can quote data or credentials."""


def status(message):
    """Progress messages go to stderr, keeping stdout clean for command output."""
    print(message, file=sys.stderr, flush=True)


@dataclass
class Services:
    """Things a workflow uses outside Python's own calculations.

    Production uses the defaults. Tests replace them with fake connections, a
    scripted model, a fixed clock and captured terminal input/output.
    """

    connection_factory: Callable = password_connection_factory
    model_factory: Callable = OllamaClient
    snapshot_dir: Path = PROJECT_ROOT / "outputs" / "snapshots"
    default_config: Path | None = PROJECT_ROOT / "loan_analyst.toml"
    log: Callable = status
    out: Callable = print
    read_line: Callable = input
    clock: Callable = utc_now


@dataclass(frozen=True)
class RunOptions:
    """Validated inputs for one command, separate from its external services."""

    database: DatabaseConfig
    scenario: Scenario | None  # SQL preview and snapshot-only commands need no scenario.
    model: ModelConfig | None = None  # None disables the model (report --no-ai).
    output_dir: Path = PROJECT_ROOT / "outputs" / "framework_report"
    snapshot: str | None = None  # a saved snapshot path, or "latest"
    sections: tuple = DEFAULT_SECTIONS


def preview_queries(database):
    """The exact aggregate SQL; no connection is made."""
    return {kind: {"sql": build_query(kind, database), "params": []} for kind in SOURCES}


def obtain_snapshot(database, reference, services):
    """Load an explicit saved snapshot, or read MySQL once and save the result."""
    if reference:
        snapshot = load_snapshot(reference, services.snapshot_dir, database)
        services.log(f"Using saved snapshot {snapshot.snapshot_id} (MySQL read completed "
                     f"{snapshot.read_completed_at_utc}); MySQL is not queried.")
        return snapshot

    services.log(f"Reading {database.database}.{database.accepted_table} and {database.database}."
                 f"{database.rejected_table} in one read-only MySQL snapshot; "
                 "large tables can take minutes...")
    started = perf_counter()

    connection = None
    try:
        connection = services.connection_factory(database)
        snapshot = read_snapshot(connection, database, clock=services.clock)
    except (ConfigurationError, SchemaError):
        raise
    except Exception:
        raise DatabaseError("Database operation failed; check access, schema and query timeout") from None
    finally:
        if connection is not None:
            connection.close()

    path = save_snapshot(snapshot, services.snapshot_dir)
    services.log(f"Snapshot {snapshot.snapshot_id} read in {perf_counter() - started:.1f}s and saved to "
                 f"{display_path(path)}. Reuse it without rescanning: --snapshot latest")
    return snapshot


def run_calls(registry, context, ledger, calls):
    """Run host-chosen tool calls through the same registry the model uses."""
    for name, arguments in calls:
        envelope = registry.call(name, arguments, context, sanitize=False)
        if envelope["ok"]:
            ledger.record(name, envelope["arguments"], envelope["output"], origin="report")
        elif envelope["error"]["code"] != "unavailable":
            raise RuntimeError(f"{name}: {envelope['error']['message']}")


def _short(arguments):
    return ", ".join(f"{key}={value}" for key, value in arguments.items())


def progress(log):
    """Translate analyst events into terminal progress lines."""

    def on_event(event, **details):
        if event == "investigating":
            log(f"  Round {details['round']}: the analyst is choosing tools...")
        elif event == "tool":
            # Repeats reuse the ledger record and fact IDs; the handler still runs.
            repeat = " (repeat, cached)" if details["repeated"] else ""
            log(f"    {details['call_id']} {details['name']}({_short(details['arguments'])}){repeat}")
        elif event == "tool_error":
            log(f"    rejected {details['name']}: {details['error']['code']}")
        elif event == "composing":
            log(f"  Writing the evidence-checked answer (attempt {details['attempt']})...")
        elif event == "answered":
            log(f"  {details['statements']} statement(s) validated, {details['withheld']} withheld.")

    return on_event


def build_report_evidence(options, snapshot, ledger, services):
    """Freeze the calculated results before the model adds its own tool calls.

    The source hash links the report and model answer to these exact inputs.
    DatabaseConfig.sources() deliberately excludes the host, user and credentials.
    """
    report_calls = []
    for call in ledger.calls:
        report_calls.append({
            "id": call["id"],
            "tool": call["tool"],
            "arguments": call["arguments"],
            "origin": call["origin"],
            "title": call["title"],
            "fact_ids": call["fact_ids"],
            "result": call["result"],
        })

    settings = {
        "database": options.database.sources(),
        "scenario": options.scenario.to_dict(),
    }
    # The hash covers the standard report inputs only (not model usage), so a
    # report and its model answer can be tied to the same data later.
    source = {
        "mode": "historical_mysql",
        "settings": settings,
        "snapshot_sha256": snapshot.content_sha256,
        "queries": snapshot.queries,
        "results": report_calls,
    }

    return {
        "format": REPORT_FORMAT,
        "mode": "historical_mysql",
        "run_id": str(uuid4()),
        "source_evidence_sha256": sha256_json(source),
        "generated_at_utc": services.clock(),
        "settings": settings,
        "snapshot": snapshot.provenance(),
        "queries": snapshot.queries,
        "calls": report_calls,
        "facts": dict(ledger.facts),
    }


def report_data(evidence, analysis):
    """Everything report sections read; rebuildable from the saved artifacts."""
    return {
        "run_id": evidence["run_id"],
        "generated_at_utc": evidence["generated_at_utc"],
        "source_evidence_sha256": evidence["source_evidence_sha256"],
        "settings": evidence["settings"],
        "snapshot": evidence["snapshot"],
        "facts": evidence["facts"],
        "calls": [*evidence["calls"], *analysis.get("tool_results", [])],
        "analysis": analysis,
    }


def run_report(options: RunOptions, services: Services):
    """Read data, calculate facts, validate model commentary, then write the report."""
    started = perf_counter()

    # 1. Check the local model before an expensive MySQL scan.
    client = None
    if options.model is not None:
        services.log(f"Checking local Ollama model {options.model.model}...")
        client = services.model_factory(options.model)
        client.preflight()

    # 2. Read MySQL once, or load the explicitly requested saved snapshot.
    snapshot = obtain_snapshot(options.database, options.snapshot, services)

    # 3. Run the standard analyses over the in-memory snapshot.
    registry = build_registry()
    context = ToolContext(snapshot, options.scenario)
    ledger = Ledger(DEFINITIONS)
    run_calls(registry, context, ledger, REPORT_CALLS)
    services.log(f"Computed {len(ledger.calls)} analyses from the snapshot.")

    # 4. Freeze the calculated inputs before the model can add tool calls.
    evidence = build_report_evidence(options, snapshot, ledger, services)

    # 5. Optionally let the analyst investigate and write checked commentary.
    if client is None:
        analysis = {"status": "disabled", "reason": "Explicit --no-ai option"}
    else:
        services.log("Analyst reviewing the results; it may run further tools over the snapshot...")
        analyst = Analyst(
            client, registry, context, ledger,
            max_rounds=options.model.max_tool_rounds,
            max_calls=options.model.max_tool_calls,
            on_event=progress(services.log),
        )
        analysis = analyst.run(prompts.REPORT_GOAL, sections=REPORT_SECTIONS,
                               preload=ledger.highlights("report"))
        analysis["tool_results"] = [call for call in ledger.calls if call["origin"] == "model"]

    analysis.update(run_id=evidence["run_id"],
                    source_evidence_sha256=evidence["source_evidence_sha256"])
    evidence["ai_analysis"] = {
        key: analysis[key] for key in ("status", "run_id", "source_evidence_sha256")}
    evidence["elapsed_seconds"] = perf_counter() - started

    # 6. Render and serialize everything, then replace the four output files.
    #    A failure before this point leaves earlier report files untouched.
    write_files(options.output_dir, {
        "report.md": render_report(report_data(evidence, analysis), options.sections),
        "aggregates.json": json.dumps(evidence, indent=2, allow_nan=False),
        "snapshot.json": json.dumps(snapshot.to_payload(), separators=(",", ":"), allow_nan=False),
        "ollama_analysis.json": json.dumps(analysis, indent=2, ensure_ascii=False, allow_nan=False),
    })

    folder = display_path(options.output_dir)
    services.out(f"Report: {folder}/report.md")
    services.out(f"Evidence: {folder}/aggregates.json, {folder}/snapshot.json, "
                 f"{folder}/ollama_analysis.json")
    services.log(f"Completed in {perf_counter() - started:.1f}s. Reruns replace these four files.")
    return 0


def prepare_session(options, services):
    """Check the model, obtain one snapshot, and prepare its analyst."""
    services.log(f"Checking local Ollama model {options.model.model}...")
    client = services.model_factory(options.model)
    client.preflight()

    snapshot = obtain_snapshot(options.database, options.snapshot, services)

    registry = build_registry()
    context = ToolContext(snapshot, options.scenario)
    ledger = Ledger(DEFINITIONS)
    run_calls(registry, context, ledger, [("describe_data", {})])

    analyst = Analyst(
        client, registry, context, ledger,
        max_rounds=options.model.max_tool_rounds,
        max_calls=options.model.max_tool_calls,
        on_event=progress(services.log),
    )
    return snapshot, ledger, analyst


def build_transcript(snapshot, options, turns, ledger):
    return {
        "format": "loan-analyst-transcript/1",
        "mode": "historical_mysql",
        "snapshot": snapshot.provenance(),
        "settings": {"database": options.database.sources(),
                     "scenario": options.scenario.to_dict()},
        "turns": turns,
        "tool_results": [call for call in ledger.calls if call["origin"] == "model"],
    }


def run_ask(options, question, services):
    snapshot, ledger, analyst = prepare_session(options, services)

    result = analyst.run(question, sections=ASK_SECTIONS,
                         preload=ledger.highlights("report"))

    services.out("")
    services.out(answer_text(result))

    stamp = datetime.fromisoformat(services.clock()).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    transcript = build_transcript(snapshot, options,
                                  [{"question": question, "analysis": result}], ledger)
    write_files(options.output_dir, {
        f"ask-{stamp}.md": answer_markdown(question, result),
        f"ask-{stamp}.json": json.dumps(transcript, indent=2, ensure_ascii=False,
                                        allow_nan=False),
    })
    services.log(f"Saved {display_path(options.output_dir)}/ask-{stamp}.md and .json")
    return 0


CHAT_HELP = ("Ask a question in plain English. Commands: /tools lists the analyst's tools, "
             "/help shows this, /quit ends the session and saves the transcript.")


def run_chat(options, services):
    snapshot, ledger, analyst = prepare_session(options, services)
    session = ChatSession(analyst, preload=ledger.highlights("report"))

    provenance = snapshot.provenance()
    accepted_rows = provenance["sources"]["accepted"]["total_rows"]
    rejected_rows = provenance["sources"]["rejected"]["total_rows"]
    services.out(f"Loan analyst ready: snapshot {snapshot.snapshot_id}, {accepted_rows:,} accepted and "
                 f"{rejected_rows:,} rejected rows. {CHAT_HELP}")

    while True:
        try:
            line = services.read_line("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if line in ("/quit", "/exit"):
            break
        if line in ("", "/help"):
            services.out(CHAT_HELP)
            continue
        if line == "/tools":
            services.out("\n".join(
                f"  {item['function']['name']}: {item['function']['description']}"
                for item in analyst.registry.definitions()))
            continue

        try:
            result = session.ask(line)
        except (AnalysisError, ModelError) as error:
            services.out(f"analyst> Sorry, I could not produce a validated answer: {error}")
            continue
        except KeyboardInterrupt:
            services.out("analyst> Stopped that question. Ask another, or /quit to save and exit.")
            continue

        services.out("\n" + answer_text(result))

    if session.turns:
        stamp = datetime.fromisoformat(services.clock()).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        turns = []
        for turn in session.turns:
            turns.append({"question": turn["question"], "analysis": turn["result"]})

        markdown = "\n".join(answer_markdown(turn["question"], turn["result"])
                             for turn in session.turns)
        transcript = build_transcript(snapshot, options, turns, ledger)
        write_files(options.output_dir, {
            f"chat-{stamp}.md": markdown,
            f"chat-{stamp}.json": json.dumps(transcript, indent=2, ensure_ascii=False,
                                             allow_nan=False),
        })
        services.log(f"Saved {display_path(options.output_dir)}/chat-{stamp}.md and .json")
    return 0


def check_review(analysis):
    """Edited wording must record its review and still pass the evidence checks."""
    if analysis["answer"] != analysis["model_draft"] and not analysis.get("review"):
        raise SettingsError(
            'The answer differs from model_draft; describe the edits in a "review" entry first')

    sections = {name: (1 if name == "answer" else 0, 10) for name in analysis["model_draft"]}
    _, _, problems = validate_answer(analysis["answer"], analysis["facts"], sections)
    if problems:
        raise SettingsError("Reviewed text fails evidence validation: " + "; ".join(problems[:3]))


def run_render(directory, sections, services):
    """Re-render report.md from saved artifacts, e.g. after a human review of the wording."""
    try:
        evidence = read_json((directory / "aggregates.json").read_text(encoding="utf-8"))
        analysis = read_json((directory / "ollama_analysis.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SettingsError(f"{display_path(directory)} does not contain readable report artifacts") from None

    mismatch_message = "aggregates.json and ollama_analysis.json are not from the same report run"
    if evidence.get("format") != REPORT_FORMAT:
        raise SettingsError(mismatch_message)

    expected_identity = (evidence.get("run_id"), evidence.get("source_evidence_sha256"))
    analysis_identity = (analysis.get("run_id"), analysis.get("source_evidence_sha256"))
    if analysis_identity != expected_identity:
        raise SettingsError(mismatch_message)

    if analysis.get("status") == "complete":
        check_review(analysis)

    write_files(directory, {"report.md": render_report(report_data(evidence, analysis), sections)})
    services.out(f"Report: {display_path(directory)}/report.md")
    return 0
