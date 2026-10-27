# Lending Club CSV preview

## Score cutoff simulation: growth versus losses

`score_cutoff_simulation.py` reads `demo.accepted` and `demo.rejected` using
server-side aggregates and writes a local narrative report. It compares retained
accepted-loan counts and funded principal, historical charge-offs and unrecovered
principal, and rejected demand at different numeric score thresholds. It uses
the existing MySQL dependency and requires MySQL 8.0 or later.

```powershell
# Read the existing MySQL tables; enter your password locally when prompted:
.\.venv\Scripts\python.exe .\score_cutoff_simulation.py

# Example historical cohort and risk ceiling (6% is an assumption, not policy):
.\.venv\Scripts\python.exe .\score_cutoff_simulation.py `
  --cutoffs 600,640,660,680,700,720,740 --baseline 660 `
  --start-month 2012-01 --end-month 2015-12 --max-loss-rate 0.06

# Run a walkthrough without MySQL, using clearly labeled artificial aggregates:
.\.venv\Scripts\python.exe .\score_cutoff_simulation.py --demo `
  --max-loss-rate 0.06 --output-dir outputs/score_cutoff_demo
```

The default outputs are `outputs/score_cutoff/report.md` and `aggregates.json`.
The JSON contains the aggregate evidence, calculations, assumptions and SQL for
review. Reruns replace those files; choose `--output-dir` to preserve separate
runs. Relative output paths resolve from the project root. The report is generated
with deterministic Python prose; it does not need Ollama or send data to an AI
service. `--demo` results are synthetic and must not be presented as MySQL findings.

Connection options match the importer: `--host`, `--port`, `--user`, `--database`
(or `--schema`), `--accepted-table`, and `--rejected-table`. Passwords come only
from an interactive prompt or the local `MYSQL_PASSWORD` environment variable.
A SELECT-only account is sufficient; both sources must be InnoDB tables. The
script uses a consistent read-only transaction and never changes database data.
`--print-sql` shows query templates and parameters without a connection. Only
grouped counts, sums and month bounds reach Python; no borrower records are
exported. This is aggregate-only processing, not an anonymization guarantee.

Accepted scores use the midpoint of **original** FICO bounds. Performance uses
only `Fully Paid` and `Charged Off` loans; open, unknown and legacy statuses are
excluded. The loss proxy is `max(funded_amnt - total_rec_prncp - recoveries, 0)`
for financially complete charged-off loans, divided by funded principal across
all financially complete terminal loans. Fully paid loans contribute zero loss.
It excludes collection costs, interest and fees and is not an annualized or
accounting loss measure. Invalid text is excluded before numeric conversion;
missing financial values are never silently treated as zero. Coverage and
exclusions are shown. Rates require at least 100 relevant outcomes by default
(`--min-outcomes`), which is a demo stability check, not statistical validation.

The source dictionary (`data/LCDataDictionary.xlsx`) says rejected `Risk_Score`
is FICO before **November 5, 2013** and VantageScore after that date. It does not
specify the VantageScore version or range. The default report therefore shows
rejected numeric score screens separately and excludes scores outside 300–850;
it does not claim comparable scores or known repayment outcomes. To demonstrate
a hypothetical expansion, explicitly supply both assumptions:

```powershell
# Illustrative assumptions: equal score scales, 25% take-up, 15% lifetime loss.
.\.venv\Scripts\python.exe .\score_cutoff_simulation.py `
  --assume-comparable-scores --take-up-rate 0.25 --rejected-loss-rate 0.15
```

This scenario applies take-up to screened rejected applications with valid
requested amounts, assumes full requested funding, and estimates new losses
from the supplied rate. It includes all qualifying rejects at each threshold,
not just those between that threshold and baseline. It does not infer rejected
risk from accepted borrowers or add unlike observation windows into a portfolio
loss forecast. The optional `--max-loss-rate` chooses a candidate based **only on
historical accepted losses**, with complete terminal financial coverage; it is
not a recommendation to approve rejected applicants.

