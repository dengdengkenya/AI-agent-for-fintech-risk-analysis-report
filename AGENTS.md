# Project guide

## Project status

Last updated: 2026-11-04.

This project generates historical Lending Club reports from existing MySQL
accepted/rejected tables. A local Ollama/Qwen analyst investigates the results by
calling allowlisted analysis tools and writes evidence-checked commentary; it also
answers questions (`ask`, `chat`). Reporting has no synthetic mode, sample CSV
input, or CSV fallback. The local schema is named `demo`; that name does not make
its imported loan tables synthetic.

`CLAUDE.md` is a relative symbolic link to `AGENTS.md`. Edit `AGENTS.md` to update
the shared instructions; preserve the link rather than creating a second copy.

The code is the `loan_analyst/` package. Each layer can be used or replaced alone:

| Path | Purpose |
| --- | --- |
| `loan_analyst/cli.py`, `__main__.py` | Command line: `python -m loan_analyst report \| ask \| chat \| render \| tools \| call \| sql \| snapshot`. |
| `loan_analyst/pipeline.py` | Workflows built from small steps and injectable `Services` (connections, model client, clock, terminal I/O). |
| `loan_analyst/settings.py` | Validated `DatabaseConfig`, `Scenario`, `ModelConfig`; TOML defaults (see `loan_analyst.example.toml`). |
| `loan_analyst/db.py` | Credentials, connections, read-only transactions and table metadata; knows nothing about loans. |
| `loan_analyst/cube.py` | Grouped snapshot SQL, `Cube`/`Snapshot`, cohort and band selection, snapshot files. |
| `loan_analyst/analyses/` | Analysis plug-ins (`overview`, `cutoffs`, `segments`, `rejected`), each with tools, facts and report tables; `definitions.py` holds citable domain definitions. |
| `loan_analyst/tools.py` | Tool registry: JSON schemas, argument validation and sanitized dispatch. |
| `loan_analyst/evidence.py` | Fact ledger and validation of model statements against cited facts. |
| `loan_analyst/llm.py`, `agent.py`, `prompts.py` | Loopback Ollama client, the tool-calling analyst loop, and editable prompts. |
| `loan_analyst/report.py` | Composable report sections and answer rendering. |
| `generate_framework_report.py` | Compatibility entry point, equal to `python -m loan_analyst report`. |
| `import_csv_to_mysql.py` | Separate streaming administrative importer for the real CSV sources; supports `--dry-run`. |
| `tests/` | Offline regression tests; fixtures and fake database/model services are confined to tests. |

On 2026-11-03 the former `score_cutoff_*.py` modules, `ollama_analysis.py` and
`score_cutoff_tool_dictionary.json` were folded into the package;
`python -m loan_analyst tools` prints the generated tool catalog. Do not
reintroduce demo sources or silently use saved/synthetic aggregates when a
database query fails.

Verified on 2026-11-04: 87 offline tests pass (Python 3.13.7 on Windows).
The user's live grouped read completed in 396.4s and saved snapshot
`20261104T010032Z-4dbb63a3`: `demo.accepted` 2,260,669 rows and `demo.rejected`
27,648,741 rows. Its content hash was verified locally. The subsequent model
failure was reproduced from that snapshot: `qwen3:4b` resolved to Qwen3 4B
Thinking 2507 and stopped with `done_reason=length` after 1,024 generated tokens.
Preflight now rejects this thinking-only variant before scanning MySQL; the
default is the explicit Instruct variant below. A live saved-snapshot report
with that model on Ollama 0.40.13 completed in 473.1s: two investigation rounds,
four tool calls, one answer repair, 12 validated statements and none withheld.
Prompt `analyst-v3` selects citations before drafting and copies quantitative
sentences from their source fact to reduce citation mismatches. All four
artifacts in `outputs/framework_report/` were checked for matching run/evidence
IDs and `render` passed revalidation. MySQL was not queried again. These checks
do not certify import completeness or deduplication.

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
.\.venv\Scripts\python.exe -m loan_analyst sql

# Live report: reads MySQL once, saves a snapshot, then the local analyst runs tools:
.\.venv\Scripts\python.exe -m loan_analyst report

# Reuse the last saved snapshot instead of scanning MySQL again:
.\.venv\Scripts\python.exe -m loan_analyst report --snapshot latest
.\.venv\Scripts\python.exe -m loan_analyst ask "Which cutoff keeps losses under 8%?" --snapshot latest

