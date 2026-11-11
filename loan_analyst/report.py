"""Render the human-readable report and terminal answers.

This is the only file that decides how a number looks. Each report section is a
function of the run data, so sections can be reordered, removed or re-rendered
from saved artifacts. Tables are built here rather than in the calculation file.

The report has three kinds of sections:
* calculated tables (cutoffs, score bands, segments, rejected screens)
* model commentary, already checked by ``analyst.py``
* fixed explanations and evidence lists
"""

from .calculations import GROUPINGS, TITLES, baseline_row, ratio, screen_at_baseline
from .settings import Scenario, SettingsError
from .support import number, pct, plain, signed, table, usd, wrap


# ===========================================================================
# Tables and summaries shown in the report
# ===========================================================================

def cutoff_glance(result, scenario):
    """Calculated summary bullets for the cutoff section; no model text."""
    rows = result["scenarios"]
    baseline = baseline_row(result)

    lines = [f"- The comparison baseline is **{scenario.baseline}** (a scenario assumption, not a verified current "
             f"policy). It retains **{baseline['accepted']['rows']:,} accepted loans** and "
             f"**{usd(baseline['accepted']['amount_sum'])}** of valid funded principal. Its historical "
             f"principal-loss proxy is **{pct(baseline['loss_rate'])}** on complete terminal outcomes."]

    for label, row in (("Lowest tested cutoff", rows[0]), ("Highest tested cutoff", rows[-1])):
        if row["cutoff"] != scenario.baseline:
            lines.append(f"- {label} **{row['cutoff']}** retains **{row['accepted']['rows']:,} loans**, a change of "
                         f"**{row['accepted_count_change']:+,}** ({pct(row['accepted_growth_rate'])}) versus "
                         f"baseline, with a historical loss proxy of **{pct(row['loss_rate'])}**.")

    if scenario.max_loss_rate is None:
        lines.append("- No risk ceiling was supplied, so no cutoff is selected. "
                     "Use the tables to agree a volume/loss tradeoff.")
    elif result["candidate_cutoff"] is None:
        lines.append(f"- No tested cutoff clears the supplied **{pct(scenario.max_loss_rate)}** historical loss "
                     "ceiling with enough outcomes and complete terminal financial data.")
    else:
        lines.append(f"- **{result['candidate_cutoff']}** retains the most historical accepted loans among tested "
                     f"cutoffs with a loss proxy at or below **{pct(scenario.max_loss_rate)}**, enough outcomes, "
                     "and complete terminal financial data. This is a historical screening candidate, not a "
                     "lending-policy recommendation; hypothetical expansion losses are not tested against it.")

    if not any(row["accepted"]["rows"] for row in rows):
        lines.append("- **No accepted loans with usable scores meet the tested cutoffs in this cohort.** "
                     "There is no supported accepted growth/loss comparison.")
    elif not any(row["loss_rate"] is not None for row in rows):
        lines.append("- **No tested cutoff has enough complete terminal outcomes for a loss rate.** "
                     "Volume alone cannot establish the risk tradeoff.")
    return "\n".join(lines)


def cutoff_table(result, scenario):
    """The cumulative cutoff comparison table."""
    rows = []
    for row in result["scenarios"]:
        delta = "N/A" if row["loss_rate_change_pp"] is None else f"{row['loss_rate_change_pp']:+.2f}"
        marker = " (baseline)" if row["cutoff"] == scenario.baseline else ""
        rows.append([
            f"{row['cutoff']}{marker}",
            f"{row['accepted']['rows']:,}",
            f"{row['accepted_count_change']:+,}",
            usd(row["accepted"]["amount_sum"]),
            pct(row["terminal_share"]),
            pct(row["chargeoff_rate"]),
            pct(row["loss_rate"]),
            delta,
        ])

    return ("Each row retains loans whose original FICO midpoint is at or above the cutoff. Counts include open "
            "loans; losses use only Fully Paid and Charged Off loans. These populations differ, so the table is a "
            "tradeoff screen, not a forecast.\n\n"
            + table(["Cutoff", "Retained loans", "Change vs baseline", "Funded principal", "Terminal share",
                     "Charge-off rate", "Loss proxy", "Loss change (pp)"], rows))


