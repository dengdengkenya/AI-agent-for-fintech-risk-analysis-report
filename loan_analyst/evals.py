"""Evaluate the analyst on a suite of questions, so changes can be compared with numbers.

    python -m loan_analyst eval --snapshot latest

Each case in the suite (evals/cases.toml) is a question plus *structural*
expectations: which tools should or should not be used, which kinds of fact
should be cited, and wording that must not appear. Expectations never contain
data values, so one suite works with any snapshot.

An evaluation always uses a saved snapshot and the local model. It never reads
MySQL and never invents data. Every case starts from a fresh fact ledger, so
cases cannot help each other.

Reading order: load_suite() -> run_eval() -> run_case() -> score_case() ->
summarize() -> scorecard().
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from time import perf_counter
import tomllib

from . import PROJECT_ROOT, prompts
from .analyst import ASK_SECTIONS, AnalysisError, Ledger, cited_ids
from .calculations import DEFINITIONS, build_registry
from .ollama import ModelError
from .runtime import BudgetExceeded, Run
from .settings import SettingsError, require
from .support import display_path, read_json, table, write_files
from .tools import ToolContext
from .workflow import REPORT_CALLS, REPORT_TOPICS, build_analyst, check_model, obtain_snapshot, run_calls

EVAL_FORMAT = "loan-analyst-eval/1"
DEFAULT_SUITE = PROJECT_ROOT / "evals" / "cases.toml"
KINDS = ("ask", "report", "critic")
CASE_KEYS = {"id", "kind", "question", "expect_tools", "forbid_tools", "expect_cited", "forbid_text",
             "min_findings", "statement", "cite", "expect"}
CRITIC_EXPECTATIONS = ("supported", "flagged")


# ===========================================================================
# Suite
# ===========================================================================

def load_suite(path):
    """Read and validate the cases; a mistake in the suite is a usage error."""
    path = Path(path)
    try:
        with open(path, "rb") as stream:
            data = tomllib.load(stream)
    except OSError:
        raise SettingsError(f"Cannot read eval suite {display_path(path)}") from None
    except tomllib.TOMLDecodeError as error:
        raise SettingsError(f"{path.name}: invalid TOML ({error})") from None

    cases = data.get("case")
    require(isinstance(cases, list) and cases, f"{path.name}: needs at least one [[case]]")
    tools = set(build_registry().names())

    seen = set()
    for case in cases:
        label = f"{path.name}: case {case.get('id', '?')}"
        require(isinstance(case.get("id"), str) and case["id"] not in seen, f"{label}: id must be unique text")
        seen.add(case["id"])
        require(not set(case) - CASE_KEYS, f"{label}: unknown key {', '.join(sorted(set(case) - CASE_KEYS))}")
        require(case.get("kind") in KINDS, f"{label}: kind must be one of {', '.join(KINDS)}")
        if case["kind"] == "ask":
            require(isinstance(case.get("question"), str) and case["question"].strip(),
                    f"{label}: an ask case needs a question")
        if case["kind"] == "critic":
            # Critic cases cite fixed definitions, so they need no data and no invented facts.
            cited = case.get("cite")
            require(isinstance(case.get("statement"), str) and case["statement"].strip()
                    and isinstance(cited, list) and cited and all(key in DEFINITIONS for key in cited)
                    and case.get("expect") in CRITIC_EXPECTATIONS,
                    f"{label}: a critic case needs a statement, cite = [definition keys] and "
                    f"expect = {' or '.join(CRITIC_EXPECTATIONS)}")

        for expected in case.get("expect_tools", []):
            require(isinstance(expected, dict) and expected.get("name") in tools
                    and isinstance(expected.get("arguments", {}), dict),
                    f"{label}: expect_tools entries need a known tool name and optional arguments")
        require(all(name in tools for name in case.get("forbid_tools", [])),
                f"{label}: forbid_tools must name known tools")
        for reference in case.get("expect_cited", []):
            source = reference.split(":")[0] if isinstance(reference, str) else None
            require(source in tools or source == "def",
                    f"{label}: expect_cited uses 'tool:fact_key_prefix' or 'def:key'")
        for pattern in case.get("forbid_text", []):
            try:
                re.compile(pattern)
            except (re.error, TypeError):
                raise SettingsError(f"{label}: forbid_text must be regular expressions") from None
    return cases


# ===========================================================================
# Running and scoring one case
# ===========================================================================

def run_case(case, client, snapshot, options, services):
    """Answer one case from a fresh ledger; return (result or None, error, ledger, seconds)."""
    registry = build_registry()
    context = ToolContext(snapshot, options.scenario)
    ledger = Ledger(DEFINITIONS)

    run_calls(registry, context, ledger,
              REPORT_CALLS if case["kind"] == "report" else [("describe_data", {})])
    analyst = build_analyst(client, registry, context, ledger, options.model, services,
                            Run(client, max_model_calls=options.model.max_model_calls))
    started = perf_counter()
    try:
        if case["kind"] == "report":
            result = analyst.run_report(prompts.REPORT_GOAL, REPORT_TOPICS, preload=ledger.highlights("report"))
        else:
            result = analyst.run(case["question"], sections=ASK_SECTIONS, preload=ledger.highlights("report"))
        error = None
    except (AnalysisError, ModelError, BudgetExceeded) as failure:
        result, error = None, {"type": type(failure).__name__, "message": str(failure)[:300]}
    return result, error, ledger, perf_counter() - started


def run_critic_case(case, client, snapshot, options, services):
    """Show the critic one seeded statement and compare its verdict with the expected one."""
    ledger = Ledger(DEFINITIONS)
    analyst = build_analyst(client, build_registry(), ToolContext(snapshot, options.scenario), ledger,
                            options.model, services, Run(client, max_model_calls=options.model.max_model_calls))
    item = {"text": case["statement"], "evidence_ids": [f"def.{key}" for key in case["cite"]]}
    facts = ledger.select(item["evidence_ids"])

    score = {"id": case["id"], "kind": "critic", "answered": True, "failures": [], "error": None, "kept": 0,
             "withheld": 0, "repaired": False, "tool_calls": 0, "invalid_tool_calls": 0, "expected_tools": 0,
             "expected_tools_found": 0, "relevant_tool_calls": 0}
    started = perf_counter()
    try:
        verdict = "flagged" if analyst.critique("eval", {"findings": [item]}, facts) else "supported"
    except (ModelError, BudgetExceeded) as failure:
        verdict = None
        score["error"] = {"type": type(failure).__name__, "message": str(failure)[:300]}

    if verdict != case["expect"]:
        score["failures"].append(f"critic said {verdict or 'nothing'}, expected {case['expect']}")
    usage = [exchange["usage"] for exchange in analyst.exchanges]
    score.update(
        seconds=round(perf_counter() - started, 2), passed=not score["failures"], verdict=verdict,
        model_calls=len(usage),
        input_tokens=sum(item.get("prompt_eval_count") or 0 for item in usage),
        output_tokens=sum(item.get("eval_count") or 0 for item in usage))
    return score


def includes(expected, actual):
    """True when every expected argument is present with the same value."""
    return all(key in actual and actual[key] == value for key, value in expected.items())


def cited_sources(answer, ledger):
    """Turn cited fact IDs into snapshot-independent names: 'tool:key' or 'def:key'."""
    tool_by_call = {call["id"]: call["tool"] for call in ledger.calls}
    sources = []
    for fact_id in cited_ids(answer):
        prefix, _, key = fact_id.partition(".")
        sources.append(f"{tool_by_call.get(prefix, prefix)}:{key}")
    return sources


def score_case(case, result, error, ledger, seconds):
    """Compare one outcome with the case's expectations; every failed check is listed."""
    score = {"id": case["id"], "kind": case["kind"], "seconds": round(seconds, 2), "answered": result is not None,
             "failures": [], "error": error, "kept": 0, "withheld": 0, "repaired": False, "model_calls": 0,
             "input_tokens": 0, "output_tokens": 0, "tool_calls": 0, "invalid_tool_calls": 0,
             "expected_tools": len(case.get("expect_tools", [])), "expected_tools_found": 0,
             "relevant_tool_calls": 0}
    if result is None:
        score["failures"].append(f"no validated answer ({error['type']})")
        score["passed"] = False
        return score

    answer = result["answer"]
    ran = [call for call in result["tool_calls"] if "id" in call and not call.get("repeated")]
    score.update(
        kept=sum(len(items) for items in answer.values()),
        withheld=len(result["withheld"]),
        repaired=result["attempts"] > 1,
        model_calls=sum(1 for exchange in result["exchanges"] if not exchange.get("replayed")),
        input_tokens=result["usage"].get("prompt_eval_count", 0),
        output_tokens=result["usage"].get("eval_count", 0),
        tool_calls=len(ran),
        invalid_tool_calls=sum(1 for call in result["tool_calls"] if "error" in call),
    )

    expected_tools = case.get("expect_tools", [])
    for expected in expected_tools:
        found = any(call["tool"] == expected["name"] and includes(expected.get("arguments", {}), call["arguments"])
                    for call in ran)
        if found:
            score["expected_tools_found"] += 1
        else:
            wanted = json.dumps(expected.get("arguments", {}), sort_keys=True)
            score["failures"].append(f"expected tool {expected['name']} {wanted} was not called")
    # Precision: of the calls the model made, how many were ones the case asked for.
    score["relevant_tool_calls"] = sum(
        1 for call in ran if any(call["tool"] == expected["name"] for expected in expected_tools))

    for name in case.get("forbid_tools", []):
        if any(call["tool"] == name for call in ran):
            score["failures"].append(f"forbidden tool {name} was called")

    sources = cited_sources(answer, ledger)
    for reference in case.get("expect_cited", []):
        if not any(source.startswith(reference) for source in sources):
            score["failures"].append(f"no cited fact matches {reference}")

    text = " ".join(item["text"] for items in answer.values() for item in items)
    for pattern in case.get("forbid_text", []):
        if re.search(pattern, text, re.IGNORECASE):
            score["failures"].append(f"answer matches forbidden wording /{pattern}/")

    minimum = case.get("min_findings", 0)
    if len(answer.get("findings", [])) < minimum:
        score["failures"].append(f"fewer than {minimum} validated findings")

    score["passed"] = not score["failures"]
    return score


