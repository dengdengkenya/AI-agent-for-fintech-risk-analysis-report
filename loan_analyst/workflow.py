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
import shutil
import sys
from time import perf_counter
from uuid import uuid4

from . import PROJECT_ROOT, prompts
from .analyst import (ASK_SECTIONS, AnalysisError, Analyst, ChatSession, Ledger, Topic,
                      validate_answer)
from .calculations import DEFINITIONS, build_registry
from .database import ConfigurationError, password_connection_factory
from .ollama import ModelError, OllamaClient
from .report import DEFAULT_SECTIONS, answer_markdown, answer_text, render_report
from .runtime import (BudgetExceeded, Journal, ResumeError, Run, Tracer, format_trace,
                      identity_differences, read_trace)
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
    ("vintage_trend", {}),
    ("loss_concentration", {"dimension": "score_band"}),
    ("loss_concentration", {"dimension": "grade"}),
    ("mix_shift", {"dimension": "score_band"}),
)


# How the analyst divides the report. Each topic is written by its own model call
# that sees only that topic's facts, which keeps a small model focused. A topic
# whose standard calls are all unavailable in this snapshot is skipped.
REPORT_TOPICS = (
    Topic("tradeoff",
          "The main tradeoff between retained loan volume and historical losses across the tested score "
          "cutoffs, and which score bands drive it.",
          calls=(("compare_score_cutoffs", {}), ("segment_performance", {"dimension": "score_band"}),
                 ("loss_concentration", {"dimension": "score_band"})),
          tools=("compare_score_cutoffs", "sweep_loss_ceilings", "segment_performance", "loss_concentration"),
          definitions=("retained", "loss_proxy", "candidate_rule", "concentration"),
          dimensions=("score_band",)),
    Topic("vintages",
          "How performance and the origination mix differ by issue year, and which vintages are seasoned "
          "enough to compare.",
          calls=(("vintage_trend", {}), ("segment_performance", {"dimension": "issue_year"}),
                 ("mix_shift", {"dimension": "score_band"})),
          tools=("vintage_trend", "mix_shift", "segment_performance"),
          definitions=("seasoning", "mix", "loss_proxy"),
          dimensions=("issue_year",)),
    Topic("segments",
          "How historical losses differ by loan grade and term, and where losses are concentrated.",
          calls=(("segment_performance", {"dimension": "grade"}), ("segment_performance", {"dimension": "term"}),
                 ("loss_concentration", {"dimension": "grade"})),
          tools=("segment_performance", "loss_concentration"),
          definitions=("loss_proxy", "concentration"),
          dimensions=("grade", "term")),
    Topic("rejected",
          "What the rejected applications show and why they cannot be read as lending opportunity.",
          calls=(("rejected_applications", {"by": "score_era"}),),
          tools=("rejected_applications",),
          definitions=("rejected_scores",)),
    Topic("data_quality",
          "Which data limitations and exclusions matter most when reading this report.",
          calls=(("describe_data", {}),),
          tools=("describe_data",),
          definitions=("source_limits", "next_checks")),
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
    runs_dir: Path = PROJECT_ROOT / "outputs" / "runs"  # journals of unfinished runs
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
    resume: str | None = None  # an interrupted report run ID, or "latest"
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
        elif event == "topic":
            log(f"  Topic: {details['name']}")
        elif event == "composing":
            stage = details.get("stage", "compose")
            what = "Assembling the report answer" if stage == "edit" else "Writing evidence-checked statements"
            log(f"  {what} (attempt {details['attempt']})...")
        elif event == "reviewed":
            log(f"    Critic reviewed {details['reviewed']} statement(s), flagged {details['flagged']}.")
        elif event == "answered":
            log(f"  {details['statements']} statement(s) validated, {details['withheld']} withheld.")

    return on_event


def build_report_evidence(options, snapshot, ledger, services, run_id=None):
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
        "run_id": run_id or str(uuid4()),  # a resumed run keeps its original ID
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

    tracer = Tracer(clock=services.clock)

    # 0. --resume continues an interrupted run from its journal and its own snapshot.
    journal = None
    snapshot_reference = options.snapshot
    if options.resume:
        journal, snapshot_reference = find_interrupted_run(options, services)

    # 1. Check the local model before an expensive MySQL scan.
    client = None
    if options.model is not None:
        client = check_model(options.model, services)

    # 2. Read MySQL once, or load the explicitly requested saved snapshot.
    with tracer.span("snapshot"):
        snapshot = obtain_snapshot(options.database, snapshot_reference, services)

    # 3. Run the standard analyses over the in-memory snapshot.
    with tracer.span("standard analyses"):
        registry = build_registry()
        context = ToolContext(snapshot, options.scenario)
        ledger = Ledger(DEFINITIONS)
        run_calls(registry, context, ledger, REPORT_CALLS)
    services.log(f"Computed {len(ledger.calls)} analyses from the snapshot.")

    # 4. Freeze the calculated inputs before the model can add tool calls.
    evidence = build_report_evidence(options, snapshot, ledger, services,
                                     run_id=journal.run_id if journal else None)

    # 5. Optionally let the analyst investigate and write checked commentary.
    #    Every model reply is journaled, so a failure here can be resumed.
    if client is None:
        analysis = {"status": "disabled", "reason": "Explicit --no-ai option"}
    else:
        identity = run_identity(options, snapshot, client, registry)
        if journal is None:
            journal = Journal.create(services.runs_dir, evidence["run_id"], "report", identity,
                                     snapshot_id=snapshot.snapshot_id)
        else:
            different = identity_differences(journal.header["identity"], identity)
            if different:
                raise ResumeError(f"Run {journal.run_id} cannot be resumed: these inputs changed since it "
                                  f"started: {', '.join(different)}. Rerun without --resume")
            services.log(f"Resuming run {journal.run_id}; saved model replies are reused when the "
                         "request is identical.")

        services.log("Analyst reviewing the results; it may run further tools over the snapshot...")
        model_run = Run(client, journal=journal, tracer=tracer,
                        max_model_calls=options.model.max_model_calls)
        analyst = build_analyst(client, registry, context, ledger, options.model, services, model_run)
        try:
            with tracer.span("analyst", **{"gen_ai.operation.name": "invoke_agent"}):
                analysis = analyst.run_report(prompts.REPORT_GOAL, REPORT_TOPICS,
                                              preload=ledger.highlights("report"))
        except AnalysisError:
            # The model answered but its text failed validation. Replaying the same
            # replies would fail the same way, so there is nothing worth resuming.
            shutil.rmtree(journal.path.parent, ignore_errors=True)
            raise
        except BaseException:
            services.log(f"Run {journal.run_id} stopped; its finished model replies are saved. Continue "
                         f"without repeating them: python -m loan_analyst report --resume {journal.run_id}")
            raise
        analysis["tool_results"] = [call for call in ledger.calls if call["origin"] == "model"]
        if model_run.replayed:
            analysis["replayed_model_calls"] = model_run.replayed
            services.log(f"  Reused {model_run.replayed} saved model reply(ies) from the interrupted run.")

    analysis.update(run_id=evidence["run_id"],
                    source_evidence_sha256=evidence["source_evidence_sha256"])
    evidence["ai_analysis"] = {
        key: analysis[key] for key in ("status", "run_id", "source_evidence_sha256")}
    evidence["elapsed_seconds"] = perf_counter() - started

    # 6. Render and serialize everything, then replace the five output files.
    #    A failure before this point leaves earlier report files untouched.
    write_files(options.output_dir, {
        "report.md": render_report(report_data(evidence, analysis), options.sections),
        "aggregates.json": json.dumps(evidence, indent=2, allow_nan=False),
        "snapshot.json": json.dumps(snapshot.to_payload(), separators=(",", ":"), allow_nan=False),
        "ollama_analysis.json": json.dumps(analysis, indent=2, ensure_ascii=False, allow_nan=False),
        "trace.jsonl": tracer.to_jsonl(evidence["run_id"]),
    })

    # The journal only exists to resume an unfinished run.
    if journal is not None:
        shutil.rmtree(journal.path.parent, ignore_errors=True)

    folder = display_path(options.output_dir)
    services.out(f"Report: {folder}/report.md")
    services.out(f"Evidence: {folder}/aggregates.json, {folder}/snapshot.json, "
                 f"{folder}/ollama_analysis.json, {folder}/trace.jsonl")
    services.log(f"Completed in {perf_counter() - started:.1f}s. Reruns replace these five files.")
    return 0


def find_interrupted_run(options, services):
    """Locate the journal to resume and the saved snapshot that run used.

    Resuming is an explicit request to reuse that run's snapshot; MySQL is not
    queried. The snapshot file is still verified against its content hash.
    """
    if options.model is None:
        raise SettingsError("--resume continues the analyst's work; it cannot be combined with --no-ai")
    if options.snapshot is not None:
        raise SettingsError("--resume reuses the interrupted run's own snapshot; omit --snapshot")

    journal = Journal.find(services.runs_dir, options.resume)
    if journal.header.get("kind") != "report" or not journal.header.get("snapshot_id"):
        raise ResumeError("The saved run is not a report run; start again without --resume")
    snapshot_path = services.snapshot_dir / f"snapshot-{journal.header['snapshot_id']}.json"
    return journal, str(snapshot_path)


def run_identity(options, snapshot, client, registry):
    """Everything that must be unchanged for saved model replies to remain valid."""
    metadata = client.metadata or {}
    return {
        "snapshot_sha256": snapshot.content_sha256,
        "database": options.database.sources(),
        "scenario": options.scenario.to_dict(),
        "model": metadata.get("model"),
        "model_digest": metadata.get("model_digest"),
        "prompt_version": prompts.VERSION,
        "tool_definitions_sha256": sha256_json(registry.definitions()),
        "max_tool_rounds": options.model.max_tool_rounds,
        "max_tool_calls": options.model.max_tool_calls,
        "tool_mode": options.model.tool_mode,
        "critic": options.model.critic,
    }


def run_trace(directory, services):
    """Print the step timeline saved with a report."""
    try:
        trace_id, spans = read_trace((directory / "trace.jsonl").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SettingsError(f"{display_path(directory)} does not contain a readable trace.jsonl") from None
    services.out(format_trace(trace_id, spans))
    return 0


def check_model(model, services):
    """Create the model client and verify the installed local model before any slow work."""
    services.log(f"Checking local Ollama model {model.model}...")
    client = services.model_factory(model)
    if model.tool_mode == "native":
        client.preflight()
    else:
        # Schema mode asks for plain JSON, so the model needs no tool-calling support.
        client.preflight(require_tools=False)
    return client


def build_analyst(client, registry, context, ledger, model, services, model_run):
    """One place that turns ModelConfig into an Analyst, for every command."""
    return Analyst(
        client, registry, context, ledger,
        max_rounds=model.max_tool_rounds,
        max_calls=model.max_tool_calls,
        on_event=progress(services.log),
        run=model_run,
        tool_mode=model.tool_mode,
        critic=model.critic,
    )


def prepare_session(options, services):
    """Check the model, obtain one snapshot, and prepare its analyst."""
    client = check_model(options.model, services)

    snapshot = obtain_snapshot(options.database, options.snapshot, services)

    registry = build_registry()
    context = ToolContext(snapshot, options.scenario)
    ledger = Ledger(DEFINITIONS)
    run_calls(registry, context, ledger, [("describe_data", {})])

    # The model-call budget is per question, so each chat turn starts a fresh count.
    analyst = build_analyst(client, registry, context, ledger, options.model, services,
                            Run(client, max_model_calls=options.model.max_model_calls))
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
    # One transcript per session, named for when the session started.
    stamp = datetime.fromisoformat(services.clock()).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

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
        except (AnalysisError, ModelError, BudgetExceeded) as error:
            services.out(f"analyst> Sorry, I could not produce a validated answer: {error}")
            continue
        except KeyboardInterrupt:
            services.out("analyst> Stopped that question. Ask another, or /quit to save and exit.")
            continue

        services.out("\n" + answer_text(result))
        # Save after every answer, so a crash or closed terminal loses nothing.
        save_chat(session, snapshot, options, ledger, stamp)

    if session.turns:
        services.log(f"Saved {display_path(options.output_dir)}/chat-{stamp}.md and .json")
    return 0


def save_chat(session, snapshot, options, ledger, stamp):
    """Write the whole session so far; each save replaces the previous one."""
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