# Bounded CSV validation, separate from reporting:
.\.venv\Scripts\python.exe .\import_csv_to_mysql.py --dry-run --rows 100
```

Tests create temporary fixtures under `outputs/` and clean them up. A snapshot
scans each full table once and can take minutes; `--query-timeout` defaults to
600 seconds per SELECT. Every analysis and model tool call afterwards is computed
in memory. Run live validation when needed and report it separately from offline
checks. An offline pass does not establish current service availability or a
complete/deduplicated import.

## Reporting and model boundaries

- Defaults: `localhost:3306`, user `root`, schema `demo`, tables `accepted` and
  `rejected`. Credentials come from `MYSQL_PASSWORD` or a local terminal prompt,
  never code, documentation, config files, command-line arguments, model context,
  or evidence.
- Source tables must be InnoDB with the required columns; optional `grade` and
  `term` columns add segments when present. Both tables are checked before scans.
  Quote identifiers; the snapshot SQL has no bound values. Snapshot reads share one
  read-only repeatable transaction and roll back/close before any analysis.
- Only grouped counts and sums by cohort month, score and optional grade/term
  leave MySQL. Python calculates cohorts, bands, rates, coverage and candidate
  selection. Missing financial data are not zero. Open/unknown/legacy statuses
  are excluded from terminal outcome rates.
- Each read is saved under `outputs/snapshots/` with a content hash. Saved
  snapshots are used only when `--snapshot PATH|latest` is given, are checked
  against their hash and table names, and are never a fallback for a failed read.
- Rejected scores are not generally comparable to original accepted FICO.
  Rejected repayment outcomes are absent. Expansion parameters are explicit user
  assumptions (flags or config), disabled by default and never tool arguments;
  never present them as facts.
- Ollama defaults: `http://localhost:11434`, `qwen3:4b-instruct-2507-q4_K_M`, 300 seconds per request,
  temperature zero, thinking disabled, five-minute keep-alive, at most 4 tool
  rounds and 8 tool calls, and one answer repair. Preflight verifies an installed
  local, tool-capable model before database scans and rejects known thinking-only
  models that cannot honor disabled thinking, including aliases identified by
  GGUF metadata. Hybrid models that allow thinking to be disabled remain valid.
  A token-limit response is discarded before any partial tool call can execute.
- Endpoint must be loopback HTTP. Disable proxies and redirects; reject cloud
  models. Never fall back to an external AI service.
- The model may call only tools registered by `loan_analyst/analyses`; `tools.py`
  validates arguments against their schemas before execution. Tools compute over
  the in-memory snapshot, cannot reach MySQL, and accept no SQL, credentials,
  hosts, table names, paths or expansion assumptions. The model receives fact
  sentences only, never cube rows, SQL or connection settings.
- Every model statement cites fact IDs (`def.*`, `t<n>.*`). Numeric magnitudes and
  units in prose must appear in its cited facts, and accept/reject/approve/deny
  wording is refused. After one repair attempt, statements that still fail are
  withheld and listed in `ollama_analysis.json`; the run fails if the main answer
  cannot be validated. These checks are not semantic verification; label
  model-generated text.
- Model failures fail the run before replacing existing report outputs; the saved
  snapshot allows a quick rerun. `--no-ai` is an explicit MySQL-only mode and
  writes a disabled-status model evidence file. `sql` and `--print-sql` connect to
  neither service. `--demo` is unsupported everywhere.
- A report folder holds `report.md`, `aggregates.json` (settings, snapshot
  provenance, every report-step result and fact), `snapshot.json` (the grouped
  aggregates and SQL, with its content hash) and `ollama_analysis.json` (exact
  requests, tool definitions, tool calls and results, validated answer, withheld
  statements, model digest, usage). `aggregates.json` and `ollama_analysis.json`
  share one run ID and source evidence hash. Do not save model thinking or raw
  service error bodies. Read timestamps do not establish the repayment
  observation date.
- To review wording, edit `answer` in `ollama_analysis.json`, keep `model_draft`
  unchanged, describe the edits in `review`, then run
  `python -m loan_analyst render DIR`. Rendering re-validates the evidence and
  refuses unrecorded edits. Keep the original requests intact.
- Paths resolve relative to `PROJECT_ROOT` (the repository root, defined in
  `loan_analyst/__init__.py`). Reruns replace report artifacts; select another
  output directory to retain runs.

## Extending

- New analysis: add a module under `loan_analyst/analyses/` with pure
  `analyze`/`facts` functions and a `TOOLS` tuple of `tools.Tool` (JSON schema
  plus a handler returning `ToolOutput`), then list it in `analyses.MODULES`.
  Write every number the model may quote into a fact.
- New segment column: add a validated `Dimension` to `cube.DIMENSIONS` and a
  grouping in `analyses/segments.py`.
- Report layout: reorder or drop sections with `--sections` or the `[report]`
  config table; add renderers to `report.SECTIONS` and deterministic steps to
  `pipeline.REPORT_CALLS`.
- Wording: prompts live in `prompts.py` (bump `VERSION` when changing them) and
  citable definitions in `analyses/definitions.py`.
- Another local model server: pass any object with `preflight()` and
  `chat(messages, tools=, format=, num_predict=)` through `Services.model_factory`.
  It must stay loopback-only.

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