The baseline defaults to 660 as an illustrative comparison, not verified policy.
Growth means retained historical funded loans, not a measured approval rate.
Monthly filters use accepted issue month and rejected application month; they
do not reconstruct repayment performance as of a historical date. Resolved-loan
selection, different seasoning, incomplete/duplicate imports, and score changes
limit interpretation. Use a sufficiently mature, comparable cohort for review.
The script does not certify import completeness or calculate profit.

Each source is scanned once; text conversion and aggregation on large full
tables can take several minutes or longer. Month filters do not avoid that scan.
`--query-timeout` defaults to 600 seconds per query. Only aggregate result groups
are held in Python memory.

```powershell
# Offline arithmetic, data-boundary and report checks:
.\.venv\Scripts\python.exe -m unittest test_score_cutoff_simulation -v
```

## Test local Ollama / Qwen3 4B

With Ollama running and `qwen3:4b` installed, run from the project folder:

```powershell
python .\test_ollama.py
```

No extra Python packages are required. The script checks the server, confirms
the model is installed, sends a short prompt, prints the reply and elapsed time,
and verifies the model is loaded. Exit code `0` means all checks passed; `1`
means a check failed. This tests connectivity and text generation, not model
accuracy. The model is kept loaded for five minutes after the test.
The prompt uses Qwen's [`/no_think` switch](https://qwenlm.github.io/blog/qwen3/#advanced-usages)
to request a brief reply; any reasoning emitted by the installed model is kept
separate from the printed answer.

The defaults are `http://localhost:11434`, `qwen3:4b`, and a 180-second generation
timeout. Override them with `--host`, `--model`, or `--timeout`, for example:

```powershell
python .\test_ollama.py --model qwen3:4b --timeout 300
```

