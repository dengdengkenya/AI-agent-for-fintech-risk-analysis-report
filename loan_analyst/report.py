"""Composable Markdown report built from JSON-safe run data.

Every section is a function of the run data, so sections can be reordered, removed or
added (see SECTIONS), and a report can be re-rendered from its saved artifacts.
"""

from .analyses import cutoffs, overview, rejected, segments
from .formatting import pct, plain, table, wrap
from .settings import Scenario, SettingsError


class ReportView:
    """Lookups over run data plus the citation numbering shared by all sections."""

    def __init__(self, data):
        self.data = data
        self.calls = data["calls"]
        self.analysis = data["analysis"]
        self.scenario = Scenario(**data["settings"]["scenario"])
        self.citations = _CitationNumbers()
        self.cite = self.citations.markdown

    def result(self, tool, **arguments):
        """Result of the first report call of `tool` whose arguments include these values."""
        for call in self.calls:
            if (call["origin"] == "report" and call["tool"] == tool
                    and all(call["arguments"].get(key) == value for key, value in arguments.items())):
                return call["result"]
        return None

    def fact(self, ref):
        return self.analysis.get("facts", {}).get(ref) or self.data.get("facts", {}).get(ref, "")


def _title(view):
    data = view.data
    return "\n".join(["# Score cutoff report: growth versus principal losses", "",
                      "**Historical MySQL aggregate analysis**", "",
                      f"Run `{data['run_id']}`, generated {data['generated_at_utc']}."])


def _summary(view):
    data, scenario, analysis = view.data, view.scenario, view.analysis
    snapshot, database = data["snapshot"], data["settings"]["database"]["database"]
    accepted, rejected_source = snapshot["sources"]["accepted"], snapshot["sources"]["rejected"]
    commentary = ("disabled with `--no-ai`" if analysis["status"] == "disabled" else
                  f"local model {plain(analysis['model'])}, evidence-checked; requires review")
    lines = [
        f"- **Data:** {database}.{accepted['table']} ({accepted['total_rows']:,} rows) and "
        f"{database}.{rejected_source['table']} ({rejected_source['total_rows']:,} rows).",
        f"- **Snapshot:** `{snapshot['snapshot_id']}`, read {snapshot['read_started_at_utc']} to "
        f"{snapshot['read_completed_at_utc']}"
        + (" in a live MySQL read" if snapshot["origin"] == "mysql" else f", loaded from {plain(snapshot['origin'])}")
        + ". This is the read time, not the repayment performance cutoff.",
        f"- **Cohort:** {scenario.cohort}; loans by issue month, rejected applications by application month.",
        f"- **Cutoffs:** {', '.join(map(str, scenario.cutoffs))}; baseline **{scenario.baseline}** "
        "(illustrative, not a verified policy).",
        "- **Loss ceiling:** " + (pct(scenario.max_loss_rate) if scenario.max_loss_rate is not None
                                 else "none supplied, so no cutoff is selected") + ".",
        f"- **Commentary:** {commentary}.",
    ]
    if scenario.assume_comparable_scores:
        lines.append(f"- **Explicit assumptions:** comparable scores, {pct(scenario.take_up_rate)} take-up, "
                     f"{pct(scenario.rejected_loss_rate)} lifetime loss on new rejected lending.")
    return "\n".join(lines)


def _analyst(view):
    analysis = view.analysis
    if analysis["status"] == "disabled":
        return "## Analyst summary\n\nDisabled explicitly with `--no-ai`; all tables use MySQL results."
    own_calls = sum(1 for call in analysis.get("tool_calls", []) if "id" in call)
    lines = ["## Analyst summary", "",
             f"> Written by the local model **{plain(analysis['model'])}** from the cited facts, after {own_calls} "
             "tool call(s) of its own. Every statement passed evidence-reference and number checks; interpretations "
             "are model-generated and require review. Tables and cutoff selection are calculated by Python.", ""]
    if analysis.get("review"):
        lines += ["Wording reviewed against the database evidence. The original model draft and review notes "
                  "are preserved in `ollama_analysis.json`.", ""]
    answer = analysis["answer"]
    for item in answer["answer"]:
        lines += [f"**{plain(item['text'])}** {view.cite(item['evidence_ids'])}", ""]
    for key, heading in ANSWER_HEADINGS:
        if answer.get(key):
            lines += [f"**{heading}**", ""]
            lines += [f"- {plain(item['text'])} {view.cite(item['evidence_ids'])}" for item in answer[key]]
            lines.append("")
    if analysis.get("withheld"):
        lines.append(f"_{len(analysis['withheld'])} model statement(s) failed evidence validation and were "
                     "withheld; they are listed in `ollama_analysis.json`._")
    return "\n".join(lines).rstrip()