def rejected_screen(result, scenario):
    """Rejected numeric screens at each cutoff, plus optional expansion assumptions."""
    rows = []
    for row in result["scenarios"]:
        rows.append([
            row["cutoff"],
            f"{row['rejected']['rows']:,}",
            usd(row["rejected"]["amount_sum"]),
            f"{row['rejected']['amount_rows']:,}",
        ])
    lines = [table(["Numeric threshold", "Screened applications", "Valid requested amount",
                    "Applications with valid amount"], rows)]

    if scenario.assume_comparable_scores:
        expansion_rows = []
        for row in result["scenarios"]:
            expansion_rows.append([
                row["cutoff"],
                number(row["assumed_new_loans"]),
                usd(row["assumed_new_principal"]),
                usd(row["assumed_new_loss"]),
                signed(row["hypothetical_count_change"]),
            ])
        lines += [
            "",
            "#### Explicit hypothetical expansion",
            "",
            f"Assumes comparable scores, **{pct(scenario.take_up_rate)}** take-up of valid requested amounts, "
            f"full requested funding, and **{pct(scenario.rejected_loss_rate)}** lifetime principal loss on "
            "that new lending. Neither take-up nor losses are estimated from rejected outcomes.",
            "",
            table(["Cutoff", "Assumed new loans", "Assumed new principal", "Assumed new losses",
                   "Total loan change vs accepted-only baseline"], expansion_rows),
            "",
            "New-loss assumptions and historical accepted losses have different observation windows; they are "
            "not added into a portfolio loss forecast. Expansion includes all screened rejects at each cutoff, "
            "including the baseline; it is not solely the effect of lowering a threshold.",
        ]
    else:
        lines += ["", "No rejected loan growth or loss forecast is made because rejected applications have no "
                      "observed repayment outcomes."]
    return "\n".join(lines)


def cutoff_coverage_table(result):
    """Coverage and loss-denominator detail for each cutoff."""
    rows = []
    for row in result["scenarios"]:
        rows.append([
            row["cutoff"],
            f"{row['accepted']['rows'] - row['accepted']['terminal_rows']:,}",
            f"{row['accepted']['performance_rows']:,}",
            pct(row["loss_data_coverage"]),
            usd(row["accepted"]["performance_principal"]),
            usd(row["accepted"]["loss_amount"]),
        ])
    return table(["Cutoff", "Excluded nonterminal/other outcomes", "Complete terminal outcomes",
                  "Terminal financial coverage", "Loss denominator", "Observed loss proxy dollars"], rows)


def overview_coverage_table(result):
    """Source row counts and exclusion counts from the data-overview result."""
    rows = []
    for kind, source_quality in result["quality"].items():
        rows.append([
            kind,
            f"{source_quality['total_rows']:,}",
            f"{source_quality['in_scope_rows']:,}",
            f"{source_quality['invalid_date_rows']:,}",
            f"{source_quality['outside_period_rows']:,}",
            f"{source_quality['invalid_score_rows']:,}",
            f"{source_quality['invalid_amount_rows']:,}",
        ])
    lines = [table(["Source", "Table rows", "In cohort", "Invalid date", "Outside cohort",
                    "Invalid score in cohort", "Invalid amount in cohort"], rows,
                   ["---", *["---:"] * 6]), ""]
    for kind, source_quality in result["quality"].items():
        lines.append(f"{kind.capitalize()} observed cohort bounds: "
                     f"{source_quality['first_month'] or 'unavailable'} "
                     f"to {source_quality['last_month'] or 'unavailable'}.")
    return "\n".join(lines)


def segment_table(result):
    """One segment breakdown (score band, issue year, grade or term)."""
    rows = []
    for segment in result["segments"]:
        accepted = segment["accepted"]
        rows.append([
            segment["segment"],
            f"{accepted['rows']:,}",
            pct(segment["loan_share"]),
            usd(accepted["amount_sum"]),
            pct(segment["terminal_share"]),
            pct(segment["chargeoff_rate"]),
            pct(segment["loss_rate"]),
            pct(segment["loss_data_coverage"]),
        ])
    return table([TITLES[result["dimension"]],
                  "Loans", "Share", "Funded principal", "Terminal share", "Charge-off rate",
                  "Loss proxy", "Financial coverage"], rows, ["---", *["---:"] * 7])


def vintage_table(result):
    """Issue vintages with their seasoning status."""
    rows = []
    for vintage in result["vintages"]:
        rows.append([
            vintage["vintage"],
            f"{vintage['accepted']['rows']:,}",
            pct(vintage["terminal_share"]),
            "yes" if vintage["seasoned"] else "no",
            pct(vintage["chargeoff_rate"]),
            pct(vintage["loss_rate"]),
        ])
    return table(["Vintage", "Loans", "Terminal share", "Seasoned", "Charge-off rate", "Loss proxy"], rows,
                 ["---", "---:", "---:", "---", "---:", "---:"])