If the server is unavailable, open the Ollama app or run `ollama serve`. If the
model is missing, install it with `ollama pull qwen3:4b` and rerun the test.
The script uses Ollama's [generate](https://docs.ollama.com/api/generate),
[model list](https://docs.ollama.com/api/tags), and
[running models](https://docs.ollama.com/api/ps) endpoints.

## Create sample CSVs for the repository

Run from the project folder (no additional Python packages needed):

```powershell
python .\sample_csv.py
```

This writes `processed/accepted_sample_10000.csv` and
`processed/rejected_sample_10000.csv`, each with one header and 10,000 data rows.
All source columns and text values are preserved. Reservoir sampling selects
records uniformly without replacement across each full file, keeping only the
sample in memory. Blank records and the accepted file's funded-total footers
are excluded. Selected records are saved in their original relative order.

The default seed is `42`; the same inputs and seed reproduce the same sample.
Use `--rows` and `--seed` to change these settings. Each run replaces the matching
sample files. Source paths are resolved relative to the script and opened
read-only. Scanning both large files can take a few minutes.

Git allows the two default sample files; other contents of `processed/` remain
ignored. After reviewing the samples, add them and the script with:

```powershell
git add sample_csv.py processed/accepted_sample_10000.csv processed/rejected_sample_10000.csv .gitignore README.md
```

## Import accepted and rejected CSVs into MySQL

`import_csv_to_mysql.py` streams both full source files from `data/` into separate
tables: `demo.accepted` (151 columns) and `demo.rejected` (9 columns). It prefers
the extracted CSVs and falls back to `.csv.gz` files without extracting them.
Source files are opened read-only, and relative input paths are resolved from
the project folder even when you launch the script elsewhere.

```powershell
# Install the existing project dependency once:
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Check the first 100 rows from each file without connecting to MySQL:
.\.venv\Scripts\python.exe .\import_csv_to_mysql.py --dry-run --rows 100

# Import both complete datasets; enter your MySQL password at the prompt:
.\.venv\Scripts\python.exe .\import_csv_to_mysql.py
```

The default connection is `localhost:3306`, user `root`. Use `--host`, `--port`,
`--user`, and `--database` (or `--schema`) to change it. You can set
`MYSQL_PASSWORD` for unattended execution; passwords are never saved in the
script or passed as command-line arguments. The MySQL user needs permission to
create the database/tables if missing, read the destination tables, and insert.

To import the existing sample files instead, choose separate sample tables:

```powershell
.\.venv\Scripts\python.exe .\import_csv_to_mysql.py `
  --accepted-csv processed_samples/accepted_sample_10000.csv `
  --rejected-csv processed_samples/rejected_sample_10000.csv `
  --accepted-table accepted_sample --rejected-table rejected_sample
```

Use `--rows 100` to import at most 100 eligible rows per file. Omit `--rows` to
import every row. `--batch-size 1000` controls the maximum rows per transaction;
batches also shrink to fit the server's packet limit. Progress is printed during
imports. Both input headers and existing destination schemas are checked before
any data rows are inserted.

CSV headers become the exact MySQL column names, including the rejected file's
spaces and hyphens. All columns are nullable `LONGTEXT` with `utf8mb4` encoding;
numbers, percentages, dates, and leading zeros remain source text. Empty CSV
fields become SQL `NULL`. This creates raw import tables; cast fields when you
need numeric or date calculations. Blank records and accepted funded-total
footers are skipped; malformed records cause an error.

By default, both destination tables must be new or empty. Nonempty tables are
rejected to prevent accidental duplicate imports. `--append` explicitly permits
adding rows and does not deduplicate or resume an interrupted import. Existing
tables must have the same columns, in CSV order, as nullable `utf8mb4 LONGTEXT`,
and use InnoDB. The script never drops or truncates tables.

Each successful batch is committed separately. If a later batch fails or you
interrupt the run, earlier batches (including any completed table) remain. The
current uncommitted batch is rolled back when possible. If the connection fails
during a commit, inspect the table because that batch's outcome may be unknown.
For a clean retry, use new table names or manually clear the intended tables;
do not use `--append` as a resume mechanism. MySQL commits table creation
separately from inserted rows.

```sql
SELECT COUNT(*) FROM demo.accepted;
SELECT COUNT(*) FROM demo.rejected;
SELECT id, loan_amnt, loan_status FROM demo.accepted LIMIT 5;
SELECT `Amount Requested`, `Application Date`, `Loan Title`
FROM demo.rejected LIMIT 5;
```

Batch inserts use MySQL Connector/Python's
[`executemany`](https://dev.mysql.com/doc/connector-python/en/connector-python-api-mysqlcursor-executemany.html)
with parameterized values and explicit
[`commit`](https://dev.mysql.com/doc/connector-python/en/connector-python-connectargs.html#connector-python-connectargs-transactions)
calls.

Run the CSV importer regression checks with:

```powershell
python -m unittest test_import_csv_to_mysql -v
```

## MySQL preview

`preview_csv.py` reads the CSV header and first five data rows from the project's
`data/` directory, then appends the data rows to MySQL table
`demo.accepted_preview`. It creates the schema and table if they do not exist.
The source CSV is opened read-only; the full file is never loaded.

## Run in PowerShell

From this project folder:

```powershell
# First-time setup, if the virtual environment does not already exist:
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Connect to localhost:3306 as root and enter the password at the prompt:
.\.venv\Scripts\python.exe .\preview_csv.py

# Import ten rows instead, or choose a different destination:
.\.venv\Scripts\python.exe .\preview_csv.py --rows 10
.\.venv\Scripts\python.exe .\preview_csv.py --database demo --table accepted_preview
```

Other options are `--host`, `--port`, and `--user`. `--schema` is an alias for
`--database`: MySQL uses these terms interchangeably. Passwords are not saved
in the script. For unattended execution, the script also accepts a
`MYSQL_PASSWORD` environment variable.

Each CSV column becomes a nullable `LONGTEXT` column, preserving the source text
without inferring numeric or date types from a small sample. Empty CSV fields
become SQL `NULL`. The header supplies column names and is not inserted as data.
Each run appends rows, so running the same preview again creates duplicates.
An existing table must have compatible columns. Insert failures roll back the
rows from that run; schema/table creation is committed separately by MySQL.

## Run from a Python console

Import the file instead of pasting its contents into the console. This gives it
its own `__file__` value and avoids reading the console's startup arguments:

```python
import preview_csv

preview_csv.main([])                  # Five rows; prompts for the password.
preview_csv.main(["--rows", "10"])     # Ten rows; prompts for the password.
```

## Check the result in MySQL

```sql
SELECT COUNT(*) FROM demo.accepted_preview;
SELECT id, loan_amnt, term, int_rate, grade, loan_status
FROM demo.accepted_preview
LIMIT 5;
```