# ===========================================================================
# Summary and scorecard
# ===========================================================================

def _rate(numerator, denominator):
    return numerator / denominator if denominator else None


def summarize(scores):
    """Suite-level rates. A rate with nothing to measure is None (shown as N/A), never zero."""
    count = len(scores)
    critic_cases = [score for score in scores if score["kind"] == "critic"]
    statements = sum(score["kept"] + score["withheld"] for score in scores)
    requested = sum(score["tool_calls"] + score["invalid_tool_calls"] for score in scores)
    with_expectations = [score for score in scores if score["expected_tools"]]
    return {
        "cases": count,
        "pass_rate": _rate(sum(score["passed"] for score in scores), count),
        "answered_rate": _rate(sum(score["answered"] for score in scores), count),
        "tool_recall": _rate(sum(score["expected_tools_found"] for score in with_expectations),
                             sum(score["expected_tools"] for score in with_expectations)),
        "tool_precision": _rate(sum(score["relevant_tool_calls"] for score in with_expectations),
                                sum(score["tool_calls"] for score in with_expectations)),
        "invalid_tool_call_rate": _rate(sum(score["invalid_tool_calls"] for score in scores), requested),
        "withheld_rate": _rate(sum(score["withheld"] for score in scores), statements),
        "repair_rate": _rate(sum(score["repaired"] for score in scores), count),
        "critic_agreement": _rate(sum(score["passed"] for score in critic_cases), len(critic_cases)),
        "model_calls_per_case": _rate(sum(score["model_calls"] for score in scores), count),
        "input_tokens_per_case": _rate(sum(score["input_tokens"] for score in scores), count),
        "output_tokens_per_case": _rate(sum(score["output_tokens"] for score in scores), count),
        "seconds_per_case": _rate(sum(score["seconds"] for score in scores), count),
    }


