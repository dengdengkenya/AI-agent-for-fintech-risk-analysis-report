"""Durable plumbing for one agent run: a model-reply journal, a trace and a call budget.

Nothing in this file knows about loans, prompts or evidence. It answers three
practical questions about a run that makes several slow local model calls:

* Journal - if the run stops halfway, which model replies are already paid for?
  Every completed reply is appended to ``journal.jsonl``. A resumed run repeats
  the same steps; a call whose inputs match the next journal entry is answered
  from the file instead of the model.
* Tracer  - where did the time and tokens go? Each step is recorded as a span.
  The trace is a local file only; nothing is exported.
* Run     - ties a model client to both, retries one transient failure and
  enforces the model-call budget.

Tool calls are not journaled: they are fast, deterministic Python and are simply
computed again on resume.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
from time import perf_counter

from .support import read_json, sha256_json, utc_now

JOURNAL_FORMAT = "loan-analyst-journal/1"
JOURNAL_NAME = "journal.jsonl"
TRACE_FORMAT = "loan-analyst-trace/1"


class BudgetExceeded(RuntimeError):
    """The run needed more model calls than its configured limit; safe to show."""


class ResumeError(ValueError):
    """A saved run cannot be resumed with the current inputs; safe to show."""


# ===========================================================================
# Journal
# ===========================================================================

def _line(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"


class Journal:
    """Completed model replies for one run, in the order they were made."""

    def __init__(self, path, header, entries=()):
        self.path = Path(path)
        self.header = header
        self._pending = list(entries)   # saved replies not yet matched by this process
        self._kept = []                 # saved replies already replayed, in order

    @property
    def run_id(self):
        return self.header["run_id"]

    @classmethod
    def create(cls, directory, run_id, kind, identity, **details):
        """Start a new journal at <directory>/<run_id>/journal.jsonl."""
        path = Path(directory) / run_id / JOURNAL_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        header = {"format": JOURNAL_FORMAT, "run_id": run_id, "kind": kind,
                  "identity": identity, **details}
        journal = cls(path, header)
        journal._rewrite()
        return journal

    @classmethod
    def load(cls, path):
        """Read a journal. A torn final line (a crash during a write) is ignored."""
        path = Path(path)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            raise ResumeError("No interrupted run was found to resume") from None

        records = []
        for line in lines:
            try:
                record = read_json(line)
            except ValueError:
                break
            if not isinstance(record, dict):
                break
            records.append(record)

        if not records or records[0].get("format") != JOURNAL_FORMAT:
            raise ResumeError("The saved run journal is unreadable; start the run again without --resume")
        entries = [record for record in records[1:] if "key" in record and "reply" in record]
        return cls(path, records[0], entries)

    @classmethod
    def find(cls, directory, reference="latest"):
        """Load a run by ID, or the most recently written interrupted run."""
        directory = Path(directory)
        if reference != "latest":
            # A run ID is a folder name; refuse anything that could leave the runs folder.
            if Path(reference).name != reference or reference in (".", ".."):
                raise ResumeError("Run ID must be a folder name under the runs directory")
            return cls.load(directory / reference / JOURNAL_NAME)

        paths = sorted(directory.glob(f"*/{JOURNAL_NAME}"), key=lambda path: path.stat().st_mtime)
        if not paths:
            raise ResumeError("No interrupted run was found to resume")
        return cls.load(paths[-1])

    def replay(self, key):
        """Return the saved reply when ``key`` matches the next saved call, else None.

        Replies are matched strictly in order. The first call that differs means
        the run has diverged from the saved one, so every later saved reply is
        dropped and the model is asked again from that point.
        """
        if not self._pending:
            return None
        if self._pending[0]["key"] == key:
            entry = self._pending.pop(0)
            self._kept.append(entry)
            return entry["reply"]

        self._pending = []
        self._rewrite()
        return None

    def append(self, stage, key, reply):
        entry = {"stage": stage, "key": key, "reply": reply}
        self._kept.append(entry)
        with open(self.path, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(_line(entry))
            stream.flush()
            os.fsync(stream.fileno())

    def _rewrite(self):
        """Write the header and the replies still valid for this run."""
        temporary = self.path.with_suffix(".tmp")
        with open(temporary, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(_line(self.header))
            for entry in self._kept:
                stream.write(_line(entry))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)


def identity_differences(saved, current):
    """Names of the identity fields that differ between a saved run and this one."""
    return sorted(key for key in saved.keys() | current.keys() if saved.get(key) != current.get(key))


# ===========================================================================
# Trace
# ===========================================================================

class Tracer:
    """Nested timing spans for one run. Attribute names follow the OpenTelemetry
    GenAI conventions (gen_ai.*) so the file can be converted later; this program
    never exports it. Spans hold metadata only, never prompts or model text.
    """

    def __init__(self, clock=utc_now):
        self.clock = clock
        self.spans = []
        self._stack = []

    @contextmanager
    def span(self, name, **attributes):
        """Time one step. The yielded dict accepts further attributes."""
        span = {
            "span_id": f"s{len(self.spans) + 1}",
            "parent_id": self._stack[-1] if self._stack else None,
            "name": name,
            "start_utc": self.clock(),
            "duration_ms": None,
            "status": "ok",
            "attributes": dict(attributes),
        }
        self.spans.append(span)
        self._stack.append(span["span_id"])
        started = perf_counter()
        try:
            yield span["attributes"]
        except BaseException as error:
            span["status"] = "error"
            span["error_type"] = type(error).__name__  # the type only; messages can quote data
            raise
        finally:
            span["duration_ms"] = round((perf_counter() - started) * 1000, 1)
            self._stack.pop()

    def to_jsonl(self, trace_id):
        """One JSON object per line: a header, then every span in start order."""
        lines = [_line({"format": TRACE_FORMAT, "trace_id": trace_id})]
        lines += [_line({"trace_id": trace_id, **span}) for span in self.spans]
        return "".join(lines)


def read_trace(text):
    """Parse a trace file into (trace_id, spans)."""
    records = [read_json(line) for line in text.splitlines() if line.strip()]
    if not records or records[0].get("format") != TRACE_FORMAT:
        raise ValueError("Not a loan analyst trace file")
    return records[0].get("trace_id"), records[1:]


def format_trace(trace_id, spans):
    """A readable timeline: indentation shows nesting, with seconds and tokens."""
    depth = {}
    lines = [f"Trace {trace_id}"]
    for span in spans:
        level = depth.get(span.get("parent_id"), -1) + 1
        depth[span["span_id"]] = level
        attributes = span.get("attributes", {})

        notes = []
        tokens_in = attributes.get("gen_ai.usage.input_tokens")
        tokens_out = attributes.get("gen_ai.usage.output_tokens")
        if tokens_in is not None or tokens_out is not None:
            notes.append(f"{tokens_in or 0} in / {tokens_out or 0} out tokens")
        if attributes.get("replayed"):
            notes.append("replayed from journal")
        if attributes.get("retried"):
            notes.append("retried once")
        if span.get("status") == "error":
            notes.append(f"ERROR {span.get('error_type', '')}".strip())

        seconds = (span.get("duration_ms") or 0) / 1000
        suffix = f"  ({'; '.join(notes)})" if notes else ""
        lines.append(f"{seconds:9.2f}s  {'  ' * level}{span['name']}{suffix}")
    return "\n".join(lines)


# ===========================================================================
# Run
# ===========================================================================

class Run:
    """One agent run's access to the model: replay, retry, budget and tracing."""

    def __init__(self, client, *, journal=None, tracer=None, max_model_calls=None):
        self.client = client
        self.journal = journal
        self.tracer = tracer or Tracer()
        self.max_model_calls = max_model_calls
        self.model_calls = 0   # real calls since the budget was last reset
        self.replayed = 0      # replies answered from the journal

    def reset_budget(self):
        """Start counting again, e.g. for the next question in a chat session."""
        self.model_calls = 0

    def chat(self, stage, messages, **options):
        """One model turn for ``stage``. Returns (reply, replayed)."""
        key = sha256_json({"messages": messages, "options": options})
        metadata = self.client.metadata or {}

        with self.tracer.span(f"chat {stage}", **{
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": metadata.get("model"),
                "stage": stage}) as attributes:
            reply = self.journal.replay(key) if self.journal else None
            replayed = reply is not None

            if replayed:
                self.replayed += 1
                attributes["replayed"] = True
            else:
                if self.max_model_calls is not None and self.model_calls >= self.max_model_calls:
                    raise BudgetExceeded(
                        f"The analyst reached its limit of {self.max_model_calls} model calls; "
                        "raise --max-model-calls or reduce tool rounds")
                self.model_calls += 1
                try:
                    reply = self.client.chat(messages, **options)
                except Exception as error:
                    # Only failures the client marks transient (timeouts, a busy
                    # server) are worth one more attempt with identical input.
                    if not getattr(error, "transient", False):
                        raise
                    attributes["retried"] = True
                    reply = self.client.chat(messages, **options)
                if self.journal:
                    self.journal.append(stage, key, reply)

            usage = reply.get("usage") or {}
            attributes["gen_ai.usage.input_tokens"] = usage.get("prompt_eval_count")
            attributes["gen_ai.usage.output_tokens"] = usage.get("eval_count")
        return reply, replayed
