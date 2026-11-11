# Reading and changing the loan analyst

This program answers questions about historical Lending Club loans. Python
calculates every number; a local language model chooses extra analyses and
writes commentary that cites those numbers. The model never sees borrower rows,
SQL, credentials or connection settings.

You can read, run and test the code without MySQL or Ollama. The tests use a
small fake snapshot and a scripted model.

The code is arranged as a few larger, cohesive files rather than many tiny
modules. That is deliberate: one idea lives in one place, and you rarely have
to follow a call through five files. Start with the six-step workflow below.

## The whole report in six steps

Open [`loan_analyst/workflow.py`](../loan_analyst/workflow.py) and read
`run_report` from top to bottom. It is the application:

```text
1. check the local model              (before an expensive database read)
2. get a Snapshot                     (read MySQL once, or load a saved file)
3. run the standard analyses          (REPORT_CALLS, through the tool registry)
4. freeze the evidence                (results + facts + content hashes)
5. ask the analyst for commentary     (optional; --no-ai skips this)
6. render and write five files        (report.md, three JSON files, trace.jsonl)
```

Each step is a comment in that function, and each step uses one clear piece of
the package:

| Step | Main code | What it does |
| --- | --- | --- |
| 1 | `workflow.run_report` | `OllamaClient.preflight()` checks the installed local model. |
| 2 | `workflow.obtain_snapshot` -> `snapshot.read_snapshot` | Runs one grouped SELECT per table inside one read-only transaction, then saves the result. |
| 3 | `calculations.build_registry` + `workflow.run_calls` | Runs the standard tool calls in `REPORT_CALLS`. An absent optional column is skipped. |
| 4 | `workflow.build_report_evidence` | Copies settings, query text, results, facts and hashes into the evidence dict. Model calls are added later. |
| 5 | `analyst.Analyst.run_report` | The model plans tool calls, writes each topic in `REPORT_TOPICS` separately, has the statements reviewed, and an editor step assembles the answer. Every reply is journaled by `runtime.Run`. |
| 6 | `report.render_report` + `support.write_files` | Builds Markdown and stages all five files before replacing the old ones. |

## Read these files in this order

