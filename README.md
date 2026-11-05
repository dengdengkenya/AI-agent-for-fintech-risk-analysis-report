# MySQL loan reports with a local tool-calling analyst

**New to the code?** Start with the [developer guide](docs/DEVELOPER_GUIDE.md).
It walks through one report, explains the main data structures, and shows where
to make common changes without needing a running database or model.

Generate reports from the existing MySQL tables `demo.accepted` and
`demo.rejected`, and ask questions about them in plain English. The schema is
named `demo`, but these are the real imported loan tables. Reporting has no
synthetic mode, sample-file input or CSV fallback.

A local Qwen model (via Ollama) acts as the analyst. It does more than describe a
finished table: it chooses analysis tools, such as another cohort, a score-band
breakdown, vintages, grades or a loss-ceiling sweep, runs them, and writes
findings. Every statement cites the facts it relies on, and Python checks the
statements before anything is shown.

## Quick start

Run from this project in PowerShell. MySQL 8.0+ and local Ollama must be running.

```powershell
# First-time setup only, if the environment is missing:
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
ollama pull qwen3:4b-instruct-2507-q4_K_M

# Full report; enter the MySQL password at the prompt (or set MYSQL_PASSWORD):
.\.venv\Scripts\python.exe -m loan_analyst report
```

The first run reads both tables once (this can take minutes) and saves that
read as a snapshot. After that, everything can reuse the snapshot and finish in
seconds plus model time:

```powershell
.\.venv\Scripts\python.exe -m loan_analyst ask "Which cutoff keeps losses under 8% for 2012-2015 loans?" --snapshot latest
.\.venv\Scripts\python.exe -m loan_analyst chat --snapshot latest
```

The default model is `qwen3:4b-instruct-2507-q4_K_M`, an explicit non-thinking
variant. Choose another installed local model with `--ollama-model` or the
`[model]` config table; it must support tools and allow thinking to be disabled.

### Recovering from an incomplete model response

A saved snapshot survives model failures. If a run stops with "Model response
was incomplete", reuse it instead of scanning MySQL again. The local `qwen3:4b`
alias was found to point to Qwen3 4B Thinking 2507 on 2026-11-04; it exhausted the
1,024-token investigation budget despite `think: false`. Known thinking-only
models are now rejected during preflight, before any database scan.

Install the explicit Instruct variant and select it when rerunning, including
when an existing `loan_analyst.toml` still names the old alias:

```powershell
ollama pull qwen3:4b-instruct-2507-q4_K_M
.\.venv\Scripts\python.exe -m loan_analyst report --snapshot latest --ollama-model qwen3:4b-instruct-2507-q4_K_M
```

Update `[model].model` in your local config to keep that choice for future runs.
Increasing `--ollama-timeout` does not change the generated-token limit or make
a thinking-only model obey `think: false`.

## Everyday commands

All commands are `python -m loan_analyst <command>`; each has `--help`.

| Command | What it does |
| --- | --- |
| `report` | Reads MySQL once, runs every analysis, lets the analyst investigate, writes the report folder. |
| `report --snapshot latest` | Same, from the newest saved snapshot; MySQL is not queried. |
| `ask "QUESTION"` | Answers one question; prints the answer with numbered evidence and saves a transcript. |
| `chat` | Interactive session over one snapshot; follow-up questions keep context. `/tools`, `/help`, `/quit`. |
| `render DIR` | Re-renders `report.md` after a human review of the wording (see below). |
| `tools`, `call NAME '{json}'` | Prints the tool catalog, or runs one tool and prints its facts as JSON. |
| `sql` | Prints the exact snapshot SQL without connecting. |
| `snapshot` | Reads MySQL and saves a snapshot without running a model. |

Previous commands still map cleanly:

| Before | Now |
| --- | --- |
| `generate_framework_report.py [options]` | Unchanged; it runs `report [options]`. |
| `score_cutoff_simulation.py` | `report --output-dir outputs/score_cutoff` |
| `score_cutoff_tools.py --list-tools` | `tools` |
| `score_cutoff_tools.py --call NAME --arguments JSON` | `call NAME JSON` |
| `--print-sql` | `sql` (or `report --print-sql`) |

Connection defaults: `localhost:3306`, user `root`, database `demo`, tables
`accepted` and `rejected`. Override with `--host`, `--port`, `--user`,
`--database` (or `--schema`), `--accepted-table` and `--rejected-table`.
Passwords come only from `MYSQL_PASSWORD` or the terminal prompt. A SELECT-only
account is sufficient. Scenario options are unchanged: `--cutoffs`, `--baseline`,
`--start-month`, `--end-month`, `--max-loss-rate`, `--min-outcomes`, and the
explicit expansion assumptions `--assume-comparable-scores`,
`--rejected-loss-rate` and `--take-up-rate`.

## How the analyst works

1. **One snapshot.** Each table is scanned once in a read-only transaction and
   grouped by cohort month, score and (when present) grade and term. Only those
   counts and sums leave MySQL. The snapshot is saved to `outputs/snapshots/`.
2. **Report steps.** Python runs the standard analyses over the snapshot: data
   coverage, cutoff comparison, score bands, issue years, grade, term and
   rejected score eras. Each produces tables plus plain-sentence facts.
3. **Investigation.** The model sees the key facts and may call tools for up to
   4 rounds and 8 calls (`--max-tool-rounds`, `--max-tool-calls`). Tools compute
   in memory, so the model never triggers another database scan.
