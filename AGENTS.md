# Project guide

## Project status

Last verified: 2026-10-27.

This is a local Python demo for working with Lending Club accepted and rejected
loan CSVs. It includes reproducible sampling, a small MySQL preview, a streaming
MySQL importer, and a separate local Ollama / Qwen3 connectivity check.

`CLAUDE.md` is a relative symbolic link to `AGENTS.md`. Edit `AGENTS.md` to update
the shared instructions; preserve the link rather than creating a second copy.

Current entry points:

| File | Purpose and current defaults |
| --- | --- |
| `sample_csv.py` | Reservoir sampling of both extracted CSVs; 10,000 rows per file, seed `42`; writes to `processed_samples/`. Uses only the standard library. |
| `preview_csv.py` | Appends the first five accepted rows to `demo.accepted_preview` on MySQL. Each rerun appends again. |
| `import_csv_to_mysql.py` | Streams both full datasets into `demo.accepted` and `demo.rejected`; prefers extracted CSVs with gzip fallback; supports validation with `--dry-run`. |
| `test_import_csv_to_mysql.py` | 11 offline regression tests covering CSV parsing, text preservation, validation, batching, paths, and dry runs. |
| `test_ollama.py` | Checks local Ollama and generates a short reply using `qwen3:4b` at `http://localhost:11434`; uses only the standard library. |
| `README.md` | Setup and usage instructions; see the sample-path discrepancy below. |

Verified locally on the date above:

- All five source paths listed below exist.
- The two tracked CSVs under `processed_samples/` each contain 10,000 data rows;
  accepted has 151 columns and rejected has 9 columns.
- All 11 importer regression tests pass.
- An importer dry run validates the first 100 rows of each full source CSV.

These checks do not establish whether a full MySQL import has completed or
whether MySQL and Ollama are currently running. Check those separately when the
task depends on them. The Ollama script is a connectivity and generation check,
not a test of model accuracy or an integrated loan-analysis agent.

## Project data source

Use `data/` as this project's data source. It is a Windows directory junction
pointing to `C:\Users\kenny\Documents\lending-club-data`; files are not copied.
Resolve `data/` relative to the project root, not an arbitrary working directory.

Available inputs:

- `data/accepted_2007_to_2018Q4.csv.gz`
- `data/rejected_2007_to_2018Q4.csv.gz`
- `data/accepted_2007_to_2018q4.csv/accepted_2007_to_2018Q4.csv`
- `data/rejected_2007_to_2018q4.csv/rejected_2007_to_2018Q4.csv`
- `data/LCDataDictionary.xlsx`

Treat source data as read-only unless the user explicitly requests changes.
Writes through the junction change the original files. Save generated outputs
in `outputs/` and new derived datasets in `processed/` inside this project.
The existing sampler and tracked sample CSVs use `processed_samples/`; preserve
that established location unless a task calls for changing it.
Ignore temporary Excel lock files whose names begin with `~$`.
The CSV files are large; use streaming, selected columns, or chunked reads.

The junction is local to this computer and excluded from Git. If the project
is moved to another computer, recreate `data/` pointing to that computer's
data folder.

A tracked `LCDataDictionary.xlsx` also exists at the project root. Use the
dictionary under `data/` as the source reference; do not assume the two copies
remain synchronized.

## Environment and checks

Run commands from the project root in PowerShell. The existing `.venv` uses
Python 3.13.7. `requirements.txt` pins `mysql-connector-python==26.7.0`, the
project's only listed third-party dependency. Prefer the project interpreter:

```powershell
# Install dependencies when setting up or updating the environment.
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Offline regression tests; no MySQL server or credentials needed.
.\.venv\Scripts\python.exe -m unittest test_import_csv_to_mysql -v

# Read only the first 100 eligible rows per source without connecting to MySQL.
.\.venv\Scripts\python.exe .\import_csv_to_mysql.py --dry-run --rows 100
```

If `.venv` is missing, create it with `python -m venv .venv` first. Tests create
temporary fixtures under `outputs/` and clean them up. Use bounded dry runs for
routine validation; a full scan or sample regeneration reads the large files.

For an Ollama-specific task, run
`.\.venv\Scripts\python.exe .\test_ollama.py` with Ollama running and the model
installed. Its default generation timeout is 180 seconds and it keeps the model
loaded for five minutes. This is a separate service check, not an offline unit
test.

## MySQL behavior to preserve

- Connection defaults are `localhost:3306`, user `root`, database `demo`.
  Passwords come from a prompt or `MYSQL_PASSWORD`; keep credentials out of
  code, documentation, command-line arguments, and committed files.
- The full importer preserves CSV header names and source text using nullable
  `utf8mb4 LONGTEXT` columns in InnoDB tables. Empty CSV fields become SQL
  `NULL`. Numeric and date analysis requires explicit conversion later.
- Both input headers and existing destination schemas are checked before row
  insertion. Blank records and known accepted-loan funded-total footers are
  skipped; malformed records fail validation.
- The full importer requires new or empty destination tables unless `--append`
  is supplied. Append mode does not deduplicate or resume interrupted imports.
  The preview script appends on every run.
- The full importer commits batches independently (default maximum 1,000 rows,
  also bounded by the server packet limit). Earlier committed batches survive
  a later failure. A lost connection during commit can leave that batch's
  outcome uncertain. Table creation is committed separately by MySQL.
- Keep database values parameterized and identifiers quoted. Preserve the
  importer's checks for compatible schemas and nonempty tables. It never drops
  or truncates tables. A live import writes to MySQL; use `--dry-run` for CSV
  validation when database writes are not part of the task.

## Repository conventions and known documentation gap

- Resolve input and output paths relative to the project root, following
  `PROJECT_ROOT = Path(__file__).resolve().parent` in the existing scripts.
- `data/`, `outputs/`, `.venv/`, and Python bytecode are ignored by Git.
  `processed/` is ignored except for the two default sample filenames explicitly
  allowed in `.gitignore`. The current tracked samples are in
  `processed_samples/`.
- The README sampling section and `sample_csv.py` module docstring still name
  `processed/`, while the sampler's implementation writes `processed_samples/`.
  Use the actual implementation and existing files when locating samples; keep
  code, documentation, and ignore rules aligned when changing sample paths.
- Keep large source data, generated outputs, and local environment files out of
  commits. Keep checks appropriate to the change and report service-dependent
  validation separately from offline tests.
