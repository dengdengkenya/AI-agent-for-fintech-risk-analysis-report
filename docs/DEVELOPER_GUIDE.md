# Reading and changing the loan analyst

This application answers questions about historical loans. Python calculates
the numbers; a local language model chooses additional analyses and writes
commentary using those numbers. The model does not query the database or
calculate the report's rates.

You can understand and test the code without running MySQL or Ollama. Start
with the workflow below, then follow the reading order. The [README](../README.md)
covers operating the application, and [AGENTS.md](../AGENTS.md) records project
constraints and live validation history.

## Follow the data

```text
Command-line arguments + optional TOML config
    |
    v
Validated settings
    |
    v
MySQL grouped read OR an explicitly requested saved snapshot
    |
    v
Snapshot: accepted cube + rejected cube + source information
    |
    v
Analysis functions: calculated results + citable fact sentences
    |
    +--> Local model requests more analyses through the tool registry
    |        |
    |        v
    |    Model answer --> Python evidence checks
    |        |
    v        v
Report tables + validated commentary --> Markdown and JSON files
```

A **cohort** is the selected range of loan issue months (or rejected application
months). A **cutoff** retains scores at or above a threshold. A **score band**
contains scores in one range: below the first threshold, between adjacent
thresholds, or at and above the last threshold. These bands do not overlap.

The model is optional for `report --no-ai`. The data still come from MySQL, or
from a saved snapshot only when `--snapshot` is supplied. A failed database
read never silently switches to a saved file or test data.

## Read these pieces first

| Order | File and starting point | What to look for |
| --- | --- | --- |
| 1 | [pipeline.py](../loan_analyst/pipeline.py), `run_report` | The main steps and their order. Follow the named helpers as needed. |
| 2 | [settings.py](../loan_analyst/settings.py), `Scenario` | The user's analysis choices and the rules that validate them. |
| 3 | [analyses/common.py](../loan_analyst/analyses/common.py), `performance` | Which totals are used as denominators for each rate. |
| 4 | [analyses/cutoffs.py](../loan_analyst/analyses/cutoffs.py), `analyze` and `facts` | How calculations become results and then sentences the model can cite. |
| 5 | [tools.py](../loan_analyst/tools.py), `ToolOutput` and `ToolRegistry.call` | How named tools validate model arguments before running analysis code. |
| 6 | [agent.py](../loan_analyst/agent.py), `Analyst.run` | Investigation followed by answer writing and validation. |
| 7 | [tests/test_pipeline.py](../tests/test_pipeline.py) | Complete workflows with a fake database and scripted model. |

After that, read [cli.py](../loan_analyst/cli.py) for command routing,
[cube.py](../loan_analyst/cube.py) for SQL and snapshots, and
[report.py](../loan_analyst/report.py) for presentation. You do not need to
understand the SQL builder before changing a report heading.

## Know what each object holds

| Name | Meaning | Where it lives |
| --- | --- | --- |
| Grouped row | A dictionary containing group keys such as `period` and `score`, plus counts and sums. Its `rows` value counts source records; it is not one borrower record. | `cube.py` |
| `Cube` | All grouped rows for one source, either accepted or rejected. It can select a cohort and collect rows into score bands. | `cube.py` |
| `Snapshot` | Both cubes, source table information, query text, read timestamps and a content hash. Loading a saved snapshot verifies its hash and table names. | `cube.py` |
| `Scenario` | Cutoffs, baseline, cohort dates, minimum outcome count, loss ceiling and any explicit expansion assumptions. | `settings.py` |
| `ToolContext` | The snapshot and session scenario supplied by the application to an analysis tool. | `tools.py` |
| `ToolOutput` | A title, a structured `result`, a dictionary of `facts`, and optional `highlights`. The result feeds tables; facts feed model citations. | `tools.py` |
| `Ledger` | The collected facts and successful tool-call records. It assigns citation IDs such as `t1.scope`; definitions use IDs such as `def.loss_proxy`. | `evidence.py` |
| `RunOptions` | Settings for one command: source, scenario, model, output folder and related choices. | `pipeline.py` |
| `Services` | Replaceable access to external systems: database connections, model client, terminal input/output and a clock. Tests supply fakes here. | `pipeline.py` |

Most data passed between these layers are ordinary dictionaries. For a result's
exact fields, find the analysis function's return value and then its `facts`
function. Avoid confusing a `ToolOutput.result` dictionary with the model's
`answer`: one contains calculated data, the other contains statements and
their evidence IDs.

## Trace one report

1. `__main__.py` calls `cli.main`. The CLI loads defaults, parses flags and builds
   validated settings. It sends the `report` command to `pipeline.run_report`.
2. When AI is enabled, the pipeline checks the local model before reading the
   database. `obtain_snapshot` then loads an explicitly selected snapshot or
   reads MySQL and immediately saves the new snapshot.
3. For a live read, `read_snapshot` checks both tables before scanning either
   one. Both scans share one read-only transaction. The transaction and
   connection end before analysis begins.
4. `toolbox` creates a registry of allowed tools, their context and a fact
   ledger. `run_calls` executes `REPORT_CALLS`, the standard report analyses.
   Missing optional grade or term segments can be skipped.