def concentration_table(result):
    """Each segment's share of funded principal and of unrecovered principal."""
    rows = []
    for segment in result["segments"]:
        relative = segment["loss_to_principal_ratio"]
        rows.append([
            segment["segment"],
            f"{segment['complete_loans']:,}",
            pct(segment["principal_share"]),
            pct(segment["loss_share"]),
            "N/A" if relative is None else f"{relative:.2f}",
        ])
    return table([TITLES[result["dimension"]], "Complete terminal loans", "Share of funded principal",
                  "Share of unrecovered principal", "Loss share / principal share"], rows,
                 ["---", *["---:"] * 4])


def mix_table(result):
    """One row per issue year; one share column per segment."""
    rows = []
    for year in result["years"]:
        average = "N/A" if year["average_score"] is None else f"{year['average_score']:.1f}"
        rows.append([year["year"], f"{year['loans']:,}", average,
                     *[pct(segment["loan_share"]) for segment in year["segments"]]])
    return table(["Issue year", "Issued loans", "Average score", *result["segments"]], rows,
                 ["---", *["---:"] * (2 + len(result["segments"]))])


def rejected_table(result):
    """Rejected applications grouped by score era or application year."""
    baseline = result["baseline"]
    rows = []
    for group in result["groups"]:
        screen = screen_at_baseline(group, baseline)
        rows.append([
            group["group"],
            f"{group['rows']:,}",
            f"{group['usable_score_rows']:,}",
            f"{screen['rows']:,}",
            pct(ratio(screen["rows"], group["usable_score_rows"])),
            usd(screen["amount_sum"]),
        ])
    return table([GROUPINGS[result["by"]].capitalize(),
                  "Applications", "Usable score", f"At or above {baseline}",
                  "Share of usable", f"Requested amount at or above {baseline}"], rows,
                 ["---", *["---:"] * 5])


# ===========================================================================
# Report data and citation numbers
# ===========================================================================

class ReportView:
    """Lookups over run data plus the citation numbering shared by all sections."""

    def __init__(self, data):
        self.data = data
        self.calls = data["calls"]
        self.analysis = data["analysis"]
        self.scenario = Scenario(**data["settings"]["scenario"])
        self.citations = CitationNumbers()
        self.cite = self.citations.markdown

    def result(self, tool, **arguments):
        """Result of the first report call of ``tool`` whose arguments include these values."""
        for call in self.calls:
            if (call["origin"] == "report" and call["tool"] == tool
                    and all(call["arguments"].get(key) == value for key, value in arguments.items())):
                return call["result"]
        return None

    def fact(self, ref):
        return (self.analysis.get("facts", {}).get(ref)
                or self.data.get("facts", {}).get(ref, ""))


class CitationNumbers(dict):
    """Map each fact ID to its citation number, assigned in order of first use."""

    def markdown(self, ids):
        return " ".join(f"[\\[{number}\\]](#evidence-{number})" for number in map(self.number, ids))

    def text(self, ids):
        return "".join(f"[{number}]" for number in map(self.number, ids))

    def number(self, ref):
        if ref not in self:
            self[ref] = len(self) + 1
        return self[ref]


# ===========================================================================
# Report sections
# ===========================================================================

def _title(view):
    data = view.data
    return "\n".join([
        "# Score cutoff report: growth versus principal losses", "",
        "**Historical MySQL aggregate analysis**", "",
        f"Run `{data['run_id']}`, generated {data['generated_at_utc']}.",
    ])