def _glance(view):
    result = view.result("compare_score_cutoffs")
    return result and "## At a glance (calculated)\n\n" + cutoffs.glance(result, view.scenario)


def _cutoffs(view):
    result = view.result("compare_score_cutoffs")
    return result and "## Accepted loans: historical cutoff replay\n\n" + cutoffs.cutoff_table(result, view.scenario)


def _score_bands(view):
    result = view.result("segment_performance", dimension="score_band")
    return result and ("## Loss by score band\n\nEach band holds only the loans between adjacent cutoffs, so raising "
                       "the cutoff by one step removes exactly that band.\n\n" + segments.section(result))


def _vintages(view):
    result = view.result("segment_performance", dimension="issue_year")
    return result and ("## Performance by issue year\n\nA low terminal share means most of that year's loans were "
                       "still open in the source data, so its loss proxy covers only resolved loans.\n\n"
                       + segments.section(result))


def _segments(view):
    parts = [segments.section(result) for result in (view.result("segment_performance", dimension=dimension)
                                                     for dimension in ("grade", "term")) if result]
    return parts and "## Performance by grade and term\n\n" + "\n\n".join(parts)


def _rejected(view):
    result = view.result("compare_score_cutoffs")
    if not result:
        return ""
    lines = ["## Rejected applications: numeric screen", "",
             "The source dictionary identifies rejected Risk_Score as FICO before November 5, 2013 and VantageScore "
             "after that date, without specifying the VantageScore version or range. Risk_Score is therefore not "
             "generally comparable to accepted FICO. This is a numeric score screen only; it is not an approval rate "
             "or measured lending opportunity. Rejected applicants have no loan outcomes.", "",
             cutoffs.rejected_screen(result, view.scenario)]
    eras = view.result("rejected_applications", by="score_era")
    if eras:
        lines += ["", "### By score era", "", rejected.section(eras)]
    return "\n".join(lines)


def _coverage(view):
    parts = []
    described, compared = view.result("describe_data"), view.result("compare_score_cutoffs")
    if described:
        parts.append(overview.coverage_section(described))
    if compared:
        parts.append(cutoffs.coverage_by_cutoff(compared, view.scenario))
    return parts and "## Coverage and exclusions\n\n" + "\n\n".join(parts)


def _next_decision(view):
    return ("## Interpretation and next decision\n\nAgree a loss ceiling and a comparable, sufficiently seasoned "
            "origination window before selecting a cutoff. For an expansion case, first establish the rejected score "
            "scale and test alternative take-up and loss assumptions. This analysis does not estimate revenue, "
            "funding costs, operating costs or profit.")


def _arguments(arguments):
    return ", ".join(f"{key}={value}" for key, value in arguments.items()) or "session settings"


def _investigation(view):
    # Arguments passed schema validation (numbers, months, enums), so code spans need no escaping.
    rows = [[call["id"], "analyst" if call["origin"] == "model" else "report", f"`{call['tool']}`",
             f"`{_arguments(call['arguments'])}`", plain(call["title"])] for call in view.calls]
    failed = [call for call in view.analysis.get("tool_calls", []) if "error" in call]
    lines = ["## How this analysis was produced", "",
             "Report steps run automatically; analyst steps were chosen by the local model. Every step is an "
             "allowlisted tool computed in Python over the same read-only snapshot, so no step queried MySQL again.",
             "", table(["Step", "By", "Tool", "Arguments", "Result"], rows, ["---"] * 5)]
    if failed:
        lines += ["", f"The model also made {len(failed)} invalid tool request(s); each was rejected before execution "
                      "and is recorded in `ollama_analysis.json`."]
    return "\n".join(lines)


