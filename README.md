# Lending Club CSV preview

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
