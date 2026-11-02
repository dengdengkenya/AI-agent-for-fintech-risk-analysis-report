# Project guide

## Project status

Last updated: 2026-11-02.

This project generates historical Lending Club reports from existing MySQL
accepted/rejected tables and adds local Ollama/Qwen interpretation. Reporting
has no synthetic mode, sample CSV input, or CSV fallback. The local schema is
named `demo`; that name does not make its imported loan tables synthetic.

`CLAUDE.md` is a relative symbolic link to `AGENTS.md`. Edit `AGENTS.md` to update
the shared instructions; preserve the link rather than creating a second copy.

| File | Purpose |
| --- | --- |
| `generate_framework_report.py` | Primary MySQL/Qwen runner; writes `report.md`, `aggregates.json`, and `ollama_analysis.json` to `outputs/framework_report/`. |
| `score_cutoff_simulation.py` | Shared query construction, deterministic calculations and Markdown rendering; CLI delegates to the same runner with `outputs/score_cutoff/`. |
| `score_cutoff_sql.py` | MySQL connection, schema checks, and aggregates in a read-only consistent snapshot. |
| `score_cutoff_tools.py` | Allowlisted JSON-callable MySQL operations and host-controlled configuration. |
| `score_cutoff_tool_dictionary.json` | Function argument schemas, output definitions, and interpretation rules. |
| `ollama_analysis.py` | Loopback-only Ollama preflight, structured analysis, evidence validation and rendering. |
| `import_csv_to_mysql.py` | Separate streaming administrative importer for the real CSV sources; supports `--dry-run`. |
| `test_*.py` | Offline regression tests; fixtures and fake database/model services are confined to tests. |

The sample generator, CSV preview, standalone Ollama greeting check, and tracked
sample CSVs have been removed. Do not reintroduce demo sources or silently use
saved/synthetic aggregates when a database query fails.

Verified on 2026-11-02: 60 offline tests pass. A live read of `demo.accepted`
counted 2,260,669 rows and `demo.rejected` counted 27,648,741 rows. Local
`qwen3:4b` generated analysis from that snapshot; the delivered report's wording
was reviewed, with its original model draft retained in the evidence file.
These checks do not certify import completeness or deduplication.

## Environment and validation

Run commands from the project root in PowerShell. Use `.venv/Scripts/python.exe`
(Python 3.13.7). `requirements.txt` pins `mysql-connector-python==26.7.0`, the only
third-party dependency. The model client uses Python's standard library.

```powershell
# Set up only if .venv is absent:
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# All offline regression tests; no services or credentials needed:
.\.venv\Scripts\python.exe -m unittest discover -v

# SQL preview; no database/model connection or output writes:
.\.venv\Scripts\python.exe .\generate_framework_report.py --print-sql

# Live report: reads MySQL and invokes local Qwen; writes local report files:
.\.venv\Scripts\python.exe .\generate_framework_report.py

# Bounded CSV validation, separate from reporting:
.\.venv\Scripts\python.exe .\import_csv_to_mysql.py --dry-run --rows 100
```

Tests create temporary fixtures under `outputs/` and clean them up. Report
queries scan the full tables even with cohort filters and can take minutes;
`--query-timeout` defaults to 600 seconds per SELECT. Run live validation when
needed and report it separately from offline checks. An offline pass does not
establish current service availability or a complete/deduplicated import.

## Reporting and model boundaries

- Defaults: `localhost:3306`, user `root`, schema `demo`, tables `accepted` and
  `rejected`. Credentials come from `MYSQL_PASSWORD` or a local terminal prompt,
  never code, documentation, command-line arguments, model context, or evidence.
- Source tables must be InnoDB with the required columns. Both are checked
  before scans. Use parameterized values and quoted identifiers. Report reads
  share one read-only repeatable snapshot and roll back/close before generation.
- Only grouped counts, sums, and month bounds leave MySQL. Python calculates
  all rates, coverage and candidate selection. Missing financial data are not
  zero. Open/unknown/legacy statuses are excluded from terminal outcome rates.
- Rejected scores are not generally comparable to original accepted FICO.
  Rejected repayment outcomes are absent. Optional expansion parameters are
  explicit user assumptions, disabled by default; never present them as facts.
- Ollama defaults: `http://localhost:11434`, `qwen3:4b`, 300 seconds per request,
  at most one validation retry, temperature zero, thinking disabled, five-minute
  keep-alive. Preflight verifies an installed local model before database scans.
- Endpoint must be loopback HTTP. Disable proxies and redirects; reject cloud
  models. Never fall back to an external AI service. No model-directed SQL or
  tool execution occurs. Qwen receives only bounded deterministic aggregate facts.
- Require short structured sections and valid evidence references. Numeric
  magnitudes/units in prose must appear in cited facts; Python also renders those
  facts. These checks are not semantic verification; label model-generated text.
  If reviewing/editing generated prose, retain the original as `model_draft` and
  record the edits in `review`; keep the original request intact.
- Model failures fail the run before replacing existing outputs. `--no-ai` is an
  explicit MySQL-only mode and writes a disabled-status model evidence file.
  `--print-sql` connects to neither service. `--demo` is unsupported everywhere.
- All three artifacts carry one run ID and source evidence hash. Preserve the
  exact model request, model digest, validated response, and usage for review;
  do not save model thinking or raw service error bodies. Read timestamps do not
  establish the underlying repayment observation date.
- Paths resolve relative to `PROJECT_ROOT = Path(__file__).resolve().parent`.
  Reruns replace report artifacts; select another output directory to retain runs.

## Source data and importer safeguards

Reporting uses MySQL only. `data/` remains the source for the separate importer
and dictionary reference. It is a Windows directory junction to
`C:\Users\kenny\Documents\lending-club-data`; writes through it change originals.
Treat source data as read-only unless explicitly instructed otherwise. Use
streaming or bounded reads of the large CSVs. Ignore Excel locks named `~$*`.

Inputs:

- `data/accepted_2007_to_2018Q4.csv.gz`
- `data/rejected_2007_to_2018Q4.csv.gz`
- `data/accepted_2007_to_2018q4.csv/accepted_2007_to_2018Q4.csv`
- `data/rejected_2007_to_2018q4.csv/rejected_2007_to_2018Q4.csv`
- `data/LCDataDictionary.xlsx`

Use the dictionary under `data/` as authoritative; the tracked root workbook
may be older. Recreate the ignored junction on another computer. Save generated
outputs in `outputs/`, and any new derived data in `processed/`. Those folders,
`processed_samples/`, `.venv/`, and Python bytecode are ignored by Git. Keep
large source data and local environment files out of commits.

The importer preserves exact header names and source text in nullable utf8mb4
LONGTEXT columns in InnoDB; empty CSV fields become SQL NULL. It validates both
input headers and existing schemas before inserting and skips blank records and
known accepted-loan funded-total footers. Malformed records fail validation.

The importer requires new/empty tables unless `--append` is explicit. Append
does not deduplicate or resume interrupted imports. It never drops or truncates
tables. Batches (default up to 1,000 rows, also packet-bounded) commit independently;
earlier commits survive later failures. A lost connection during commit can
leave that batch uncertain. MySQL commits table creation separately. Keep
values parameterized, identifiers quoted, and these checks intact. Use dry runs
when database writes are outside the task.