def _evidence(view):
    if not view.citations:
        return ""
    lines = ["## Evidence cited", ""]
    for ref, number in view.citations.items():
        lines += [f'<a id="evidence-{number}"></a>**[{number}]** `{ref}`: {plain(view.fact(ref))}', ""]
    return "\n".join(lines).rstrip()


def _definitions(view):
    minimum = view.scenario.min_outcomes
    bullets = [
        "Growth is the change in historically accepted loan count or funded principal retained by a score rule. It "
        "cannot recover historical approvals below the original underwriting policy. Application rows are not "
        "deduplicated people.",
        "Accepted score = midpoint of original `fico_range_low` and `fico_range_high`, inclusive cutoff. Both "
        "endpoints must be integers in 300–850 and ordered. Rejected scores outside 300–850, decimals, missing, or "
        "invalid text are excluded; score-scale compatibility remains unverified even inside that range.",
        "Terminal outcomes = exactly Fully Paid or Charged Off. Current, late, Default, legacy policy statuses, and "
        "unknown statuses are excluded from performance rates. Terminal-only selection can bias loss estimates; this "
        "is not a fixed-horizon default model and open loans are not treated as zero-loss outcomes.",
        "Charge-off rate = terminal Charged Off count / all terminal count. Principal-loss proxy = sum of "
        "max(funded_amnt − total_rec_prncp − recoveries, 0) for complete Charged Off loans / total funded_amnt of "
        "complete terminal loans. Fully Paid contributes zero loss. Recoveries are gross; costs, interest, fees and "
        "recovery timing are excluded. This is an unrecovered-principal proxy, not accounting net charge-offs.",
        "Complete financial data requires positive funded amount, nonnegative principal received no greater than "
        "funded amount, and nonnegative recoveries. Missing or malformed values are excluded, never silently made "
        "zero. Counts can therefore include loans excluded from dollar totals. Monetary text accepts up to four "
        "decimal places.",
        f"Rates are N/A below {minimum} relevant outcomes or with zero denominator. The minimum is a reporting "
        "threshold, not a statistical guarantee or privacy threshold. The optional historical candidate also "
        "requires 100% terminal financial coverage.",
        "Dates filter issue/application cohorts, not repayment observation dates. The CSV performance snapshot does "
        "not establish performance as of the execution date. Different calendar coverage, seasoning, selection bias, "
        "incomplete imports, or duplicate imports can distort comparisons. No import completeness or deduplication "
        "is certified by this report.",
        "MySQL is read once in a consistent, read-only transaction. Only grouped counts and sums by month, score and "
        "optional grade/term reach Python; no borrower rows, names, IDs, descriptions or contact data are exported. "
        "The local model sees only calculated facts and can run only allowlisted in-memory tools. Aggregates can "
        "still reveal small groups; this report does not provide anonymization or differential privacy.",
    ]
    return ("## Definitions and limitations\n\n<details>\n<summary>Metric definitions, exclusions and data "
            "limitations</summary>\n\n" + "\n".join(f"- {bullet}" for bullet in bullets) + "\n\n</details>")


def _reproducibility(view):
    data = view.data
    names = data["settings"]["database"]
    return "\n".join([
        "## Reproducibility", "",
        f"Source: {names['database']}.{names['accepted_table']} and {names['database']}.{names['rejected_table']}. "
        f"Source evidence hash `{data['source_evidence_sha256']}`.",
        "",
        "- `aggregates.json`: settings, snapshot provenance, every analysis result and fact, and the source evidence "
        "hash.",
        "- `snapshot.json`: the grouped MySQL aggregates and SQL used for this run; reuse it with "
        "`--snapshot <this folder>/snapshot.json` to rerun without scanning.",
        "- `ollama_analysis.json`: exact local model requests, tool calls, validated answer, withheld statements, "
        "model digest and usage, or an explicit disabled status.",
        "",
        "The source dictionary is `data/LCDataDictionary.xlsx`. Use `--print-sql` to inspect queries without "
        "connecting.",
    ])