# (summary key, label, is a 0..1 rate, higher is better)
METRICS = (
    ("pass_rate", "Cases passed", True, True),
    ("answered_rate", "Validated answer produced", True, True),
    ("tool_recall", "Expected tools called (recall)", True, True),
    ("tool_precision", "Tool calls that were expected (precision)", True, True),
    ("invalid_tool_call_rate", "Invalid tool requests", True, False),
    ("withheld_rate", "Statements withheld by evidence checks", True, False),
    ("repair_rate", "Cases needing an answer repair", True, False),
    ("critic_agreement", "Critic verdicts matching the seeded cases", True, True),
    ("model_calls_per_case", "Model calls per case", False, False),
    ("input_tokens_per_case", "Input tokens per case", False, False),
    ("output_tokens_per_case", "Output tokens per case", False, False),
    ("seconds_per_case", "Seconds per case", False, False),
)


def _show(value, is_rate):
    if value is None:
        return "N/A"
    return f"{value:.1%}" if is_rate else f"{value:,.1f}"


def _change(value, before, is_rate, higher_is_better):
    if value is None or before is None:
        return "N/A"
    difference = value - before
    if abs(difference) < 1e-9:
        return "same"
    verdict = "better" if (difference > 0) == higher_is_better else "worse"
    amount = f"{difference * 100:+.1f} pp" if is_rate else f"{difference:+,.1f}"
    return f"{amount} ({verdict})"