5. Each tool returns a `ToolOutput`. `Ledger.record` stores the results and
   gives the facts citation IDs. The report's initial results and facts become
   the source evidence used to identify this run.
6. `Analyst.run` lets the model request more tools, within configured round and
   call limits. It then asks for a structured answer. The model receives fact
   sentences, not grouped rows, SQL or connection settings.
7. `validate_answer` checks references, numeric magnitudes and units, and
   prohibited decision wording. There is at most one repair attempt. Remaining
   invalid statements are withheld; an invalid main answer fails the run.
   These checks do not establish that an interpretation is correct.
8. `render_report` builds the Markdown. The pipeline writes `report.md`,
   `aggregates.json`, `snapshot.json` and `ollama_analysis.json` only after the
   analysis succeeds. A model failure leaves prior report files untouched; a
   newly saved snapshot remains available for a rerun.

For `ask`, `pipeline.run_ask` starts with the same snapshot setup but initially
runs only `describe_data`. The model chooses the analyses needed for the
question. The validated answer is printed and saved as Markdown and JSON.
`chat` reuses one snapshot and ledger across questions; `ChatSession` carries
recent successful answers and their cited facts into the next turn.

## Understand a rate before changing it

Accepted-loan metrics use different populations:

| Metric | Numerator | Denominator |
| --- | --- | --- |
| Terminal share | Fully Paid + Charged Off loans | All retained accepted loans |
| Charge-off rate | Charged Off loans | Fully Paid + Charged Off loans |
| Principal-loss proxy | Unrecovered principal on financially complete charged-off loans | Funded principal of all financially complete terminal loans |
| Financial coverage | Financially complete terminal loans | All terminal loans |

`performance` returns `None` when a rate is unavailable, including when its
minimum outcome count is not met. `None` must not become zero. An open loan
counts toward retained volume but not terminal loss outcomes. Missing financial
data must not contribute an invented zero loss.

To see a small worked case, open `tests/fixtures.py` and the
`test_cumulative_cutoffs_and_weighted_loss_denominator` test in
`tests/test_analyses.py`. For its cohort and cutoff of 600, five loans are
retained, four have complete terminal data, and the loss proxy is `80 / 400`,
or 20%. The retained principal is 500, which is deliberately a different
denominator. These fixtures are test inputs only.

Rejected applications have no repayment outcomes. Their numeric score screens
stay separate from accepted-loan performance; they are not additional observed
loans or losses.

## Find the right place for a change

| Change | Start here | Verify with |
| --- | --- | --- |
| A default or validation rule | `settings.py`, then the CLI/config entry points | `tests.test_settings`, `tests.test_pipeline` |
| A formula or cutoff rule | The relevant module under `analyses/` | `tests.test_analyses` |
| Report headings or formatting | `report.py`, or a table renderer in `analyses/` | `tests.test_pipeline` |
| Model instructions | `prompts.py`; bump `VERSION` when editing prompts | `tests.test_agent`; live model checks separately when needed |
| Evidence validation | `evidence.py` | `tests.test_evidence`, `tests.test_agent` |
| SQL or snapshot structure | `cube.py`; connection behavior is in `db.py` | `tests.test_cube` |
| A new analysis tool | A module under `analyses/` and `analyses.MODULES` | `tests.test_tools`, analysis-specific tests |

For a new analysis, follow an existing module: write a calculation function,
turn its results into citable facts, and register a `Tool` with a schema and
handler returning `ToolOutput`. Add a standard call to `pipeline.REPORT_CALLS`
and a renderer to `report.SECTIONS` if it belongs in every report. Every number
the model may quote must appear in a fact. Tool arguments cannot include SQL,
paths, credentials, connection settings or expansion assumptions.

Keep calculation changes separate from model wording changes when possible.
A calculation can be verified directly without waiting for the model.

## Work locally without services

Run these from the repository root in PowerShell using the existing virtual
environment:

```powershell
# Read the commands and inspect the SQL/tool catalog without connecting:
.\.venv\Scripts\python.exe -m loan_analyst --help
.\.venv\Scripts\python.exe -m loan_analyst sql
.\.venv\Scripts\python.exe -m loan_analyst tools

# Exercise the calculations and full workflows with test fixtures:
.\.venv\Scripts\python.exe -m unittest tests.test_analyses -v
.\.venv\Scripts\python.exe -m unittest tests.test_pipeline -v

# Run the complete offline regression suite:
.\.venv\Scripts\python.exe -m unittest discover -v
```

`tests/fixtures.py` contains the reusable test helpers. `FakeConnection` records
queries and transaction operations. `ScriptedModel` returns queued responses
and records requests. `services(...)` puts those fakes into a `Services`
instance and captures terminal output. Tests create temporary folders under
`outputs/` and clean them up. This lets a workflow test check both its result
and boundaries such as closing the connection before the analyst investigates.

`call`, `snapshot` and `report --no-ai` are not automatically offline commands:
they can read MySQL. Use `--snapshot` explicitly when reusing an existing saved
read. `ask` and `chat` still need the local model when using a saved snapshot.
Passing the offline suite verifies code against fixtures; it does not verify
current service availability or import completeness.

The separate `import_csv_to_mysql.py` utility writes source data to MySQL and
is outside the reporting path. Do not run an import to test a report change.
Treat the source files under `data/` as read-only: that folder is a junction to
the original dataset.
