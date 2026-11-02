# MySQL loan reports with local Qwen analysis

Generate reports from the existing MySQL tables `demo.accepted` and
`demo.rejected`. The schema is named `demo`, but these are the real imported
loan tables. Reporting has no synthetic mode, sample-file input, CSV fallback,
or preview-table workflow.

## Generate a report

Run from this project in PowerShell. MySQL 8.0+ and local Ollama must be running.
The default model is the installed `qwen3:4b`.

```powershell
# First-time setup only, if the environment is missing:
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Install the model only if it is not already installed:
ollama pull qwen3:4b

# Generate a report; enter the MySQL password at the local terminal prompt:
.\.venv\Scripts\python.exe .\generate_framework_report.py
```

Connection defaults: `localhost:3306`, user `root`, database `demo`, tables
`accepted` and `rejected`. Override these with `--host`, `--port`, `--user`,
`--database` (or `--schema`), `--accepted-table`, and `--rejected-table`.
Unattended runs read `MYSQL_PASSWORD` from the local process environment.
Never put passwords in command-line arguments, source code, or committed files.
A SELECT-only account is sufficient for reporting.

The default folder is `outputs/framework_report/`, containing:

| File | Contents |
| --- | --- |
| `report.md` | Numerical comparisons, coverage, definitions, and local Qwen interpretation with evidence references. |
| `aggregates.json` | The actual MySQL aggregate snapshot, deterministic calculations, settings, executed SQL templates and parameters, read times, run ID, and evidence hash. |
| `ollama_analysis.json` | Exact aggregate-only model request, cited facts, validated analysis, model digest, timing, token counts, and matching run ID and evidence hash. |

Reruns replace these three files. Use `--output-dir` to keep separate runs.
Relative paths resolve from the project root. Both tables are validated and read
in one consistent, read-only transaction, which ends before model generation.
Each table is scanned once; large text tables can take several minutes. The
server-side query limit defaults to 600 seconds per SELECT (`--query-timeout`).
Neither reports nor models modify the database.

```powershell
# Choose a historical cohort and comparisons:
.\.venv\Scripts\python.exe .\generate_framework_report.py `
  --cutoffs 600,640,660,680,700,720,740 --baseline 660 `
  --start-month 2012-01 --end-month 2015-12 `
  --output-dir outputs/cohort_report

# Inspect SQL without connecting to MySQL or Ollama, or creating output:
.\.venv\Scripts\python.exe .\generate_framework_report.py --print-sql

# Explicitly omit model commentary, while still using the real MySQL tables:
.\.venv\Scripts\python.exe .\generate_framework_report.py --no-ai
```

`score_cutoff_simulation.py` remains a compatibility entry point for the same
MySQL/Qwen pipeline, with its existing `outputs/score_cutoff/` default folder.
The old `--demo` flag is rejected by every reporting entry point.

## What Qwen does

The runner checks the installed local model before scanning MySQL. After Python
calculates the results, Qwen writes interpretations of volume/loss tradeoffs,
coverage limitations, and useful next analytical steps. It sees only a bounded
set of aggregate facts and metric definitions. It receives no credentials,
connection settings, borrower records, or SQL execution tools. It cannot change
queries, calculations, or the selected historical cutoff.

The client uses Ollama's [chat API](https://docs.ollama.com/api/chat) with
[structured JSON output](https://docs.ollama.com/capabilities/structured-outputs),
`think: false`, and temperature zero. The endpoint must be loopback HTTP;
HTTP proxies, redirects, and cloud model tags are blocked. Exact numerical facts
are rendered by Python beside the model's evidence references. Numeric magnitudes
and units quoted by the model must appear in its cited facts. These checks do not prove that
the model's interpretation is correct, so commentary is labeled for review.
If wording is edited during review, retain the original response as `model_draft`
and record the corrections in `review` inside the model evidence file.

Options: `--ollama-url http://localhost:11434`, `--ollama-model qwen3:4b`, and
`--ollama-timeout 300` (seconds per generation request). One validation retry is
allowed without querying MySQL again. A missing model, timeout, malformed output,
unknown reference, or incomplete response fails the run without replacing the
previous report. There is no silent model-free or synthetic fallback. `--no-ai`
is the explicit way to disable commentary; its evidence file records that status.
The model stays loaded for five minutes after generation. No new Python package
is required for the Ollama integration.

## Reading the results