| Order | File | Read this first |
| --- | --- | --- |
| 1 | `loan_analyst/workflow.py` | `run_report`, then `run_ask` and `run_chat`. |
| 2 | `loan_analyst/settings.py` | `Scenario` (the user's choices) and `DatabaseConfig`. The validation is a plain top-to-bottom `__init__`. |
| 3 | `loan_analyst/snapshot.py` | `Cube.bands` (cohort + score bands) and `Snapshot` (the saved read). Skim `build_query`; the SQL is the hardest part. |
| 4 | `loan_analyst/calculations.py` | One section at a time: shared rate rules, overview, cutoffs, segments, rejected applications. Each analysis has an `analyze_*` and an `*_facts`; the tool schemas and handlers are grouped at the end. |
| 5 | `loan_analyst/report.py` | The tables and headings a reader sees. Each report section is a short function. |
| 6 | `loan_analyst/analyst.py` | `Ledger`, `check_statement`, `validate_answer`, then `Analyst.run` (one question) and `Analyst.run_report` (topics). |
| 7 | `loan_analyst/runtime.py` | `Run.chat`: replay from the journal, one retry, the call budget and the trace. Short, and independent of loans. |
| 8 | `tests/test_workflow.py` | Complete workflows with a fake connection and a scripted model. |

You do not need to understand the SQL builder before changing a report heading,
and you do not need to understand evidence validation before changing a number.
Keep those jobs separate.

## Follow one number

The test fixture in `tests/fixtures.py` is a tiny snapshot: cohort starts
2013-01, cutoffs are 600 and 700, and the baseline is 700. For cutoff 600:

1. `Cube.bands` groups the rows into disjoint score bands. The `600` band holds
   3 loans (score 640) and the `700` band holds 2 loans (score 712); there is
   also a below-600 band and an invalid-score band.
2. `compare_cutoffs` keeps every in-cohort band whose bucket is at least 600.
   That is 3 + 2 = **5 retained loans** and $500 funded principal.
3. `performance` uses only terminal loans: 4 are Fully Paid or Charged Off,
   1 is Charged Off, so the charge-off rate is 1/4 = **25%**. Four loans have
   complete financial fields, and they lost $80 on $400 of principal, so the
   loss proxy is **20%**.
4. `cutoff_facts` writes the sentence `accepted_600`, including `N/A` if a rate
   was unavailable. The model may quote only that sentence.
5. `report.cutoff_table` formats the same row for the Markdown table.

Try the test `test_cumulative_cutoffs_and_weighted_loss_denominator` in
`tests/test_calculations.py`; it checks exactly these numbers.

Two rules matter more than any formula:

* **A missing value is `None`, never zero.** A rate without enough outcomes is
  `N/A`. An open loan is not a zero-loss loan.
* **Different rates use different populations.** Reads counts include open
  loans; charge-off and loss rates use terminal loans only.

## Follow one model answer

The model is never asked to do everything at once. `analyst.py` gives it one
small role at a time, and each role is one method on `Analyst`.

1. `calculations.build_registry()` collects the tools from the bottom of
   `calculations.py`. Each tool is a name, a description, a JSON schema and a
   handler.
2. **Planner** (`Analyst.plan`). The model sees a short list of fact sentences
   and decides which tools to run. In `native` tool mode (`investigate`) it
   replies with tool calls or `DONE`. In `schema` tool mode (`plan_with_schema`)
   it replies with a JSON list of calls whose shape is fixed by
   `ToolRegistry.action_schema`; an empty list means done.
3. `ToolRegistry.call` looks up the name, validates the arguments against the
   schema, runs the handler over the in-memory `Snapshot`, and returns a
   `ToolOutput`. This is the safety boundary in both tool modes.
4. `Ledger.record` saves the result and gives every fact an ID such as
   `t1.accepted_600`. Repeating the same call reuses the first ID.
5. **Writer** (`Analyst.write`). The model writes JSON statements that cite fact
   IDs. `validate_answer` checks the structure, the IDs and every quoted number.
   For a question there is one writer. For a report there is one writer per
   `Topic` in `workflow.REPORT_TOPICS`, and each sees only its own facts
   (`Analyst.topic_fact_ids`).
6. **Critic** (`Analyst.critique`, when enabled). The model sees each statement
   beside only the facts it cites and returns `supported`, `not_supported` or
   `overreach`. Statements that merely repeat whole fact sentences are skipped
   (`is_verbatim`). A missing verdict counts against the statement.
7. Problems from steps 5 and 6 go back to the writer for **one** repair. What
   still fails is withheld and listed in the artifact.
8. **Editor** (`Analyst.edit`, reports only). The model selects validated
   statements by ID and writes only the headline and next steps. Selecting by ID
   cannot introduce a new number.
9. `report.render_report` prints the validated statements with numbered
   citations, and lists the original fact sentences under "Evidence cited".

Python's checks prove that a number appears in a cited fact. The critic is the
same kind of model as the writer. Neither proves that an interpretation is
right. That is why model text is labelled for review.

### What happens when a run stops halfway

Every model call goes through `runtime.Run.chat`. After each reply it appends
one line to `outputs/runs/<run id>/journal.jsonl`. `report --resume` repeats the
same steps from the top: tool calls are simply computed again, and a model call
whose inputs match the next journal line is answered from the file. The first
call that differs ends the replay. Nothing in the steps needs to know it is
being resumed, which is why there is no "resume" code in `analyst.py`.

## The objects worth knowing

| Name | Where | What it means |
| --- | --- | --- |
| `DatabaseConfig` | `settings.py` | Host, port, user and the two table names. Never contains a password. |
| `Scenario` | `settings.py` | Cohort months, cutoffs, baseline, loss ceiling, minimum outcomes and optional expansion assumptions. Treat it as read-only; use `with_overrides()` to change one value. |
| `ModelConfig` | `settings.py` | Loopback Ollama URL, model tag, timeouts and tool limits. |
| `RunOptions` | `workflow.py` | One command's validated database + scenario + model + output folder. |
| `Services` | `workflow.py` | Replaceable outside world: connection factory, model factory, clock, terminal input/output. Tests put fakes here. |
| `Snapshot` | `snapshot.py` | Both grouped cubes, the SQL, read times and a content hash. A saved file is verified against that hash and the table names. |
| `Cube` | `snapshot.py` | One source's grouped rows. `bands()` splits them by cohort status and score band. |
| `ToolOutput` | `tools.py` | One calculation: a title, a structured `result`, citable `facts` and optional `highlights`. |
| `Ledger` | `analyst.py` | Facts by ID plus the tool calls that produced them. |
| `Topic` | `analyst.py` (listed in `workflow.REPORT_TOPICS`) | One part of the report: its goal, the standard calls it covers, the tools the planner may add for it and its definitions. |
| `Run` | `runtime.py` | One run's access to the model: journal replay, one retry of a transient failure, the model-call budget and tracing. |
| `Journal`, `Tracer` | `runtime.py` | The saved model replies of an unfinished run, and the timing spans written to `trace.jsonl`. |
| Analysis artifact | `analyst.py` | The dict saved as `ollama_analysis.json`: requests, tool calls, validated answer, withheld statements, model digest and usage. |

Most data between these layers is a plain dictionary or list. When you need a
result's exact fields, look at the `analyze_*` function that returns it.

## What each output file is

| File | Contents |
| --- | --- |
| `report.md` | The human report: calculated tables, model commentary, evidence list, definitions. |
| `aggregates.json` | Settings, snapshot provenance, every standard result and fact, run ID and source hash. |
| `snapshot.json` | The grouped rows and SQL, reusable with `--snapshot <file>`. |
| `ollama_analysis.json` | Exact model requests, tool definitions/calls, drafted statements, critic verdicts, the validated answer, withheld statements and usage (or a disabled status). |
| `trace.jsonl` | One timing span per step with token counts. Metadata only; print it with `python -m loan_analyst trace DIR`. |

`aggregates.json` and `ollama_analysis.json` share one run ID and source hash.
`render DIR` rebuilds `report.md` from them, so you can review wording without
touching MySQL or the model.

## Where to make a change

| Change | Start here | Test with |
| --- | --- | --- |
| A default or validation rule | `settings.py` | `tests.test_settings`, `tests.test_workflow` |
| A formula or cutoff rule | the matching `analyze_*` in `calculations.py` | `tests.test_calculations` |
| A sentence the model may cite | the matching `*_facts` in `calculations.py` | `tests.test_calculations` |
| A report heading or table column | `report.py` | `tests.test_workflow` |
| Model instructions | `prompts.py` (bump `VERSION`) | `tests.test_analyst`; live model check separately |
| Evidence or wording checks | `check_statement` / `validate_answer` in `analyst.py` | `tests.test_answer_checks`, `tests.test_analyst` |
| SQL or snapshot structure | `snapshot.py`; connection handling in `database.py` | `tests.test_snapshot` |
| A new tool | add the handler, schema and `Tool(...)` at the bottom of `calculations.py` | `tests.test_tools` |
| What a report topic covers | `REPORT_TOPICS` in `workflow.py` | `tests.test_analyst` |
| How a role works (planner, writer, critic, editor) | the matching method on `Analyst` in `analyst.py` | `tests.test_analyst` |
| Resume, retry, budget or trace | `runtime.py` | `tests.test_runtime` |
| An evaluation case or metric | `evals/cases.toml`; scoring in `evals.py` | `tests.test_evals`; live `eval` run separately |
| What other programs can call | `mcp_server.py` (it serves the same registry) | `tests.test_mcp_server` |

For a new tool, follow an existing one exactly: write the calculation, turn every
number the model may quote into a fact sentence (including any difference or
ratio, because the model may not calculate), register a `Tool`, and (if it
belongs in every report) add a call to `workflow.REPORT_CALLS`, list it in the
`Topic` that should write about it, and add a renderer to `report.SECTIONS`.

After changing a prompt, a role or the default model, run the evaluation suite
against a saved snapshot and compare with an earlier result:

```powershell
.\.venv\Scripts\python.exe -m loan_analyst eval --snapshot latest --compare-with outputs/evals/<earlier run>/results.json
```

## Run it without services

From the repository root in PowerShell:

```powershell
# Help and static information; no connections:
.\.venv\Scripts\python.exe -m loan_analyst --help
.\.venv\Scripts\python.exe -m loan_analyst sql
.\.venv\Scripts\python.exe -m loan_analyst tools

# The whole offline regression suite (fakes only, no MySQL or Ollama):
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

`tests/fixtures.py` holds the reusable helpers: `FakeConnection` records queries
and transaction calls, `ScriptedModel` replays queued replies (and can answer
everything after them with `auto`, a valid reply built from the request's
schema), and `services(...)` wires both into `Services` and captures terminal
output. `planned(...)`, `verdicts(...)` and `broken()` script a plan, a critic
review and an invalid reply. Tests create temporary
folders under `outputs/` and remove them afterwards.

`call`, `snapshot` and `report --no-ai` can read MySQL; use `--snapshot` when you
only want to reuse a saved read. `ask` and `chat` still need the local model.

## A short glossary

| Term | Meaning |
| --- | --- |
| Cohort | The selected range of loan issue months (or rejected application months). |
| Cutoff | A minimum score. "Retains" means historical loans at or above it. |
| Score band | A disjoint range: below the first cutoff, between adjacent cutoffs, or at/above the last. |
| Terminal loan | Exactly `Fully Paid` or `Charged Off`. Open, late and unknown statuses are excluded from rates. |
| Terminal share | Terminal loans / all retained loans. |
| Charge-off rate | Charged Off / terminal loans. |
| Principal-loss proxy | Unrecovered principal on complete Charged Off loans / funded principal of all complete terminal loans. |
| Financial coverage | Complete terminal loans / all terminal loans. Completeness, not credit quality. |
| Evidence ID | A fact reference such as `def.loss_proxy` or `t1.accepted_600`. |
| Expansion assumptions | Optional user-supplied hypothetical lending on rejected applications. Never measured outcomes. |

## Why the files are grouped this way

An earlier version split the same work across 23 small modules. Making one
change meant jumping between `pipeline.py`, four modules under `analyses/`,
`common.py`, `formatting.py`, `jsonutil.py` and `artifacts.py`. The code was
correct but hard to read.

The current layout keeps one subject per file:

* calculations and their facts live together in `calculations.py`;
* all human-readable output lives in `report.py`;
* everything about the model lives in `analyst.py` + `prompts.py` + `ollama.py`;
* run plumbing that knows nothing about loans (journal, trace, budget) lives in
  `runtime.py`;
* the workflow is a numbered recipe in `workflow.py`;
* `evals.py` and `mcp_server.py` are separate entry points over the same tools.

The files are longer, but each is a straight read from top to bottom. When a
file gets too long to scan, use the section banners (`# ====`) and read only the
section you need.

Project constraints, live-validation history and the importer are documented in
[`AGENTS.md`](../AGENTS.md). Operating commands are in [`README.md`](../README.md).
