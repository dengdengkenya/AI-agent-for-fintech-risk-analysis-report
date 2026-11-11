"""Offline test doubles: a small grouped cube, a fake MySQL connection and a scripted model.

These fixtures exist only for tests; reporting never reads them.
"""

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile

from loan_analyst import PROJECT_ROOT
from loan_analyst.snapshot import ACCEPTED, REJECTED, REQUIRED, Cube, Snapshot
from loan_analyst.workflow import Services
from loan_analyst.settings import ModelConfig, Scenario

OPTIONAL = {ACCEPTED: {"grade", "term"}, REJECTED: set()}


def accepted(period, score, rows, amount_rows, amount, terminal=0, bad=0, complete=0, principal=0, loss=0,
             grade=None, term=None):
    return {"period": period, "score": score, "grade": grade, "term": term, "rows": rows,
            "amount_rows": amount_rows, "amount_sum": amount, "terminal_rows": terminal, "charged_off_rows": bad,
            "performance_rows": complete, "performance_principal": principal, "loss_amount": loss}


def cube_rows():
    """With cohort 2013-01 onward and cutoffs 600/700 this reproduces the reviewed band fixture."""
    return {
        ACCEPTED: [
            accepted("2015-03", 640.0, 3, 3, 300, terminal=2, bad=1, complete=2, principal=200, loss=80,
                     grade="C", term="36"),
            accepted("2015-06", 712.0, 2, 2, 200, terminal=2, complete=2, principal=200, grade="A", term="36"),
            accepted("2014-02", 580.0, 5, 5, 500, grade="E", term="60"),
            accepted("2016-01", None, 11, 10, 1000, term="36"),
            accepted(None, 705.0, 7, 7, 700, grade="B", term="36"),
            accepted("2012-06", 720.0, 6, 6, 600, grade="B", term="60"),
        ],
        REJECTED: [
            {"period": "2013-05", "score": 650.0, "rows": 4, "amount_rows": 3, "amount_sum": 600},
            {"period": "2015-01", "score": 702.0, "rows": 1, "amount_rows": 1, "amount_sum": 400},
        ],
    }


def scenario(**changes):
    values = {"cutoffs": (600, 700), "baseline": 700, "min_outcomes": 1, "start_month": "2013-01"}
    return Scenario(**(values | changes))


def make_snapshot(rows=None, dimensions=("grade", "term")):
    rows = deepcopy(rows or cube_rows())
    for row in rows[ACCEPTED]:
        for name in OPTIONAL[ACCEPTED] - set(dimensions):
            row.pop(name, None)
    sources = {ACCEPTED: {"table": "accepted", "engine": "InnoDB", "dimensions": list(dimensions)},
               REJECTED: {"table": "rejected", "engine": "InnoDB", "dimensions": []}}
    cubes = {kind: Cube(kind, sources[kind]["dimensions"], rows[kind]) for kind in rows}
    return Snapshot("demo", sources, {ACCEPTED: "-- accepted SQL", REJECTED: "-- rejected SQL"}, cubes,
                    "2026-11-03T10:00:00+00:00", "2026-11-03T10:04:00+00:00")


class FakeCursor:
    def __init__(self, connection):
        self.connection, self.closed, self.current = connection, False, []

    def execute(self, query, params=()):
        connection = self.connection
        connection.executed.append((query, params))
        if "information_schema.TABLES" in query:
            self.current = [{"ENGINE": connection.engine}]
        elif "information_schema.COLUMNS" in query:
            kind = connection.tables[params[1]]
            columns = (REQUIRED[kind] | OPTIONAL[kind]) - connection.missing.get(kind, set())
            self.current = [{"COLUMN_NAME": name} for name in sorted(columns)]
        elif "GROUP BY period, score" in query:
            if connection.failure is not None:
                raise connection.failure
            kind = next(kind for table, kind in connection.tables.items() if f"`{table}`" in query)
            dimensions = {name for name in OPTIONAL[kind] if f"`{name}`" in query.split("GROUP BY")[1]}
            # Return Decimal scores and sums, as mysql-connector does.
            self.current = [{key: Decimal(str(value)) if (key == "score" or key.endswith(
                                 ("rows", "sum", "principal", "amount"))) and value is not None else value
                             for key, value in row.items()
                             if key not in OPTIONAL[kind] or key in dimensions} for row in connection.rows[kind]]
        else:
            raise AssertionError(f"Unexpected query: {query}")

    def fetchone(self):
        return self.current[0] if self.current else None

    def fetchall(self):
        return self.current

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, *, missing=None, failure=None, engine="InnoDB", tables=None, rows=None):
        self.rows = rows or cube_rows()
        self.missing, self.failure, self.engine = missing or {}, failure, engine
        self.tables = tables or {"accepted": ACCEPTED, "rejected": REJECTED}
        self.executed, self.transactions, self.cursors = [], [], []
        self.rollbacks, self.closed = 0, False

    def cursor(self, dictionary=False):
        if not dictionary:
            raise AssertionError("Expected a dictionary cursor")
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def start_transaction(self, **settings):
        self.transactions.append(settings)

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


METADATA = {"model": ModelConfig.model, "model_digest": "test-digest", "server_version": "test",
            "endpoint": "http://localhost:11434", "capabilities": ["completion", "tools"]}