Accepted loans use the midpoint of original `fico_range_low` and
`fico_range_high`. Retained counts and funded principal cover all qualifying
accepted loans. Performance uses only exactly `Fully Paid` and `Charged Off`.
The principal-loss proxy is unrecovered principal on financially complete
charged-off loans divided by funded principal of all financially complete
terminal loans. Fully paid loans contribute zero loss; open loans do not enter
the loss denominator. Costs, interest, fees, and timing are excluded. This is
historical cohort analysis, not an annualized loss measure or a forecast.

Malformed numeric text is excluded before casting. Missing financial values
are not zero. Rates require at least 100 relevant outcomes by default
(`--min-outcomes`); this is a reporting threshold, not statistical validation.
Coverage and exclusions are shown in the report. Read times do not establish
when the underlying repayment outcomes were observed. The report cannot certify
that imports are complete or deduplicated.

The baseline defaults to 660 for comparison; it is not an established policy.
No cutoff is selected unless you supply your own `--max-loss-rate` as a fraction.
A candidate must meet that historical accepted-loss ceiling, have enough
outcomes, and have complete terminal financial coverage. It is not a lending
recommendation. Cutoff comparisons replay historical loans and do not measure
approval rates or prove a causal effect of changing policy.

Rejected `Risk_Score` is FICO before November 5, 2013 and VantageScore after that
date according to `data/LCDataDictionary.xlsx`, which does not identify the
VantageScore version/range. Numeric rejected screens are therefore reported
separately, with no inferred repayment outcomes or claimed creditworthy demand.
The optional `--assume-comparable-scores`, `--rejected-loss-rate`, and
`--take-up-rate` parameters are explicit user-supplied modeling assumptions;
they are disabled by default and never represented as measured results.

## Read-only SQL tools

`score_cutoff_tools.py` exposes an allowlist of operations described in
`score_cutoff_tool_dictionary.json`. The report runner executes the aggregate
operation once, then reuses the result for calculations and model context.
The model is an interpreter of those results, not an autonomous SQL agent.

| Tool | Purpose |
| --- | --- |
| `inspect_sources` | Check table engines and required columns without scanning records. |
| `preview_score_cutoff_queries` | Return SQL templates and parameters without connecting. |
| `get_score_cutoff_aggregates` | Read disjoint score bands from both tables in one consistent snapshot. |
| `compare_score_cutoffs` | Read aggregates and calculate comparisons and an optional historical candidate. |

```powershell
.\.venv\Scripts\python.exe .\score_cutoff_tools.py --list-tools
# MYSQL_PASSWORD must be available locally for a live tool call:
.\.venv\Scripts\python.exe .\score_cutoff_tools.py --call inspect_sources
```

Python callers can use `ScoreCutoffTools(DatabaseConfig(...))` for custom source
settings or inject a trusted `connection_factory(config)`. Model-shaped arguments
cannot supply arbitrary SQL, credentials, or table names. Invalid arguments fail
before connection. Database exceptions are sanitized and connections are closed.
Do not call both aggregate and comparison tools for the same question: each
would rescan both sources.

## Importing real source data separately

The report never imports CSVs. `import_csv_to_mysql.py` remains a separate
administrative utility for loading the real source files if needed. `data/` is a
read-only Windows junction to the local Lending Club source folder; changing
files through it changes the originals. The authoritative dictionary is
`data/LCDataDictionary.xlsx`, not the older root copy.

```powershell
# Validate the first 100 eligible source rows without touching MySQL:
.\.venv\Scripts\python.exe .\import_csv_to_mysql.py --dry-run --rows 100

# Only when intentionally loading new or empty destination tables:
.\.venv\Scripts\python.exe .\import_csv_to_mysql.py
```

The importer preserves CSV headers and text in nullable utf8mb4 LONGTEXT columns
in InnoDB; empty fields become NULL. It prefers extracted CSVs, with gzip fallback,
and streams batches bounded by row count and server packet size. It validates
both headers and destination schemas before inserting. It never drops or
truncates tables. Nonempty destinations require explicit `--append`, which does
not deduplicate or resume an interrupted import. Batches commit independently;
earlier commits survive later failures, and a connection loss during commit can
leave that batch's outcome uncertain. Table creation commits separately.

## Validation

```powershell
# Offline tests; no running services or credentials needed:
.\.venv\Scripts\python.exe -m unittest discover -v
```

Unit tests use isolated fixtures and fake services only inside test modules;
those fixtures are not available as report data sources. They cover calculations,
SQL/read-only boundaries, importer behavior, model requests and validation,
credential exclusion, and failure preservation. A successful live report run
separately verifies the actual MySQL and Ollama integration.