def scorecard(results, baseline=None):
    """The Markdown summary a person reads; results.json holds the detail."""
    summary = results["summary"]
    lines = [
        f"# Analyst evaluation: {results['model'].get('model')}", "",
        f"Suite `{results['suite']}` ({summary['cases']} case runs, {results['repeat']} repeat(s)) on snapshot "
        f"`{results['snapshot_id']}`, prompt `{results['prompt_version']}`, generated "
        f"{results['generated_at_utc']}.", "",
        "Expectations are structural (tools, cited fact kinds, wording). They do not prove that an "
        "interpretation is correct.", "",
    ]

    headers, rows = ["Metric", "Result"], []
    if baseline:
        headers += ["Baseline", "Change"]
    for key, label, is_rate, higher_is_better in METRICS:
        row = [label, _show(summary[key], is_rate)]
        if baseline:
            before = baseline["summary"].get(key)
            row += [_show(before, is_rate), _change(summary[key], before, is_rate, higher_is_better)]
        rows.append(row)
    lines += [table(headers, rows, ["---"] + ["---:"] * (len(headers) - 1)), ""]
    if baseline:
        lines += [f"Baseline: {baseline['model'].get('model')}, prompt `{baseline['prompt_version']}`, "
                  f"snapshot `{baseline['snapshot_id']}`.", ""]

    lines += ["## Cases", ""]
    case_rows = []
    for score in results["cases"]:
        case_rows.append([f"`{score['id']}`", "pass" if score["passed"] else "FAIL", score["tool_calls"],
                          f"{score['kept']}/{score['withheld']}", f"{score['seconds']:.1f}",
                          "; ".join(score["failures"]) or "-"])
    lines.append(table(["Case", "Result", "Tools run", "Kept/withheld", "Seconds", "Failed checks"], case_rows,
                       ["---", "---", "---:", "---:", "---:", "---"]))
    return "\n".join(lines) + "\n"


# ===========================================================================
# The eval command
# ===========================================================================

def run_eval(options, services, *, suite=DEFAULT_SUITE, repeat=1, baseline=None):
    """Run every case, print the scorecard and save it with the detailed results."""
    if options.snapshot is None:
        raise SettingsError("eval needs a saved snapshot: add --snapshot latest or --snapshot PATH")
    cases = load_suite(suite)

    baseline_results = None
    if baseline is not None:
        try:
            baseline_results = read_json(Path(baseline).read_text(encoding="utf-8"))
            if baseline_results.get("format") != EVAL_FORMAT:
                raise ValueError
        except (OSError, ValueError, AttributeError):
            raise SettingsError(f"{display_path(baseline)} is not a readable eval results.json") from None

    client = check_model(options.model, services)
    snapshot = obtain_snapshot(options.database, options.snapshot, services)

    scores, artifacts = [], {}
    for attempt in range(1, repeat + 1):
        for case in cases:
            if case["kind"] == "critic":
                result, seconds = None, None
                score = run_critic_case(case, client, snapshot, options, services)
                seconds = score["seconds"]
            else:
                result, error, ledger, seconds = run_case(case, client, snapshot, options, services)
                score = score_case(case, result, error, ledger, seconds)
            score["attempt"] = attempt
            scores.append(score)
            services.log(f"  {case['id']} (run {attempt}): {'pass' if score['passed'] else 'FAIL'} "
                         f"in {seconds:.1f}s" + (f" - {score['failures'][0]}" if score["failures"] else ""))
            if result is not None:
                artifacts[f"case-{case['id']}-{attempt}.json"] = json.dumps(
                    result, indent=2, ensure_ascii=False, allow_nan=False)

    results = {
        "format": EVAL_FORMAT,
        "generated_at_utc": services.clock(),
        "suite": display_path(suite),
        "repeat": repeat,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_sha256": snapshot.content_sha256,
        "scenario": options.scenario.to_dict(),
        "prompt_version": prompts.VERSION,
        "model": client.metadata or {},
        "summary": summarize(scores),
        "cases": scores,
    }
    card = scorecard(results, baseline_results)

    stamp = datetime.fromisoformat(results["generated_at_utc"]).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    model_name = re.sub(r"[^A-Za-z0-9._-]+", "-", options.model.model).strip("-") or "model"
    folder = options.output_dir / f"{stamp}-{model_name}"
    write_files(folder, {
        "scorecard.md": card,
        "results.json": json.dumps(results, indent=2, ensure_ascii=False, allow_nan=False),
        **artifacts,
    })

    services.out(card)
    services.log(f"Saved {display_path(folder)}/scorecard.md and results.json. Compare a later run with "
                 f"--compare-with {display_path(folder)}/results.json")
    return 0