class ScriptedModel:
    """Replays replies in order (dicts, or callables given the request) and records requests."""

    def __init__(self, replies, config=None, default=None):
        self.replies, self.requests, self.metadata, self.config = list(replies), [], None, config
        self.default = default  # answers every request after the queued replies run out

    def preflight(self, require_tools=True):
        self.metadata = dict(METADATA)
        self.required_tools = require_tools
        return self.metadata

    def chat(self, messages, *, tools=None, format=None, num_predict=1024):
        payload = {"model": ModelConfig.model, "messages": deepcopy(messages), "stream": False, "think": False,
                   "options": {"temperature": 0, "num_predict": num_predict}}
        if tools:
            payload["tools"] = tools
        if format is not None:
            payload["format"] = format
        self.requests.append(payload)
        reply = self.replies.pop(0) if self.replies or self.default is None else self.default
        reply = reply(payload) if callable(reply) else reply
        return {"request": payload, "content": reply.get("content", ""), "tool_calls": reply.get("tool_calls", []),
                "usage": {"prompt_eval_count": 100, "eval_count": 20}}


def calls(*pairs):
    """A model reply requesting tools: calls(("name", {...}), ...)."""
    return {"tool_calls": [{"name": name, "arguments": arguments} for name, arguments in pairs]}


def allowed_ids(payload):
    return payload["format"]["properties"]["answer"]["items"]["properties"]["evidence_ids"]["items"]["enum"]


def statement(text, *ids):
    return {"text": text, "evidence_ids": list(ids)}


def grounded(payload, **sections):
    """A valid answer using only definition facts, unless sections are supplied."""
    answer = {"answer": [statement("Higher cutoffs retain fewer historical loans in this replay.", "def.retained")],
              "findings": [statement("The loss proxy uses only financially complete terminal loans.",
                                     "def.loss_proxy"),
                           statement("Candidates need an explicit ceiling and complete data.", "def.candidate_rule")],
              "caveats": [statement("Rejected scores are not comparable to accepted FICO.", "def.rejected_scores")],
              "next_steps": [statement("Agree a loss ceiling before selecting a cutoff.", "def.next_checks")]}
    answer.update(sections)
    properties = payload["format"]["properties"]
    return {"content": json.dumps({key: value[:properties[key]["maxItems"]] for key, value in answer.items()
                                   if key in properties})}


def done():
    return {"content": "DONE"}


def broken(payload=None):
    """A reply that is never valid JSON."""
    return {"content": "{"}


def planned(*items):
    """A schema-mode plan: planned(("tool", {...}), ("topic", "tool", {...}), ...)."""
    requested = []
    for item in items:
        if len(item) == 3:
            requested.append({"topic": item[0], "tool": item[1], "arguments": item[2]})
        else:
            requested.append({"tool": item[0], "arguments": item[1]})
    return {"content": json.dumps({"calls": requested})}


def statement_ids(payload):
    """The statement IDs a critic request asks about."""
    return payload["format"]["properties"]["verdicts"]["items"]["properties"]["statement"]["enum"]


def verdicts(payload, **overrides):
    """A critic reply: every statement supported unless overridden, e.g. s1="overreach"."""
    return {"content": json.dumps({"verdicts": [
        {"statement": statement_id, "verdict": overrides.get(statement_id, "supported"), "reason": "checked"}
        for statement_id in statement_ids(payload)]})}


def auto(payload):
    """A sensible valid reply to any request, decided from its structured-output schema.

    No schema: finish investigating. A plan: no calls. A review: all supported.
    Anything else: one qualitative statement per written section citing the first
    allowed fact, and the first allowed statement IDs for every selection.
    """
    properties = payload.get("format", {}).get("properties")
    if properties is None:
        return done()
    if "calls" in properties:
        return planned()
    if "verdicts" in properties:
        return verdicts(payload)

    answer = {}
    for name, section in properties.items():
        items = section["items"]
        if "properties" in items:
            first_fact = items["properties"]["evidence_ids"]["items"]["enum"][0]
            written = [statement(f"A qualitative {name} statement.", first_fact)]
            answer[name] = written[:section["maxItems"]]
        else:
            answer[name] = items["enum"][:section["maxItems"]]
    return {"content": json.dumps(answer)}


def temporary_directory(test, prefix):
    """A temporary folder under outputs/, removed after the test."""
    root = PROJECT_ROOT / "outputs"
    root.mkdir(exist_ok=True)
    folder = tempfile.TemporaryDirectory(prefix=prefix, dir=root)
    test.addCleanup(folder.cleanup)
    return Path(folder.name)


def services(test, *, connection=None, model=None, lines=()):
    """Services wired to fakes; collects log and output text on the returned object."""
    log, out, inputs = [], [], list(lines)

    def read_line(prompt):
        if not inputs:
            raise EOFError
        return inputs.pop(0)

    def connect(config):
        if connection is None:
            raise AssertionError("No database connection expected")
        test.connected_with = config
        return connection

    def load_model(config):
        test.model_config = config
        return model

    wired = Services(connection_factory=connect, model_factory=load_model,
                     snapshot_dir=temporary_directory(test, "snapshots-test-"),
                     runs_dir=temporary_directory(test, "runs-test-"), default_config=None,
                     log=log.append, out=out.append, read_line=read_line,
                     clock=lambda: "2026-11-03T10:30:00+00:00")
    wired.logged, wired.printed = log, out
    return wired
