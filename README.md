# Local report agent demo (小贷风控 / 营销报告助手)

A demo AI agent that writes monthly analysis reports in Chinese for the risk
model team and the marketing model team of a small-loan lender.

- **Data never leaves the machine.** The model is an open-weight Qwen model
  served by [Ollama](https://ollama.com) on the same laptop or server. There
  is no code path to Claude, OpenAI or any hosted LLM API.
- **The model never calculates.** SQL and pandas compute every number
  (vintage, roll rates, KS/AUC/PSI, funnel, CAC, ROI, response lift). The
  model picks which analyses fit the request and writes the narrative from
  short summary tables. Afterwards every number in the narrative is checked
  against the computed results, and unmatched numbers are listed for review.
- **One setting changes the model.** `OLLAMA_HOST` and `OLLAMA_MODEL` (in
  `.env` or the environment) switch between the laptop, an Aliyun GPU server,
  and a customer's server.
- **Synthetic data.** `report_agent/synthetic_data.py` generates a fake
  lender (45k loans issued 2025-01 to 2026-06, five acquisition channels, a
  credit score, monthly DPD buckets, an SMS reactivation campaign, and
  ~185k applications including rejected ones). No row is
  a real customer. Every report carries a 演示数据 warning.

### Run on the Windows laptop

1. Install Ollama for Windows from https://ollama.com/download, then in
   PowerShell: `ollama pull qwen3:4b` (about 2.5 GB; `qwen3:8b` writes better but is
   about twice as slow on CPU). If the download is slow,
   see step 5 of the [Aliyun guide](docs/aliyun_ecs_setup.md) for pulling
   from ModelScope (魔搭) instead.
2. In this project folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env          # edit OLLAMA_MODEL here to try other models

.\.venv\Scripts\python.exe -m report_agent check        # is the model there, how fast is it
.\.venv\Scripts\python.exe -m report_agent report --team risk
.\.venv\Scripts\python.exe -m report_agent report --team marketing --request "重点看各渠道ROI和短信召回效果"
```

The first `report` builds the synthetic database in `outputs/` (a few
seconds). Reports are written to `outputs/reports/` as Markdown, with the
computed tables, the number check and a timing table at the end. The console
also prints timing per step: model load time, prompt tokens per second and
output tokens per second. On a CPU-only laptop expect a few minutes per
report with a 4B-8B model; this is an estimate, not a measurement.

### Policy reports: cutoff simulation, approval funnel, applicant drift

`--team policy` (风险策略团队) runs three reports on *all* applications,
approved and rejected, using only the fields both have at application time
(amount, month, score, DTI, region, years employed):

- **授信策略模拟 (cutoff_simulation)**: move the score cutoff and/or the DTI
  limit and see approvals, approved amount and the bad rate before and
  after, the swap-in / swap-out groups, and a cutoff sensitivity table.
  Swap-outs were approved, so their bad rate is observed. Swap-ins were
  rejected and never repaid anything, so their bad rate is a reject-inference
  estimate from accepted loans (logistic regression on score + DTI) and is
  always labelled 估算, with a +30% stress case. Manual exceptions stay
  approved; rejected applicants who already passed the old rule are treated
  as rejected for another reason and are not swapped in.
- **申请审批漏斗 (approval_funnel)**: volume and approval rate over time, by
  score band and by region.
- **申请客群偏移 (applicant_drift)**: PSI of recent applicants against the
  same months a year earlier, and against the applicants actually approved.

```powershell
# the scenario can come from the request (the model or a regex reads it) ...
.\.venv\Scripts\python.exe -m report_agent report --team policy --request "如果把评分线提高到620分，通过率和坏账率会怎样？"
# ... or from flags, which win over the request
.\.venv\Scripts\python.exe -m report_agent report --team policy --score-cutoff 580 --max-dti 50
```

With no scenario the simulation tries the current cutoff + 20.

**On the real Lending Club files.** `load-lendingclub` streams
`data/accepted_2007_to_2018Q4.csv.gz` and `data/rejected_2007_to_2018Q4.csv.gz`
into `outputs/lending_club.db` (only the shared columns; bad = Charged Off,
Default or Late 31-120 days, unknown while Current). `--frac 0.1` keeps a
random 10% of both files, which keeps the approval rate right and makes
reports take seconds instead of minutes. Only the policy reports run on this
database; the others need the synthetic loans/performance tables.

```powershell
.\.venv\Scripts\python.exe -m report_agent load-lendingclub --frac 0.1
.\.venv\Scripts\python.exe -m report_agent report --team policy --db outputs\lending_club.db --score-cutoff 680
# quick check on the 10,000-row samples (approval rate is not real: both files are sampled to 10,000)
.\.venv\Scripts\python.exe -m report_agent load-lendingclub --accepted processed_samples\accepted_sample_10000.csv --rejected processed_samples\rejected_sample_10000.csv --db outputs\lc_sample.db
```

Lending Club's policy is not stored in the data, so the current cutoff and
DTI limit are inferred from approved loans (1st percentile of score, 99th
of DTI) and the report says so. Many rejected applications after 2016 have no
Risk_Score; they are left out of the simulation and the report states the share.
Tests: `python -m unittest test_report_agent_policy -v`.

Other commands:

```powershell
python -m report_agent list                                   # available analyses (risk / policy / marketing)
python -m report_agent report --team risk --no-llm            # numbers only, no model
python -m report_agent report --team risk --analyses vintage,score_monitoring
python -m report_agent build-data                             # regenerate synthetic data
```

### How it works

| Step | Who | File |
|---|---|---|
| Plan: choose analyses for the request (JSON) | local model | `report_agent/agent.py` |
| Compute: run SQL/pandas analyses | Python | `report_agent/analyses.py` |
| Write: Chinese narrative from tables and pre-computed facts | local model | `report_agent/agent.py` |
| Check: flag narrative numbers not found in results | Python | `report_agent/agent.py` |

When `--request` is empty the agent skips planning and uses the team's
default set of analyses. To add an analysis, write a function in
`analyses.py` returning an `AnalysisResult` and register it in `ANALYSES`.

### Run on Aliyun

See [docs/aliyun_ecs_setup.md](docs/aliyun_ecs_setup.md): a pay-as-you-go GPU
instance, Ollama, Qwen from ModelScope, and how to stop paying afterwards.

---

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