4. **Evidence-checked answer.** The model chooses supporting fact IDs before
   drafting each headline, finding, caveat and next step. Quantitative sentences
   are requested as exact copies from one cited fact. Python checks that every number is
   copied from the cited facts and that no statement calls a cutoff an
   approval or rejection. Failing statements get one repair attempt; any that
   still fail are withheld and listed. If the headline cannot be validated, the
   run fails and earlier report files are left untouched.

| Tool | Purpose |
| --- | --- |
| `describe_data` | Tables, row counts, month ranges, available segments, cohort exclusions. |
| `compare_score_cutoffs` | Retained loans, principal, charge-offs and loss proxy per cutoff, with an optional loss ceiling. |
| `segment_performance` | Disjoint breakdown by score band (what each cutoff step removes), issue year (seasoning), grade or term. |
| `sweep_loss_ceilings` | How the screening candidate moves across several loss ceilings. |
| `rejected_applications` | Rejected numeric screens by score era (FICO before 2013-11, VantageScore after) or application year. |

The model sees fact sentences only. It never sees borrower records, SQL,
credentials or connection settings, and it cannot set the expansion
assumptions. The endpoint must be loopback HTTP; proxies, redirects and cloud
models are blocked. `--max-tool-rounds 0` limits the model to interpreting the
standard facts. `--num-ctx` (default 16384 tokens) leaves room for tool rounds;
lower it on machines with little memory, together with fewer tool calls.

## Outputs

A report folder (default `outputs/framework_report/`; change with
`--output-dir`) contains:

| File | Contents |
| --- | --- |
| `report.md` | Summary, analyst findings with numbered citations, calculated tables, the investigation steps, cited evidence and definitions. |
| `aggregates.json` | Settings, snapshot provenance, every report-step result and fact, run ID and source evidence hash. |
| `snapshot.json` | The grouped MySQL aggregates and SQL behind this run; reuse with `--snapshot <folder>/snapshot.json`. |
| `ollama_analysis.json` | Exact model requests, tool definitions, tool calls and results, the validated answer, withheld statements, model digest and usage (or a disabled status). |

Reruns replace these four files. `ask` and `chat` save Markdown and JSON
transcripts under `outputs/ask/` and `outputs/chat/`. `--no-ai` explicitly
writes the calculated report without model commentary. `--sections` chooses
and orders report sections.

## Team defaults

Copy `loan_analyst.example.toml` to `loan_analyst.toml` to set shared defaults
(tables, cutoffs, cohort, loss ceiling, model, report sections) without long
command lines. It loads automatically; `--config FILE` selects another file, and
command-line flags always win. Config files may not contain passwords.

## Reviewing the wording

Model text is labeled for review. To correct it, edit `answer` in
`ollama_analysis.json`, leave `model_draft` as generated, add a `review` entry
describing the change, then run:

```powershell
.\.venv\Scripts\python.exe -m loan_analyst render outputs/framework_report
```

Rendering refuses edits that are not recorded in `review` and re-runs the
evidence checks, so reviewed text cannot quote numbers that are absent from its
cited facts.

## Reading the results

Accepted loans use the midpoint of original `fico_range_low` and
`fico_range_high`. Retained counts and funded principal cover all qualifying
accepted loans. Performance uses only exactly `Fully Paid` and `Charged Off`.
The principal-loss proxy is unrecovered principal on financially complete
charged-off loans divided by funded principal of all financially complete
terminal loans. Fully paid loans contribute zero loss; open loans do not enter
the loss denominator. Costs, interest, fees and timing are excluded. This is
historical cohort analysis, not an annualized loss measure or a forecast.

Score bands hold only the loans between adjacent cutoffs, so they show what each
step up in the cutoff removes. Issue-year tables show seasoning: years with a
low terminal share were mostly still open, so their loss proxies cover only
resolved loans.

Malformed numeric text is excluded before casting. Missing financial values are
not zero. Rates require at least 100 relevant outcomes by default
(`--min-outcomes`); this is a reporting threshold, not statistical validation.
Read times do not establish when the underlying repayment outcomes were
observed. The report cannot certify that imports are complete or deduplicated.

The baseline defaults to 660 for comparison; it is not an established policy.
No cutoff is selected unless you supply your own `--max-loss-rate` as a fraction.
A candidate must meet that historical accepted-loss ceiling, have enough
outcomes, and have complete terminal financial coverage. It is not a lending
recommendation. Cutoff comparisons replay historical loans and do not measure
approval rates or prove a causal effect of changing policy.

Rejected `Risk_Score` is FICO before November 5, 2013 and VantageScore after that
date according to `data/LCDataDictionary.xlsx`, which does not identify the
VantageScore version/range. Rejected screens are therefore reported separately
and by score era, with no inferred repayment outcomes or claimed creditworthy
demand. The optional expansion parameters are explicit user-supplied modeling
assumptions; they are disabled by default and never represented as measured
results.

## Customizing and extending

The `loan_analyst/` package is split so that each piece changes independently:
add an analysis module and it becomes a tool for the analyst, a `call` command
and (with a renderer) a report section; add a segment column in one place;
reorder report sections with a flag; swap the model client or database
connection through `Services`. See "Extending" in `AGENTS.md`.

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

Unit tests use isolated fixtures and fake services only inside `tests/`; those
fixtures are not available as report data sources. They cover calculations, the
cube and snapshot files, SQL/read-only boundaries, tool validation, the
tool-calling loop with a scripted model, evidence checks, the report, ask, chat
and render workflows, importer behavior, credential exclusion, and failure
preservation. A successful live report separately verifies the actual MySQL and
Ollama integration.