def _summary(view):
    data = view.data
    scenario = view.scenario
    analysis = view.analysis
    snapshot = data["snapshot"]
    database = data["settings"]["database"]["database"]
    accepted = snapshot["sources"]["accepted"]
    rejected_source = snapshot["sources"]["rejected"]

    if analysis["status"] == "disabled":
        commentary = "disabled with `--no-ai`"
    else:
        commentary = f"local model {plain(analysis['model'])}, evidence-checked; requires review"

    lines = [
        f"- **Data:** {database}.{accepted['table']} ({accepted['total_rows']:,} rows) and "
        f"{database}.{rejected_source['table']} ({rejected_source['total_rows']:,} rows).",
        f"- **Snapshot:** `{snapshot['snapshot_id']}`, read {snapshot['read_started_at_utc']} to "
        f"{snapshot['read_completed_at_utc']}"
        + (" in a live MySQL read" if snapshot["origin"] == "mysql"
           else f", loaded from {plain(snapshot['origin'])}")
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
    lines = [
        "## Analyst summary", "",
        f"> Written by the local model **{plain(analysis['model'])}** from the cited facts, after {own_calls} "
        "tool call(s) of its own. Every statement passed evidence-reference and number checks; interpretations "
        "are model-generated and require review. Tables and cutoff selection are calculated by Python.", "",
    ]
    reviews = analysis.get("critic_reviews") or []
    if reviews:
        flagged = sum(1 for review in reviews if review["verdict"] != "supported")
        lines += [f"A second model pass reviewed {len(reviews)} statement(s) against their cited facts and "
                  f"objected to {flagged}; statements still objected to after one repair were withheld. That "
                  "review is also model-generated: it reduces overstatement but does not prove correctness.", ""]
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
    if not result:
        return ""
    return "## At a glance (calculated)\n\n" + cutoff_glance(result, view.scenario)


def _cutoffs(view):
    result = view.result("compare_score_cutoffs")
    if not result:
        return ""
    return "## Accepted loans: historical cutoff replay\n\n" + cutoff_table(result, view.scenario)


def _score_bands(view):
    result = view.result("segment_performance", dimension="score_band")
    if not result:
        return ""
    return ("## Loss by score band\n\nEach band holds only the loans between adjacent cutoffs, so raising "
            "the cutoff by one step removes exactly that band.\n\n" + segment_table(result))


def _vintages(view):
    result = view.result("segment_performance", dimension="issue_year")
    if not result:
        return ""
    return ("## Performance by issue year\n\nA low terminal share means most of that year's loans were "
            "still open in the source data, so its loss proxy covers only resolved loans.\n\n"
            + segment_table(result))


def _segments(view):
    parts = []
    for dimension in ("grade", "term"):
        result = view.result("segment_performance", dimension=dimension)
        if result:
            parts.append(segment_table(result))
    if not parts:
        return ""
    return "## Performance by grade and term\n\n" + "\n\n".join(parts)


def _vintage_trend(view):
    result = view.result("vintage_trend")
    if not result:
        return ""
    lines = [
        "## Vintage trend and seasoning", "",
        f"A vintage is treated as seasoned when at least {pct(result['min_terminal_share'])} of its loans are "
        "resolved. Only seasoned vintages are compared with each other; the threshold is a reporting choice.", "",
        vintage_table(result),
    ]
    change = result["seasoned_change"]
    if change:
        lines += ["", f"Earliest to latest seasoned vintage ({change['first']} to {change['last']}): loss proxy "
                      f"{pct(change['first_loss_rate'])} to {pct(change['last_loss_rate'])} "
                      f"({change['change_pp']:+.2f} percentage points). Loan age and mix are not controlled."]
    return "\n".join(lines)


def _concentration(view):
    parts = []
    for dimension in ("score_band", "grade"):
        result = view.result("loss_concentration", dimension=dimension)
        if result:
            parts.append(concentration_table(result))
    if not parts:
        return ""
    return ("## Loss concentration\n\nShares are among financially complete terminal loans. A ratio above 1 means "
            "a segment carries more than a proportional share of historical unrecovered principal; it is "
            "descriptive, not a limit or a forecast.\n\n" + "\n\n".join(parts))


def _mix_shift(view):
    result = view.result("mix_shift", dimension="score_band")
    if not result:
        return ""
    return ("## Origination mix by issue year\n\nShare of each year's issued loans (open and resolved) in each "
            "score band, with the loan-weighted average score. Mix describes what was originated; it does not "
            "by itself explain losses.\n\n" + mix_table(result))


def _rejected(view):
    result = view.result("compare_score_cutoffs")
    if not result:
        return ""

    lines = [
        "## Rejected applications: numeric screen", "",
        "The source dictionary identifies rejected Risk_Score as FICO before November 5, 2013 and VantageScore "
        "after that date, without specifying the VantageScore version or range. Risk_Score is therefore not "
        "generally comparable to accepted FICO. This is a numeric score screen only; it is not an approval rate "
        "or measured lending opportunity. Rejected applicants have no loan outcomes.", "",
        rejected_screen(result, view.scenario),
    ]
    eras = view.result("rejected_applications", by="score_era")
    if eras:
        lines += ["", "### By score era", "", rejected_table(eras)]
    return "\n".join(lines)


def _coverage(view):
    parts = []
    described = view.result("describe_data")
    compared = view.result("compare_score_cutoffs")
    if described:
        parts.append(overview_coverage_table(described))
    if compared:
        parts.append(cutoff_coverage_table(compared))
    if not parts:
        return ""
    return "## Coverage and exclusions\n\n" + "\n\n".join(parts)


def _next_decision(view):
    return ("## Interpretation and next decision\n\nAgree a loss ceiling and a comparable, sufficiently seasoned "
            "origination window before selecting a cutoff. For an expansion case, first establish the rejected score "
            "scale and test alternative take-up and loss assumptions. This analysis does not estimate revenue, "
            "funding costs, operating costs or profit.")


def _arguments(arguments):
    return ", ".join(f"{key}={value}" for key, value in arguments.items()) or "session settings"


def _investigation(view):
    # Arguments passed schema validation (numbers, months, enums), so code spans need no escaping.
    rows = []
    for call in view.calls:
        rows.append([
            call["id"],
            "analyst" if call["origin"] == "model" else "report",
            f"`{call['tool']}`",
            f"`{_arguments(call['arguments'])}`",
            plain(call["title"]),
        ])
    failed = [call for call in view.analysis.get("tool_calls", []) if "error" in call]
    lines = [
        "## How this analysis was produced", "",
        "Report steps run automatically; analyst steps were chosen by the local model. Every step is an "
        "allowlisted tool computed in Python over the same read-only snapshot, so no step queried MySQL again.",
        "", table(["Step", "By", "Tool", "Arguments", "Result"], rows, ["---"] * 5),
    ]
    if failed:
        lines += ["", f"The model also made {len(failed)} invalid tool request(s); each was rejected before "
                      "execution and is recorded in `ollama_analysis.json`."]
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
    "summary": _summary,
    "analyst": _analyst,
    "at_a_glance": _glance,
    "cutoffs": _cutoffs,
    "score_bands": _score_bands,
    "vintages": _vintages,
    "vintage_trend": _vintage_trend,
    "segments": _segments,
    "concentration": _concentration,
    "mix_shift": _mix_shift,
    "rejected": _rejected,
    "coverage": _coverage,
    "next_decision": _next_decision,
    "investigation": _investigation,
    "evidence": _evidence,
    "definitions": _definitions,
    "reproducibility": _reproducibility,
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
            rendered[name] = SECTIONS[name](view)
    if "evidence" in sections:
        rendered["evidence"] = _evidence(view)

    # Display order still follows the user's selection, even for the evidence list.
    parts = [_title(view)]
    for name in sections:
        if rendered[name]:
            parts.append(rendered[name])
    return "\n\n".join(parts) + "\n"


# ===========================================================================
# Standalone answers (ask/chat)
# ===========================================================================

ANSWER_HEADINGS = (("findings", "What the results show"), ("caveats", "Caveats"),
                   ("next_steps", "Next analytical steps"))


def answer_markdown(question, analysis):
    """A standalone Markdown answer for `ask` and `chat` transcripts."""
    numbers = CitationNumbers()
    cite = numbers.markdown
    answer = analysis["answer"]

    lines = [f"## {plain(question)}", ""]
    lines += [f"**{plain(item['text'])}** {cite(item['evidence_ids'])}" for item in answer["answer"]]
    for key, heading in ANSWER_HEADINGS:
        if answer.get(key):
            lines += ["", f"**{heading}**", ""]
            lines += [f"- {plain(item['text'])} {cite(item['evidence_ids'])}" for item in answer[key]]

    lines += ["", "**Evidence**", ""]
    lines += [f'<a id="evidence-{number}"></a>[{number}] `{ref}`: {plain(analysis["facts"][ref])}  '
              for ref, number in numbers.items()]
    lines += ["", f"_Model-generated by local {plain(analysis['model'])}; every statement passed evidence and number "
                   "checks. Review before relying on it._"]
    return "\n".join(lines) + "\n"


def answer_text(analysis, width=100):
    """Readable terminal rendering with numbered evidence."""
    numbers = CitationNumbers()
    cite = numbers.text
    answer = analysis["answer"]

    lines = [wrap(f"{item['text']} {cite(item['evidence_ids'])}", width=width) for item in answer["answer"]]
    for key, heading in ANSWER_HEADINGS:
        if answer.get(key):
            lines += ["", f"{heading}:"]
            lines += [wrap(f"{item['text']} {cite(item['evidence_ids'])}", "  - ", width)
                      for item in answer[key]]

    lines += ["", "Evidence:"]
    lines += [wrap(f"{ref}: {analysis['facts'][ref]}", f"  [{number}] ", width)
              for ref, number in numbers.items()]

    ran = [call for call in analysis.get("tool_calls", []) if "id" in call]
    if ran:
        tools = "; ".join(f"{call['id']} {call['tool']}({_arguments(call['arguments'])})" for call in ran)
        lines += ["", wrap("Tools the analyst ran: " + tools, width=width)]
    if analysis.get("withheld"):
        lines.append(f"{len(analysis['withheld'])} statement(s) withheld after failing evidence checks.")
    lines.append(f"Model-generated by local {analysis['model']}; numbers checked against cited facts. "
                 "Review before relying on it.")
    return "\n".join(lines)
