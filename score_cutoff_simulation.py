"""Compare score cutoffs using only MySQL aggregates; write a local demo report.

Run: .venv/Scripts/python.exe score_cutoff_simulation.py
Offline example: .venv/Scripts/python.exe score_cutoff_simulation.py --demo
Requires MySQL 8.0+ and the existing mysql-connector-python dependency.
"""

import argparse
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from getpass import getpass
import json
import math
import os
from pathlib import Path
import re
import sys
from time import perf_counter

from import_csv_to_mysql import quote_identifier


PROJECT_ROOT = Path(__file__).resolve().parent
REQUIRED = {
    "accepted": {"fico_range_low", "fico_range_high", "funded_amnt", "issue_d",
                 "loan_status", "total_rec_prncp", "recoveries"},
    "rejected": {"Risk_Score", "Amount Requested", "Application Date"},
}
SUM_FIELDS = ("rows", "amount_rows", "amount_sum", "terminal_rows",
              "charged_off_rows", "performance_rows", "performance_principal", "loss_amount")


def project_path(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def month(value):
    if not re.fullmatch(r"[12][0-9]{3}-(0[1-9]|1[0-2])", value):
        raise argparse.ArgumentTypeError("Use YYYY-MM, for example 2015-01")
    return value


def fraction(value):
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise argparse.ArgumentTypeError("Use a fraction from 0 to 1 (0.08 means 8%)")
    return number


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--user", default="root")
    parser.add_argument("--database", "--schema", default="demo")
    parser.add_argument("--accepted-table", default="accepted")
    parser.add_argument("--rejected-table", default="rejected")
    parser.add_argument("--cutoffs", default="600,640,660,680,700,720,740",
                        help="Comma-separated integer minimum scores, inclusive")
    parser.add_argument("--baseline", type=int, default=660,
                        help="Comparison cutoff; illustrative default 660, not current policy")
    parser.add_argument("--start-month", type=month)
    parser.add_argument("--end-month", type=month)
    parser.add_argument("--max-loss-rate", type=fraction,
                        help="Optional historical loss-proxy ceiling, e.g. 0.08")
    parser.add_argument("--min-outcomes", type=int, default=100,
                        help="Minimum complete terminal outcomes to display a loss rate")
    parser.add_argument("--assume-comparable-scores", action="store_true",
                        help="Explicit hypothetical assumption: rejected scores equal FICO scale")
    parser.add_argument("--rejected-loss-rate", type=fraction,
                        help="Assumed lifetime principal loss rate on new rejected lending")
    parser.add_argument("--take-up-rate", type=fraction, default=1.0,
                        help="Assumed share of screened rejected demand funded (default 1)")
    parser.add_argument("--query-timeout", type=int, default=600,
                        help="Maximum seconds per aggregate SELECT; default 600")
    parser.add_argument("--output-dir", type=project_path, default=PROJECT_ROOT / "outputs" / "score_cutoff")
    parser.add_argument("--demo", action="store_true",
                        help="Use artificial aggregate fixtures, never real loan data")
    parser.add_argument("--print-sql", action="store_true",
                        help="Print aggregate SQL and parameters without connecting")
    args = parser.parse_args(argv)
    try:
        args.cutoffs = sorted({int(x.strip()) for x in args.cutoffs.split(",")} | {args.baseline})
    except ValueError:
        parser.error("--cutoffs must be comma-separated integers")
    if len(args.cutoffs) > 30 or any(not 300 <= x <= 850 for x in args.cutoffs):
        parser.error("Use at most 30 cutoffs, all between 300 and 850, including baseline")
    if args.start_month and args.end_month and args.start_month > args.end_month:
        parser.error("--start-month must not be after --end-month")
    if not 1 <= args.port <= 65535 or args.query_timeout < 1 or args.min_outcomes < 1:
        parser.error("Invalid port, query timeout, or minimum outcomes")
    if args.accepted_table.casefold() == args.rejected_table.casefold():
        parser.error("Accepted and rejected tables must differ")
    if args.assume_comparable_scores != (args.rejected_loss_rate is not None):
        parser.error("Use --assume-comparable-scores and --rejected-loss-rate together")
    if args.take_up_rate != 1 and not args.assume_comparable_scores:
        parser.error("--take-up-rate requires the explicit rejected lending assumptions")
    if args.demo and (args.start_month or args.end_month):
        parser.error("Demo fixtures have no monthly detail; omit date filters or use MySQL")
    try:
        for name in (args.database, args.accepted_table, args.rejected_table):
            quote_identifier(name)
    except ValueError as error:
        parser.error(str(error))
    return args


def numeric_sql(column):
    """Validate text before casting, so blanks/bad text cannot silently become zero."""
    col = f"TRIM({quote_identifier(column)})"
    return (f"CASE WHEN {col} REGEXP '^[0-9]{{1,12}}([.][0-9]{{1,4}})?$' "
            f"THEN CAST({col} AS DECIMAL(20,4)) END")


def build_query(kind, args):
    """One scan per table, bounded grouped output; no borrower records leave MySQL."""
    table = getattr(args, f"{kind}_table")
    source = f"{quote_identifier(args.database)}.{quote_identifier(table)}"
    if kind == "accepted":
        months = "'Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'"
        date_sql = (
            "CASE WHEN TRIM(`issue_d`) REGEXP '^[A-Za-z]{3}-[12][0-9]{3}$' "
            f"AND FIELD(LEFT(TRIM(`issue_d`),3),{months}) > 0 "
            f"THEN CONCAT(RIGHT(TRIM(`issue_d`),4),'-',LPAD(FIELD(LEFT(TRIM(`issue_d`),3),{months}),2,'0')) END"
        )
        parsed = f"""{numeric_sql('fico_range_low')} AS score_low,
        {numeric_sql('fico_range_high')} AS score_high,
        {numeric_sql('funded_amnt')} AS amount,
        {numeric_sql('total_rec_prncp')} AS principal_received,
        {numeric_sql('recoveries')} AS recovered,
        TRIM(`loan_status`) AS status, {date_sql} AS period"""
        score_sql = ("CASE WHEN score_low BETWEEN 300 AND 850 AND score_high BETWEEN 300 AND 850 "
                     "AND score_low <= score_high AND MOD(score_low,1)=0 AND MOD(score_high,1)=0 "
                     "THEN (score_low+score_high)/2 END")
        terminal = "status IN ('Fully Paid','Charged Off')"
        valid = f"({terminal}) AND amount > 0 AND principal_received BETWEEN 0 AND amount AND recovered >= 0"
        metrics = f""",
    SUM(CASE WHEN {terminal} THEN 1 ELSE 0 END) AS terminal_rows,
    SUM(CASE WHEN status='Charged Off' THEN 1 ELSE 0 END) AS charged_off_rows,
    SUM(CASE WHEN {valid} THEN 1 ELSE 0 END) AS performance_rows,
    SUM(CASE WHEN {valid} THEN amount ELSE 0 END) AS performance_principal,
    SUM(CASE WHEN {valid} AND status='Charged Off'
        THEN GREATEST(amount-principal_received-recovered,0) ELSE 0 END) AS loss_amount"""
    else:
        date_sql = """CASE WHEN TRIM(`Application Date`) REGEXP
            '^[12][0-9]{3}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$'
            THEN CASE WHEN CAST(RIGHT(TRIM(`Application Date`),2) AS UNSIGNED)
                <= DAY(LAST_DAY(CONCAT(LEFT(TRIM(`Application Date`),7),'-01')))
                THEN LEFT(TRIM(`Application Date`),7) END END"""
        parsed = f"""{numeric_sql('Risk_Score')} AS raw_score,
        {numeric_sql('Amount Requested')} AS amount, {date_sql} AS period"""
        score_sql = "CASE WHEN raw_score BETWEEN 300 AND 850 AND MOD(raw_score,1)=0 THEN raw_score END"
        metrics = ""
    bucket_sql = "CASE WHEN score IS NULL THEN -1 " + " ".join(
        "WHEN score >= %s THEN %s" for _ in reversed(args.cutoffs)
    ) + " ELSE 0 END"
    params = []
    for cutoff in reversed(args.cutoffs):
        params.extend([cutoff, cutoff])
    # Include off-scope aggregate counts so date and score exclusions stay visible.
    scope_sql = "CASE WHEN period IS NULL THEN 'invalid_date' "
    for operator, value in (("<", args.start_month), (">", args.end_month)):
        if value:
            scope_sql += f"WHEN period {operator} %s THEN 'outside_period' "
            params.append(value)
    scope_sql += "ELSE 'in_scope' END"
    sql = f"""WITH parsed AS (
    SELECT {parsed} FROM {source}
), scored AS (
    SELECT parsed.*, {score_sql} AS score FROM parsed
), classified AS (
    SELECT scored.*, {bucket_sql} AS bucket, {scope_sql} AS scope FROM scored
)
SELECT scope, bucket, COUNT(*) AS `rows`, MIN(period) AS first_month, MAX(period) AS last_month,
    SUM(CASE WHEN amount > 0 THEN 1 ELSE 0 END) AS amount_rows,
    SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) AS amount_sum{metrics}
FROM classified GROUP BY scope, bucket ORDER BY scope, bucket"""
    return sql, tuple(params)


def fetch_aggregates(connection, args):
    # Server-enforced read-only repeatable snapshot across both InnoDB tables.
    connection.start_transaction(isolation_level="REPEATABLE READ", readonly=True,
                                 consistent_snapshot=True)
    try:
        with closing(connection.cursor(dictionary=True)) as cursor:
            for kind in ("accepted", "rejected"):
                table = getattr(args, f"{kind}_table")
                cursor.execute("SELECT ENGINE FROM information_schema.TABLES "
                               "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (args.database, table))
                metadata = cursor.fetchone()
                if not metadata or metadata["ENGINE"] != "InnoDB":
                    raise ValueError(f"{kind}: expected an existing InnoDB table")
                cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                               "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (args.database, table))
                missing = REQUIRED[kind] - {row["COLUMN_NAME"] for row in cursor.fetchall()}
                if missing:
                    raise ValueError(f"{kind}: missing required columns: {', '.join(sorted(missing))}")
            result = {}
            for kind in ("accepted", "rejected"):
                print(f"Aggregating {kind} in MySQL (large tables can take several minutes)...", flush=True)
                sql, params = build_query(kind, args)
                cursor.execute(sql, params)
                result[kind] = [
                    {key: (int(value) if key.endswith("rows") else float(value)) if isinstance(value, Decimal) else value
                     for key, value in row.items()} for row in cursor.fetchall()
                ]
                if not result[kind]:
                    raise ValueError(f"{kind} table is empty; import data before running the report")
            return result
    finally:
        connection.rollback()


def total(rows):
    return {field: sum(row.get(field, 0) for row in rows) for field in SUM_FIELDS}


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def analyze(aggregates, args):
    quality = {}
    for kind, rows in aggregates.items():
        scoped = [row for row in rows if row["scope"] == "in_scope"]
        dates = [row[key] for row in scoped for key in ("first_month", "last_month") if row.get(key)]
        quality[kind] = {
            "total_rows": sum(row["rows"] for row in rows),
            "in_scope_rows": sum(row["rows"] for row in scoped),
            "invalid_date_rows": sum(row["rows"] for row in rows if row["scope"] == "invalid_date"),
            "outside_period_rows": sum(row["rows"] for row in rows if row["scope"] == "outside_period"),
            "invalid_score_rows": sum(row["rows"] for row in scoped if row["bucket"] == -1),
            "invalid_amount_rows": sum(row["rows"] - row["amount_rows"] for row in scoped),
            "first_month": min(dates) if dates else None, "last_month": max(dates) if dates else None,
        }
    scored_accepted = sum(row["rows"] for row in aggregates["accepted"]
                          if row["scope"] == "in_scope" and row["bucket"] >= 0)
    scenarios = []
    for cutoff in args.cutoffs:
        selected = {kind: total([row for row in rows if row["scope"] == "in_scope"
                                and row["bucket"] >= cutoff]) for kind, rows in aggregates.items()}
        accepted, rejected = selected["accepted"], selected["rejected"]
        adequate = accepted["performance_rows"] >= args.min_outcomes
        rate = ratio(accepted["loss_amount"], accepted["performance_principal"]) if adequate else None
        scenarios.append({
            "cutoff": cutoff, "accepted": accepted, "rejected": rejected,
            "accepted_retention_rate": ratio(accepted["rows"], scored_accepted),
            "terminal_share": ratio(accepted["terminal_rows"], accepted["rows"]),
            "chargeoff_rate": ratio(accepted["charged_off_rows"], accepted["terminal_rows"])
                if accepted["terminal_rows"] >= args.min_outcomes else None,
            "loss_rate": rate,
            "loss_data_coverage": ratio(accepted["performance_rows"], accepted["terminal_rows"]),
            "assumed_new_loans": rejected["amount_rows"] * args.take_up_rate
                if args.assume_comparable_scores else None,
            "assumed_new_principal": rejected["amount_sum"] * args.take_up_rate
                if args.assume_comparable_scores else None,
            "assumed_new_loss": rejected["amount_sum"] * args.take_up_rate * args.rejected_loss_rate
                if args.assume_comparable_scores else None,
        })
    baseline = next(row for row in scenarios if row["cutoff"] == args.baseline)
    for row in scenarios:
        row["accepted_count_change"] = row["accepted"]["rows"] - baseline["accepted"]["rows"]
        row["accepted_volume_change"] = row["accepted"]["amount_sum"] - baseline["accepted"]["amount_sum"]
        row["accepted_growth_rate"] = ratio(row["accepted_count_change"], baseline["accepted"]["rows"])
        row["loss_rate_change_pp"] = (100 * (row["loss_rate"] - baseline["loss_rate"])
                                      if row["loss_rate"] is not None and baseline["loss_rate"] is not None else None)
        if args.assume_comparable_scores:
            row["hypothetical_total_loans"] = row["accepted"]["rows"] + row["assumed_new_loans"]
            row["hypothetical_count_change"] = row["hypothetical_total_loans"] - baseline["accepted"]["rows"]
    eligible = [row for row in scenarios if args.max_loss_rate is not None
                and row["loss_rate"] is not None and row["loss_rate"] <= args.max_loss_rate
                and row["loss_data_coverage"] == 1]
    candidate = max(eligible, key=lambda row: (row["accepted"]["rows"], row["cutoff"])) if eligible else None
    return {"quality": quality, "scenarios": scenarios, "baseline": args.baseline,
            "candidate_cutoff": candidate["cutoff"] if candidate else None}


def demo_aggregates(args):
    """Artificial band totals, independent of source data, for a repeatable walkthrough."""
    result = {"accepted": [], "rejected": []}
    # Assign synthetic score-band populations to the requested thresholds.
    for score, count, bad, loss_rate in ((610, 800, 200, .18), (650, 1400, 252, .12),
                                        (670, 2200, 286, .085), (690, 2600, 260, .06),
                                        (710, 2300, 161, .04), (730, 1700, 85, .025),
                                        (770, 1000, 30, .012)):
        bucket = max((cutoff for cutoff in args.cutoffs if cutoff <= score), default=0)
        terminal = int(count * .8)
        result["accepted"].append({
            "scope": "in_scope", "bucket": bucket, "rows": count, "amount_rows": count,
            "amount_sum": count * 12000, "terminal_rows": terminal, "charged_off_rows": bad,
            "performance_rows": terminal, "performance_principal": terminal * 12000,
            "loss_amount": terminal * 12000 * loss_rate,
        })
        rejected = max(100, 9000 - (score - 600) * 45)
        result["rejected"].append({"scope": "in_scope", "bucket": bucket,
                                   "rows": rejected, "amount_rows": rejected,
                                   "amount_sum": rejected * 10000})
    for kind in result:
        result[kind].append({"scope": "in_scope", "bucket": -1,
                             "rows": 300, "amount_rows": 300, "amount_sum": 3000000})
    return result


def pct(value):
    return "N/A" if value is None else f"{value:.2%}"


def usd(value):
    return f"${value:,.0f}"


def render_report(result, args):
    rows = result["scenarios"]
    baseline = next(row for row in rows if row["cutoff"] == args.baseline)
    mode = "SYNTHETIC DEMO — not MySQL results" if args.demo else "Historical MySQL aggregate analysis"
    lines = ["# Score cutoff simulation: growth versus principal losses", "", f"**{mode}**", "",
             f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')} (UTC).",
             f"Cohort months: {args.start_month or 'earliest available'} through {args.end_month or 'latest available'}; "
             "accepted uses issue month, rejected uses application month.", "",
             "## Executive summary", "",
             f"- The comparison baseline is **{args.baseline}** (a scenario assumption, not a verified current policy). "
             f"It retains **{baseline['accepted']['rows']:,} accepted loans** and "
             f"**{usd(baseline['accepted']['amount_sum'])}** of valid funded principal. "
             f"Its historical principal-loss proxy is **{pct(baseline['loss_rate'])}** on complete terminal outcomes."]
    for label, row in (("Lowest tested cutoff", rows[0]), ("Highest tested cutoff", rows[-1])):
        if row["cutoff"] != args.baseline:
            lines.append(f"- {label} **{row['cutoff']}** retains **{row['accepted']['rows']:,} loans**, "
                         f"a change of **{row['accepted_count_change']:+,}** ({pct(row['accepted_growth_rate'])}) "
                         f"versus baseline, with a historical "
                         f"loss proxy of **{pct(row['loss_rate'])}**.")
    if args.max_loss_rate is None:
        lines.append("- No risk ceiling was supplied, so no cutoff is selected. Use the table to agree a volume/loss tradeoff.")
    elif result["candidate_cutoff"] is None:
        lines.append(f"- No tested cutoff clears the supplied **{pct(args.max_loss_rate)}** historical loss ceiling "
                     "with enough outcomes and complete terminal financial data.")
    else:
        lines.append(f"- **{result['candidate_cutoff']}** retains the most historical accepted loans among tested cutoffs "
                     f"with a loss proxy at or below **{pct(args.max_loss_rate)}**, enough outcomes, and complete "
                     "terminal financial data. This is a historical screening candidate, not a lending-policy recommendation. "
                     "The ceiling is tested only on accepted outcomes; hypothetical expansion losses are not tested against it.")
    if not any(row['accepted']['rows'] for row in rows):
        lines.append("- **No accepted loans with usable scores meet the tested cutoffs in this cohort.** "
                     "There is no supported accepted growth/loss comparison.")
    elif not any(row['loss_rate'] is not None for row in rows):
        lines.append("- **No tested cutoff has enough complete terminal outcomes for a loss rate.** "
                     "Volume alone cannot establish the risk tradeoff.")
    lines += ["", "## Accepted loans: historical cutoff replay", "",
              "Each row retains original FICO midpoint ≥ cutoff. Counts include open loans; losses use only "
              "Fully Paid and Charged Off loans. These populations differ, so the table is a tradeoff screen, not a forecast.", "",
              "| Cutoff | Retained loans | Change vs baseline | Funded principal | Terminal share | Charge-off rate | Loss proxy | Loss change (pp) |",
              "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in rows:
        delta = "N/A" if row["loss_rate_change_pp"] is None else f"{row['loss_rate_change_pp']:+.2f}"
        lines.append(f"| {row['cutoff']} | {row['accepted']['rows']:,} | {row['accepted_count_change']:+,} "
                     f"| {usd(row['accepted']['amount_sum'])} | {pct(row['terminal_share'])} "
                     f"| {pct(row['chargeoff_rate'])} | {pct(row['loss_rate'])} | {delta} |")
    lines += ["", "## Rejected applications: potential demand", "",
              "The source dictionary identifies rejected Risk_Score as FICO before November 5, 2013 and "
              "VantageScore after that date, without specifying the VantageScore version or range. "
              "Risk_Score is therefore not generally comparable to accepted FICO. The following is a numeric score screen "
              "only; it is not an approval rate or measured lending opportunity. Rejected applicants have no loan outcomes.", "",
              "| Numeric threshold | Screened applications | Valid requested amount | Applications with valid amount |",
              "| ---: | ---: | ---: | ---: |"]
    for row in rows:
        rejected = row["rejected"]
        lines.append(f"| {row['cutoff']} | {rejected['rows']:,} | {usd(rejected['amount_sum'])} | {rejected['amount_rows']:,} |")
    if args.assume_comparable_scores:
        lines += ["", "### Explicit hypothetical expansion", "",
                  f"Assumes comparable scores, **{pct(args.take_up_rate)}** take-up of valid requested amounts, "
                  f"full requested funding, and **{pct(args.rejected_loss_rate)}** lifetime principal loss on that new lending. "
                  "Neither take-up nor losses are estimated from rejected outcomes. Other underwriting constraints are ignored.", "",
                  "| Cutoff | Assumed new loans | Assumed new principal | Assumed new losses | Total loan change vs accepted-only baseline |",
                  "| ---: | ---: | ---: | ---: | ---: |"]
        for row in rows:
            lines.append(f"| {row['cutoff']} | {row['assumed_new_loans']:,.1f} | {usd(row['assumed_new_principal'])} "
                         f"| {usd(row['assumed_new_loss'])} | {row['hypothetical_count_change']:+,.1f} |")
        lines += ["", "New-loss assumptions and historical accepted losses have different observation windows; "
                  "they are not added into a purported portfolio loss forecast. Expansion includes all screened rejects "
                  "at each cutoff, including at the baseline cutoff; it is not solely the effect of lowering a threshold."]
    else:
        lines += ["", "No rejected loan growth or loss forecast is made. To illustrate one explicitly, rerun with "
                  "`--assume-comparable-scores --rejected-loss-rate 0.15 --take-up-rate 0.25`. "
                  "Those numbers are example assumptions, not measured estimates."]
    lines += ["", "## Coverage and exclusions", "",
              "| Source | Table rows | In cohort | Invalid date | Outside cohort | Invalid score in cohort | Invalid amount in cohort |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for kind, q in result["quality"].items():
        lines.append(f"| {kind} | {q['total_rows']:,} | {q['in_scope_rows']:,} | {q['invalid_date_rows']:,} "
                     f"| {q['outside_period_rows']:,} | {q['invalid_score_rows']:,} | {q['invalid_amount_rows']:,} |")
    lines += ["", "| Cutoff | Excluded nonterminal/other outcomes | Complete terminal outcomes | Terminal financial coverage | Loss denominator | Observed loss proxy dollars |",
              "| ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in rows:
        accepted = row["accepted"]
        lines.append(f"| {row['cutoff']} | {accepted['rows'] - accepted['terminal_rows']:,} "
                     f"| {accepted['performance_rows']:,} | {pct(row['loss_data_coverage'])} "
                     f"| {usd(accepted['performance_principal'])} | {usd(accepted['loss_amount'])} |")
    for kind, q in result["quality"].items():
        lines += ["", f"{kind.capitalize()} observed cohort bounds: {q['first_month'] or 'unavailable'} "
                  f"to {q['last_month'] or 'unavailable'}."]
    lines += ["", "## Interpretation and next decision", "",
              "Agree a loss ceiling and a comparable, sufficiently seasoned origination window before selecting a cutoff. "
              "For an expansion case, first establish the rejected score scale and test alternative take-up and loss assumptions. "
              "The script does not estimate revenue, funding costs, operating costs, or profit.", "",
              "## Definitions and limitations", "",
              "- Growth is the change in historically accepted loan count or funded principal retained by a score rule. "
              "It cannot recover historical approvals below the original underwriting policy. Application rows are not deduplicated people.",
              "- Accepted score = midpoint of original `fico_range_low` and `fico_range_high`, inclusive cutoff. "
              "Both endpoints must be integers in 300–850 and ordered. Rejected scores outside 300–850, decimals, missing, "
              "or invalid text are excluded; score-scale compatibility remains unverified even inside that range.",
              "- Terminal outcomes = exactly Fully Paid or Charged Off. Current, late, Default, legacy policy statuses, "
              "and unknown statuses are excluded from performance rates. Terminal-only selection can bias loss estimates; "
              "this is not a fixed-horizon default model and open loans are not treated as zero-loss outcomes.",
              "- Charge-off rate = terminal Charged Off count / all terminal count. Principal-loss proxy = sum of "
              "max(funded_amnt − total_rec_prncp − recoveries, 0) for complete Charged Off loans / total funded_amnt "
              "of complete terminal loans. Fully Paid contributes zero loss. Recoveries are gross; costs, interest, "
              "fees and recovery timing are excluded. This is an unrecovered-principal proxy, not accounting net charge-offs.",
              "- Complete financial data requires positive funded amount, nonnegative principal received no greater than funded "
              "amount, and nonnegative recoveries. Missing or malformed values are excluded, never silently made zero. "
              "Counts can therefore include loans excluded from dollar totals. Monetary text accepts up to four decimal places.",
              f"- Rates are N/A below {args.min_outcomes} relevant outcomes or with zero denominator. "
              "The minimum is a demo stability check, not a statistical guarantee or privacy threshold. "
              "The optional historical candidate also requires 100% terminal financial coverage.",
              "- Dates filter issue/application cohorts, not repayment observation dates. The CSV performance snapshot "
              "does not establish performance as of the execution date. Different calendar coverage, seasoning, "
              "selection bias, incomplete imports, or duplicate imports can distort comparisons. No import completeness "
              "or deduplication is certified by this report.",
              "- MySQL is read in a consistent, read-only transaction. Only grouped counts, sums and month bounds "
              "reach Python; no borrower rows, names, IDs, descriptions or contact data are exported. "
              "No AI service or external reporting service receives data. Aggregates can still reveal small groups; "
              "this demo does not provide anonymization or differential privacy.", "",
              "## Reproducibility", "",
              f"Source: {'artificial aggregate fixtures' if args.demo else args.database + '.' + args.accepted_table + ' and ' + args.database + '.' + args.rejected_table}.",
              "`aggregates.json` retains the aggregate inputs, scenario calculations, parameters and executed query templates "
              "beside this report. The source dictionary is `data/LCDataDictionary.xlsx`. "
              "Use `--print-sql` to inspect queries without connecting.", ""]
    return "\n".join(lines)


def main(argv=None):
    args = parse_args(argv)
    queries = {kind: dict(zip(("sql", "params"), build_query(kind, args))) for kind in REQUIRED}
    if args.print_sql:
        print(json.dumps(queries, indent=2))
        return
    started = perf_counter()
    connection = None
    try:
        if args.demo:
            aggregates = demo_aggregates(args)
        else:
            try:
                import mysql.connector
            except ImportError as error:
                raise RuntimeError("Install the project requirements before connecting to MySQL") from error
            password = os.environ.get("MYSQL_PASSWORD")
            if password is None:
                if not sys.stdin.isatty():
                    raise RuntimeError("Set MYSQL_PASSWORD locally or run in an interactive terminal for the password prompt; "
                                       "use --demo for synthetic data")
                password = getpass(f"MySQL password for {args.user}@{args.host}: ")
            connection = mysql.connector.connect(
                host=args.host, port=args.port, user=args.user, password=password,
                charset="utf8mb4", connection_timeout=10, autocommit=True, sql_mode="TRADITIONAL",
            )
            with closing(connection.cursor()) as cursor:
                cursor.execute("SET SESSION MAX_EXECUTION_TIME = %s", (args.query_timeout * 1000,))
            aggregates = fetch_aggregates(connection, args)
        result = analyze(aggregates, args)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        report_path = args.output_dir / "report.md"
        report_path.write_text(render_report(result, args), encoding="utf-8")
        # Explicit allowlist: never serialize credentials, connection objects or arbitrary source rows.
        settings = {key: getattr(args, key) for key in (
            "database", "accepted_table", "rejected_table", "cutoffs", "baseline", "start_month", "end_month",
            "max_loss_rate", "min_outcomes", "assume_comparable_scores", "rejected_loss_rate", "take_up_rate", "demo")}
        payload = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "settings": settings,
                   "aggregates": aggregates, "results": result,
                   "queries": queries if not args.demo else {}, "elapsed_seconds": perf_counter() - started}
        (args.output_dir / "aggregates.json").write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
        print(f"{'SYNTHETIC DEMO' if args.demo else 'MySQL aggregate'} report: {report_path}")
        print(f"Completed in {perf_counter() - started:.1f}s. Existing files in this output directory are replaced.")
    except KeyboardInterrupt:
        raise SystemExit("Simulation interrupted; no database data was changed")
    except Exception as error:
        # Connector error text may quote invalid source values. Keep failures aggregate-only too.
        if type(error).__module__.startswith("mysql"):
            detail = f"MySQL error {getattr(error, 'errno', 'unknown')}; check access, schema and query timeout"
        else:
            detail = str(error)
        print(f"Simulation failed: {detail}", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    main()