SECTIONS = {
    "summary": _summary, "analyst": _analyst, "at_a_glance": _glance, "cutoffs": _cutoffs,
    "score_bands": _score_bands, "vintages": _vintages, "segments": _segments, "rejected": _rejected,
    "coverage": _coverage, "next_decision": _next_decision, "investigation": _investigation,
    "evidence": _evidence, "definitions": _definitions, "reproducibility": _reproducibility,
}
DEFAULT_SECTIONS = tuple(SECTIONS)


def check_sections(sections):
    unknown = [name for name in sections if name not in SECTIONS]
    if unknown or not sections:
        raise SettingsError(f"Unknown report section(s): {', '.join(unknown) or 'none given'}. "
                            f"Choose from: {', '.join(SECTIONS)}")
    return tuple(sections)


def render_report(data, sections=DEFAULT_SECTIONS):
    """Render selected sections, assigning evidence numbers as citations appear."""
    view = ReportView(data)
    sections = check_sections(sections)
    # The evidence list numbers citations made by other sections, so it renders last.
    rendered = {}
    for name in sections:
        if name != "evidence":
            render_section = SECTIONS[name]
            rendered[name] = render_section(view)
    if "evidence" in sections:
        rendered["evidence"] = _evidence(view)

    # Display order still follows the user's selection, even for the evidence list.
    parts = [_title(view)]
    for name in sections:
        if rendered[name]:
            parts.append(rendered[name])
    return "\n\n".join(parts) + "\n"


ANSWER_HEADINGS = (("findings", "What the results show"), ("caveats", "Caveats"),
                   ("next_steps", "Next analytical steps"))


class _CitationNumbers(dict):
    """Map each fact ID to its citation number, assigned in order of first use."""

    def markdown(self, ids):
        return " ".join(f"[\\[{n}\\]](#evidence-{n})" for n in map(self.number, ids))

    def text(self, ids):
        return "".join(f"[{n}]" for n in map(self.number, ids))

    def number(self, ref):
        if ref not in self:
            self[ref] = len(self) + 1
        return self[ref]


def answer_markdown(question, analysis):
    """A standalone Markdown answer for `ask` and `chat` transcripts."""
    numbers = _CitationNumbers()
    cite = numbers.markdown
    answer = analysis["answer"]
    lines = [f"## {plain(question)}", ""]
    lines += [f"**{plain(item['text'])}** {cite(item['evidence_ids'])}" for item in answer["answer"]]
    for key, heading in ANSWER_HEADINGS:
        if answer.get(key):
            lines += ["", f"**{heading}**", ""]
            lines += [f"- {plain(item['text'])} {cite(item['evidence_ids'])}" for item in answer[key]]
    lines += ["", "**Evidence**", ""]
    lines += [f'<a id="evidence-{n}"></a>[{n}] `{ref}`: {plain(analysis["facts"][ref])}  ' for ref, n in numbers.items()]
    lines += ["", f"_Model-generated by local {plain(analysis['model'])}; every statement passed evidence and number "
                  "checks. Review before relying on it._"]
    return "\n".join(lines) + "\n"


def answer_text(analysis, width=100):
    """Readable terminal rendering with numbered evidence."""
    numbers = _CitationNumbers()
    cite = numbers.text
    answer = analysis["answer"]
    lines = [wrap(f"{item['text']} {cite(item['evidence_ids'])}", width=width) for item in answer["answer"]]
    for key, heading in ANSWER_HEADINGS:
        if answer.get(key):
            lines += ["", f"{heading}:"]
            lines += [wrap(f"{item['text']} {cite(item['evidence_ids'])}", "  - ", width) for item in answer[key]]
    lines += ["", "Evidence:"]
    lines += [wrap(f"{ref}: {analysis['facts'][ref]}", f"  [{n}] ", width) for ref, n in numbers.items()]
    ran = [call for call in analysis.get("tool_calls", []) if "id" in call]
    if ran:
        lines += ["", wrap("Tools the analyst ran: " + "; ".join(
            f"{call['id']} {call['tool']}({_arguments(call['arguments'])})" for call in ran), width=width)]
    if analysis.get("withheld"):
        lines.append(f"{len(analysis['withheld'])} statement(s) withheld after failing evidence checks.")
    lines.append(f"Model-generated by local {analysis['model']}; numbers checked against cited facts. "
                 "Review before relying on it.")
    return "\n".join(lines)
