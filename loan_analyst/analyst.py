"""The local analyst: citable facts, answer validation, and the model's four roles.

This is the only part of the program where model text is involved. The model
is given one small job at a time:

1. Planner - chooses allowlisted tools to run. Each successful call adds its
   fact sentences to the Ledger and gives them IDs such as ``t1.scope``.
2. Writer  - writes JSON statements that cite those IDs. Python checks every
   statement. One repair attempt is allowed; statements that still fail are
   withheld. A report uses one writer per topic, each seeing only its own facts.
3. Critic  - optionally reviews each statement against only the facts it cites.
4. Editor  - for a report, selects validated statements by ID and writes the
   headline. If the main answer cannot be validated, the run fails.

Python's checks are grounding checks and the critic is a second model opinion;
neither proves that an interpretation is correct.
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
import re
from time import perf_counter

from . import prompts
from .runtime import Run
from .settings import TOOL_MODES
from .support import canonical, read_json, sha256_json


class AnalysisError(RuntimeError):
    """The model could not produce a validated answer; safe to show to the user."""


# ===========================================================================
# Fact ledger
# ===========================================================================

class Ledger:
    """Citable facts by ID (def.<key> or t<n>.<key>) and the calls that produced them."""

    def __init__(self, definitions=None):
        self.facts = {}
        self.calls = []
        self._signatures = {}
        self.definition_ids = []
        for key, sentence in (definitions or {}).items():
            fact_id = f"def.{key}"
            self.definition_ids.append(fact_id)
            self.facts[fact_id] = sentence

    def record(self, tool, arguments, output, *, origin):
        """Store a successful call once; an identical repeat returns (first ID, True)."""
        signature = (tool, canonical(arguments))
        if signature in self._signatures:
            return self._signatures[signature], True

        # Prefix tool-local keys with a call ID: "loss" becomes "t1.loss".
        # This keeps facts from different tools or scenarios distinct.
        call_id = f"t{len(self.calls) + 1}"
        fact_ids = []
        for key, sentence in output.facts.items():
            fact_id = f"{call_id}.{key}"
            fact_ids.append(fact_id)
            self.facts[fact_id] = sentence

        highlights = [f"{call_id}.{key}" for key in output.highlights if key in output.facts]
        self.calls.append({
            "id": call_id,
            "tool": tool,
            "arguments": arguments,
            "origin": origin,
            "title": output.title,
            "fact_ids": fact_ids,
            "highlights": highlights,
            "result": output.result,
        })
        self._signatures[signature] = call_id
        return call_id, False

    def call(self, call_id):
        for call in self.calls:
            if call["id"] == call_id:
                return call
        raise LookupError(f"unknown call {call_id}")

    def find(self, tool, arguments):
        """The recorded call with exactly this tool and arguments, or None."""
        call_id = self._signatures.get((tool, canonical(arguments)))
        return None if call_id is None else self.call(call_id)

    def select(self, ids):
        """Return the requested fact sentences, preserving the requested order."""
        return {fact_id: self.facts[fact_id] for fact_id in ids}

    def highlights(self, origin="report"):
        """Collect the facts selected by tools as the starting point for an answer."""
        fact_ids = []
        for call in self.calls:
            if call["origin"] == origin:
                fact_ids.extend(call["highlights"])
        return fact_ids


# ===========================================================================
# Answer schema and validation
# ===========================================================================

def answer_schema(sections, evidence_ids):
    """Structured-output schema; evidence IDs are limited to facts the model has seen."""
    statement_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["evidence_ids", "text"],
        "properties": {
            "evidence_ids": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {"type": "string", "enum": list(evidence_ids)},
            },
            "text": {"type": "string", "minLength": 1, "maxLength": 500},
        },
    }

    section_schemas = {}
    for name, (minimum, maximum) in sections.items():
        section_schemas[name] = {
            "type": "array",
            "minItems": minimum,
            "maxItems": maximum,
            "items": statement_schema,
        }

    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(sections),
        "properties": section_schemas,
    }


_NUMERIC_TOKEN = re.compile(
    r"(?<![\w.])(?P<currency>\$)?[+-]?(?P<number>[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)(?P<percent>%)?")
_DECISIONS = re.compile(r"\b(accepts|rejects|approves|denies)\b", re.IGNORECASE)


def numeric_tokens(text):
    """Numeric magnitudes with units: 13.8% matches 13.80%, but 14% or $13.80 do not."""
    tokens = set()
    for match in _NUMERIC_TOKEN.finditer(text):
        magnitude = Decimal(match["number"].replace(",", ""))
        if match["currency"]:
            unit = "USD"
        elif match["percent"]:
            unit = "%"
        else:
            unit = "number"
        tokens.add((magnitude, unit))
    return tokens


def _show(token):
    value, unit = token
    if unit == "USD":
        return f"${value:f}"
    if unit == "%":
        return f"{value:f}%"
    return f"{value:f}"


def check_statement(item, facts):
    """Problems with one statement; an empty list means it may be shown."""
    if not isinstance(item, dict) or set(item) != {"text", "evidence_ids"}:
        return ["each item needs exactly text and evidence_ids"]

    text = item["text"]
    references = item["evidence_ids"]
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 500:
        return ["text must be a short nonempty paragraph"]
    if (not isinstance(references, list) or not 1 <= len(references) <= 4
            or any(not isinstance(ref, str) for ref in references)):
        return ["cite one to four distinct evidence IDs"]
    if len(set(references)) != len(references):
        return ["cite one to four distinct evidence IDs"]

    unknown = [ref for ref in references if ref not in facts]
    if unknown:
        return ["cites unknown evidence " + ", ".join(ref[:40] for ref in unknown[:3])]

    problems = []
    if _DECISIONS.search(text):
        problems.append("describe retained historical loans, not accept/reject/approve/deny decisions")

    # Check only the facts this statement cites, not every fact in the ledger.
    cited_text = " ".join(facts[ref] for ref in references)
    unsupported = numeric_tokens(text) - numeric_tokens(cited_text)
    if unsupported:
        quoted_values = ", ".join(sorted(_show(token) for token in unsupported))
        problems.append("quotes " + quoted_values + " not found in its cited facts")
    return problems


def validate_answer(value, facts, sections):
    """Return (kept, withheld, problems); kept holds only statements that pass every check."""
    if not isinstance(value, dict):
        return None, [], ["the response must be a JSON object with the required sections"]

    problems = [f"unexpected section {str(name)[:40]}" for name in value if name not in sections]
    kept = {}
    withheld = []

    for name, (minimum, maximum) in sections.items():
        items = value.get(name, [])
        if not isinstance(items, list):
            problems.append(f"{name} must be a list")
            items = []

        kept[name] = []
        for index, item in enumerate(items):
            issues = check_statement(item, facts)
            if not issues and len(kept[name]) >= maximum:
                issues = [f"{name} allows at most {maximum} items"]
            if issues:
                problems.append(f"{name}[{index}]: " + "; ".join(issues))
                withheld.append({"section": name, "item": item, "problems": issues})
            else:
                kept[name].append(item)

        if len(items) < minimum:
            plural = "" if minimum == 1 else "s"
            problems.append(f"{name} needs at least {minimum} item{plural}")

    return kept, withheld, problems


# ===========================================================================
# The analyst: planner, writer, critic and editor
# ===========================================================================

REPORT_SECTIONS = {"answer": (1, 1), "findings": (2, 5), "caveats": (1, 3), "next_steps": (1, 3)}
ASK_SECTIONS = {"answer": (1, 1), "findings": (0, 4), "caveats": (0, 2), "next_steps": (0, 2)}
# What one topic writer produces; the editor later selects among these.
TOPIC_SECTIONS = {"findings": (1, 3), "caveats": (0, 1)}
# The only parts of a report answer the editor writes itself.
EDITOR_SECTIONS = {"answer": REPORT_SECTIONS["answer"], "next_steps": REPORT_SECTIONS["next_steps"]}
MAX_TOPIC_FACTS = 40  # keeps one writer's prompt small enough for a 4B model
VERDICTS = ("supported", "not_supported", "overreach")
VALIDATION_NOTE = ("Structure, evidence references and quoted numeric magnitudes/units checked by Python; "
                   "where a critic pass ran, it is a second model opinion, not proof. "
                   "Interpretation requires review.")
USAGE_KEYS = ("prompt_eval_count", "eval_count", "total_duration", "load_duration")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Topic:
    """One part of the report, written by a focused model call that sees only its own facts.

    ``calls`` are standard report calls (tool, arguments) whose facts the topic
    covers. ``tools`` are the tools the planner may add for it; ``dimensions``
    says which segment breakdowns belong here when a tool serves several topics.
    """

    name: str
    goal: str
    calls: tuple
    tools: tuple
    definitions: tuple = ()
    dimensions: tuple = ()


def route(topics, tool, arguments):
    """Pick the topic that should write about a model-requested call, or None."""
    candidates = [topic for topic in topics if tool in topic.tools]
    for topic in candidates:
        if arguments.get("dimension") in topic.dimensions:
            return topic.name
    return candidates[0].name if candidates else None


def _quiet(event, **details):
    pass


def cited_ids(answer):
    """Return unique evidence IDs in the order they first appear in the answer."""
    references = []
    for statements in answer.values():
        for statement in statements:
            for fact_id in statement["evidence_ids"]:
                if fact_id not in references:
                    references.append(fact_id)
    return references


def short_arguments(arguments):
    """Keep invalid tool arguments short and JSON-safe in the saved transcript."""
    try:
        return json.dumps(arguments, ensure_ascii=False, allow_nan=False)[:500]
    except (TypeError, ValueError):
        return "<unserializable>"


def sentences(text):
    return [" ".join(part.split()) for part in _SENTENCE_END.split(text.strip()) if part.strip()]


def is_verbatim(item, facts):
    """True when a statement only repeats whole sentences of the facts it cites.

    Such a statement cannot say more than its evidence, so the critic skips it.
    """
    available = set()
    for fact_id in item["evidence_ids"]:
        available.update(sentences(facts[fact_id]))
    return all(sentence in available for sentence in sentences(item["text"]))


def verdict_schema(statement_ids):
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdicts"],
        "properties": {"verdicts": {
            "type": "array",
            "minItems": len(statement_ids),
            "maxItems": len(statement_ids),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["statement", "verdict", "reason"],
                "properties": {
                    "statement": {"type": "string", "enum": list(statement_ids)},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "reason": {"type": "string", "maxLength": 200},
                },
            },
        }},
    }


def editor_schema(fact_ids, pools, limits):
    """The editor writes a headline and next steps, and selects statements by ID."""
    schema = answer_schema(EDITOR_SECTIONS, fact_ids)
    for section, pool in pools.items():
        if not pool:
            continue
        minimum, maximum = limits[section]
        schema["properties"][section] = {
            "type": "array", "minItems": minimum, "maxItems": maximum,
            "items": {"type": "string", "enum": [statement["id"] for statement in pool]},
        }
        schema["required"].append(section)
    return schema


class Analyst:
    """Plans tool calls, writes cited statements, has them reviewed and assembles the answer.

    Two entry points share the same roles:

    * ``run``        one question: plan -> write -> (critic) -> answer
    * ``run_report`` a report: plan -> one writer per topic -> (critic) -> editor

    Every model call goes through ``call_model``, so all of them are journaled,
    traced and counted against the budget.
    """

    def __init__(self, client, registry, context, ledger, *, max_rounds=4, max_calls=8,
                 on_event=_quiet, run=None, tool_mode="native", critic=False):
        if tool_mode not in TOOL_MODES:
            raise ValueError(f"tool_mode must be one of: {', '.join(TOOL_MODES)}")
        self.client = client
        # The Run adds journal replay, one transient retry, a call budget and
        # tracing. Without one (tests, simple scripts) calls go straight through.
        self.model_run = run or Run(client)
        self.registry = registry
        self.context = context
        self.ledger = ledger
        self.max_rounds = max_rounds
        self.max_calls = max_calls
        self.on_event = on_event
        self.tool_mode = tool_mode
        self.critic = critic
        self.begin(())

    def begin(self, preload, topics=()):
        """Reset the per-run records. Facts and tool results stay in the shared ledger."""
        self.model_run.reset_budget()
        # Facts this model has seen. It grows as tools run; only these IDs may be cited.
        self.visible_fact_ids = list(dict.fromkeys([*self.ledger.definition_ids, *preload]))
        self.topics = tuple(topics)
        self.exchanges = []      # every model request and reply, in order
        self.calls = []          # every tool request the model made, valid or not
        self.reviews = []        # every critic verdict
        self.routed = {}         # topic name -> fact IDs from model-requested calls
        self._supported = set()  # statements the critic already accepted in this run

    # -----------------------------------------------------------------------
    # Entry point 1: one question
    # -----------------------------------------------------------------------

    def run(self, goal, *, sections=ASK_SECTIONS, preload=(), history=()):
        """Investigate, compose and validate; return a JSON-safe analysis artifact."""
        started = perf_counter()
        self.begin(preload)
        tools = self.registry.definitions()

        self.plan(goal, history, tools)
        answer, withheld, attempts, facts = self.write("compose", goal, sections,
                                                       self.visible_fact_ids, history)

        # Other sections may lose unsupported statements; the main answer is
        # required, so losing it fails the run before report files are replaced.
        if answer is None or not answer["answer"]:
            raise AnalysisError("Model answer failed evidence validation: " + "; ".join(attempts[-1][:3]))
        return self.artifact(goal, started, tools, answer, withheld, facts,
                             attempts=len(attempts), validation_problems=attempts, flow="ask")

    # -----------------------------------------------------------------------
    # Entry point 2: a report written topic by topic
    # -----------------------------------------------------------------------

    def run_report(self, goal, topics, *, preload=()):
        """Plan follow-up analyses, write each topic separately, then let the editor assemble."""
        started = perf_counter()
        self.begin(preload, topics)
        tools = self.registry.definitions()

        self.plan(goal, (), tools)

        statements, withheld, problems, facts = [], [], {}, {}
        for topic in topics:
            fact_ids = self.topic_fact_ids(topic)
            if fact_ids is None:
                continue  # this snapshot has no data for the topic (e.g. no grade column)

            stage = f"write:{topic.name}"
            self.on_event("topic", name=topic.name)
            kept, topic_withheld, attempts, topic_facts = self.write(stage, topic.goal, TOPIC_SECTIONS, fact_ids)
            problems[stage] = attempts
            facts.update(topic_facts)
            for entry in topic_withheld:
                withheld.append({**entry, "stage": stage})
            for section, items in (kept or {}).items():
                for item in items:
                    statements.append({"id": f"{topic.name}.{len(statements) + 1}", "topic": topic.name,
                                       "section": section, "placement": None, **item})

        answer, editor_withheld, attempts, editor_facts = self.edit(goal, statements)
        problems["edit"] = attempts
        facts.update(editor_facts)
        withheld += [{**entry, "stage": "edit"} for entry in editor_withheld]

        return self.artifact(goal, started, tools, answer, withheld, facts,
                             attempts=max(len(attempts) for attempts in problems.values()),
                             validation_problems=problems, flow="report", statements=statements)

    def topic_fact_ids(self, topic):
        """Definitions, then the topic's highlights, then its other facts, up to the cap."""
        calls = [self.ledger.find(tool, arguments) for tool, arguments in topic.calls]
        calls = [call for call in calls if call is not None]
        if not calls:
            return None

        highlights, others = [], []
        for call in calls:
            highlights += call["highlights"]
            others += [fact_id for fact_id in call["fact_ids"] if fact_id not in call["highlights"]]
        definitions = [f"def.{key}" for key in topic.definitions if f"def.{key}" in self.ledger.facts]
        # Facts from follow-up calls the planner requested come before the long tail.
        ordered = [*definitions, *highlights, *self.routed.get(topic.name, []), *others]
        return list(dict.fromkeys(ordered))[:MAX_TOPIC_FACTS]

    # -----------------------------------------------------------------------
    # Role: planner (chooses tool calls)
    # -----------------------------------------------------------------------

    def plan(self, goal, history, tools):
        """Let the model request tools, within the round and call limits."""
        if self.max_rounds == 0:
            return
        if self.tool_mode == "schema":
            self.plan_with_schema(goal, history)
        else:
            self.investigate(goal, history, tools)

    def plan_with_schema(self, goal, history):
        """Ask for a JSON list of calls constrained by a schema; an empty list means done.

        Constrained decoding does not depend on the model's own tool-call format,
        which makes small local models more reliable. ToolRegistry.call still
        validates every argument before anything runs.
        """
        if self.topics:
            choices = [(topic.name, tool) for topic in self.topics for tool in topic.tools]
        else:
            choices = [(None, name) for name in self.registry.names()]
        tool_names = list(dict.fromkeys(tool for _, tool in choices))
        schema = self.registry.action_schema(choices, max_calls=min(4, self.max_calls))

        messages = [
            {"role": "system", "content": prompts.PLANNER},
            *history,
            {"role": "user", "content": prompts.plan(
                goal, self.ledger.select(self.visible_fact_ids),
                self.registry.definitions(tool_names), self.topics)},
        ]

        for round_number in range(1, self.max_rounds + 1):
            self.on_event("investigating", round=round_number)
            reply = self.call_model("plan", messages, format=schema, num_predict=768)
            try:
                requested = read_json(reply["content"])["calls"]
            except (ValueError, KeyError, TypeError):
                requested = None
            if not isinstance(requested, list) or not requested:
                break

            results = []
            for item in requested:
                item = item if isinstance(item, dict) else {}
                call = {"name": item.get("tool"), "arguments": item.get("arguments")}
                results.append(self.execute_tool(call, topic=item.get("topic")))
            messages += [
                {"role": "assistant", "content": reply["content"]},
                {"role": "user", "content": prompts.plan_results(results)},
            ]

    def investigate(self, goal, history, tools):
        """Native tool calling: the model's replies carry tool_calls; results return as tool messages."""
        messages = [
            {"role": "system", "content": prompts.SYSTEM},
            *history,
            {"role": "user", "content": prompts.investigate(
                goal, self.ledger.select(self.visible_fact_ids))},
        ]

        for round_number in range(1, self.max_rounds + 1):
            self.on_event("investigating", round=round_number)
            reply = self.call_model("investigate", messages, tools=tools)
            if not reply["tool_calls"]:
                break

            requested_tools = []
            for index, call in enumerate(reply["tool_calls"]):
                requested_tools.append({
                    "type": "function",
                    "function": {"index": index, "name": call["name"], "arguments": call["arguments"]},
                })
            messages.append({
                "role": "assistant",
                "content": reply["content"],
                "tool_calls": requested_tools,
            })

            for call in reply["tool_calls"]:
                messages.append({
                    "role": "tool",
                    "tool_name": str(call["name"]),
                    "content": self.execute_tool(call),
                })

    def call_model(self, phase, messages, **options):
        """Call the model and save the request, response and usage for review."""
        reply, replayed = self.model_run.chat(phase, messages, **options)

        request = dict(reply["request"])
        if "tools" in request:
            # Stored once as tool_definitions; keep a verifiable reference here.
            request["tools"] = {"sha256": sha256_json(request["tools"])}

        exchange = {
            "phase": phase,
            "request": request,
            "usage": reply["usage"],
            "response": {"content": reply["content"], "tool_calls": reply["tool_calls"]},
        }
        if replayed:
            exchange["replayed"] = True  # answered from the run journal after --resume
        self.exchanges.append(exchange)
        return reply

    def execute_tool(self, call, topic=None):
        """Run one model-requested call through the registry; return the tool message text."""
        name = call["name"]
        arguments = call["arguments"]

        # Invalid and repeated calls consume the budget too. Requests beyond the
        # budget receive an error message without executing or recording a call.
        if len(self.calls) >= self.max_calls:
            return "ERROR budget_exhausted: the tool budget is used up; stop calling tools and answer."

        with self.model_run.tracer.span(f"execute_tool {str(name)[:80]}", **{
                "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": str(name)[:80]}) as attributes:
            envelope = self.registry.call(name, arguments, self.context)
            attributes["ok"] = envelope["ok"]
        if not envelope["ok"]:
            error = envelope["error"]
            self.calls.append({"tool": str(name)[:80], "arguments": short_arguments(arguments),
                               "error": error})
            self.on_event("tool_error", name=str(name)[:80], error=error)
            return f"ERROR {error['code']}: {error['message']}"

        call_id, repeated = self.ledger.record(envelope["tool"], envelope["arguments"],
                                               envelope["output"], origin="model")
        record = self.ledger.call(call_id)
        for fact_id in record["fact_ids"]:
            if fact_id not in self.visible_fact_ids:
                self.visible_fact_ids.append(fact_id)

        # In a report, the new facts go to the one topic that will write about them.
        if self.topics:
            allowed = any(item.name == topic and envelope["tool"] in item.tools for item in self.topics)
            target = topic if allowed else route(self.topics, envelope["tool"], envelope["arguments"])
            if target is not None:
                self.routed.setdefault(target, []).extend(record["fact_ids"])

        self.calls.append({"id": call_id, "tool": envelope["tool"], "arguments": envelope["arguments"],
                           "repeated": repeated})
        self.on_event("tool", call_id=call_id, name=envelope["tool"],
                      arguments=envelope["arguments"], repeated=repeated, title=record["title"])

        note = " (already computed; same facts as before)" if repeated else ""
        return (f"{call_id}: {record['title']}{note}\n"
                + prompts.fact_sheet(self.ledger.select(record["fact_ids"])))

    # -----------------------------------------------------------------------
    # Role: writer (drafts cited statements; Python checks them)
    # -----------------------------------------------------------------------

    def write(self, stage, goal, sections, fact_ids, history=()):
        """Draft statements from these facts only; allow one repair; keep what passes.

        Returns (kept, withheld, attempts, facts). ``kept`` is None when the
        reply was not a usable JSON object. ``attempts`` lists each try's problems.
        """
        facts = self.ledger.select(fact_ids)
        schema = answer_schema(sections, list(facts))
        messages = [
            {"role": "system", "content": prompts.SYSTEM},
            *history,
            {"role": "user", "content": prompts.compose(goal, sections, facts)},
        ]

        attempts = []
        for attempt in range(2):  # one bounded repair; the snapshot is never reread
            self.on_event("composing", attempt=attempt + 1, stage=stage)
            reply = self.call_model(stage, messages, format=schema, num_predict=2048)

            try:
                value = read_json(reply["content"])
            except ValueError:
                value = None

            kept, withheld, problems = validate_answer(value, facts, sections)
            self.review(stage, kept, facts, withheld, problems)
            attempts.append(problems)
            if not problems:
                break
            if attempt == 0:
                messages.extend([
                    {"role": "assistant", "content": reply["content"]},
                    {"role": "user", "content": prompts.repair(problems)},
                ])
        return kept, withheld, attempts, facts

    # -----------------------------------------------------------------------
    # Role: critic (a second opinion on interpretation)
    # -----------------------------------------------------------------------

    def review(self, stage, kept, facts, withheld, problems):
        """Move statements the critic does not accept from ``kept`` to ``withheld``."""
        if not self.critic or kept is None:
            return
        for section, item, verdict, reason in self.critique(stage, kept, facts):
            kept[section].remove(item)
            issue = f"critic: {verdict}" + (f" - {reason}" if reason else "")
            withheld.append({"section": section, "item": item, "problems": [issue]})
            problems.append(f'{section} "{item["text"][:80]}": {issue}')

    def critique(self, stage, kept, facts):
        """Ask the critic whether each statement says only what its cited facts support.

        Returns the statements that were not judged "supported" as
        (section, item, verdict, reason). A missing verdict counts against the
        statement. The critic is the same kind of model as the writer, so its
        verdict is a filter, never proof.
        """
        pending = []
        for section, items in kept.items():
            for item in items:
                key = (item["text"], tuple(item["evidence_ids"]))
                if key not in self._supported and not is_verbatim(item, facts):
                    pending.append((section, item, key))
        if not pending:
            return []

        statement_ids = [f"s{number}" for number in range(1, len(pending) + 1)]
        listed = [(statement_id, item["text"], {ref: facts[ref] for ref in item["evidence_ids"]})
                  for statement_id, (_, item, _) in zip(statement_ids, pending)]
        messages = [
            {"role": "system", "content": prompts.CRITIC},
            {"role": "user", "content": prompts.critique(listed)},
        ]
        reply = self.call_model(f"critic:{stage}", messages, format=verdict_schema(statement_ids),
                                num_predict=768)

        verdicts = {}
        try:
            for entry in read_json(reply["content"])["verdicts"]:
                verdicts[entry["statement"]] = entry
        except (ValueError, KeyError, TypeError):
            pass  # an unreadable review supports nothing

        flagged = []
        for statement_id, (section, item, key) in zip(statement_ids, pending):
            entry = verdicts.get(statement_id, {})
            verdict = entry.get("verdict") if entry.get("verdict") in VERDICTS else "no_verdict"
            reason = " ".join(str(entry.get("reason", "")).split())[:200]
            self.reviews.append({"stage": stage, "text": item["text"], "evidence_ids": item["evidence_ids"],
                                 "verdict": verdict, "reason": reason})
            if verdict == "supported":
                self._supported.add(key)
            else:
                flagged.append((section, item, verdict, reason))

        self.on_event("reviewed", stage=stage, reviewed=len(pending), flagged=len(flagged))
        return flagged

    # -----------------------------------------------------------------------
    # Role: editor (assembles the report answer from validated statements)
    # -----------------------------------------------------------------------

    def edit(self, goal, statements):
        """Select validated statements by ID and write only the headline and next steps.

        Selecting by ID cannot introduce a new number or claim. The two parts
        the editor writes itself pass the same checks as any other statement.
        """
        pools = {section: [statement for statement in statements if statement["section"] == section]
                 for section in ("findings", "caveats")}
        if not pools["findings"]:
            raise AnalysisError("Model answer failed evidence validation: no report topic produced a "
                                "validated finding")

        limits = {}
        for section, pool in pools.items():
            minimum, maximum = REPORT_SECTIONS[section]
            limits[section] = (min(minimum, len(pool)), min(maximum, len(pool)))

        fact_ids = list(dict.fromkeys([
            *self.ledger.definition_ids,
            *(fact_id for statement in statements for fact_id in statement["evidence_ids"])]))
        facts = self.ledger.select(fact_ids)
        schema = editor_schema(list(facts), pools, limits)
        messages = [
            {"role": "system", "content": prompts.SYSTEM},
            {"role": "user", "content": prompts.edit(goal, pools, limits, facts)},
        ]

        attempts = []
        for attempt in range(2):
            self.on_event("composing", attempt=attempt + 1, stage="edit")
            reply = self.call_model("edit", messages, format=schema, num_predict=1536)
            try:
                value = read_json(reply["content"])
            except ValueError:
                value = None
            value = value if isinstance(value, dict) else {}

            written = {section: value.get(section, []) for section in EDITOR_SECTIONS}
            kept, withheld, problems = validate_answer(written, facts, EDITOR_SECTIONS)
            self.review("edit", kept, facts, withheld, problems)

            selected = {}
            for section, pool in pools.items():
                known = {statement["id"]: statement for statement in pool}
                requested = value.get(section, [])
                requested = requested if isinstance(requested, list) else []
                chosen = [known[ref] for ref in dict.fromkeys(ref for ref in requested if isinstance(ref, str))
                          if ref in known]
                if len(chosen) < limits[section][0]:
                    problems.append(f"{section}: select at least {limits[section][0]} of the listed statement IDs")
                selected[section] = chosen[:limits[section][1]]

            attempts.append(problems)
            if not problems:
                break
            if attempt == 0:
                messages.extend([
                    {"role": "assistant", "content": reply["content"]},
                    {"role": "user", "content": prompts.repair(problems)},
                ])

        if not kept["answer"]:
            raise AnalysisError("Model answer failed evidence validation: " + "; ".join(attempts[-1][:3]))

        # If the editor still selected too few, fill up in topic order rather than
        # lose validated statements; the artifact records which were placed this way.
        for section, pool in pools.items():
            for statement in pool:
                if len(selected[section]) >= limits[section][0]:
                    break
                if statement not in selected[section]:
                    selected[section].append(statement)
                    statement["placed_by"] = "fallback"
            for statement in selected[section]:
                statement["placement"] = section

        answer = {
            "answer": kept["answer"],
            "findings": [{"text": item["text"], "evidence_ids": item["evidence_ids"]}
                         for item in selected["findings"]],
            "caveats": [{"text": item["text"], "evidence_ids": item["evidence_ids"]}
                        for item in selected["caveats"]],
            "next_steps": kept["next_steps"],
        }
        return answer, withheld, attempts, facts

    # -----------------------------------------------------------------------
    # The saved artifact
    # -----------------------------------------------------------------------

    def artifact(self, goal, started, tools, answer, withheld, facts, *, attempts, validation_problems,
                 flow, statements=None):
        """Everything needed to review the run later; saved as ollama_analysis.json."""
        statement_count = sum(len(items) for items in answer.values())
        self.on_event("answered", statements=statement_count, withheld=len(withheld))

        usage = {}
        for key in USAGE_KEYS:
            usage[key] = sum(exchange["usage"].get(key) or 0 for exchange in self.exchanges)

        result = {
            "status": "complete",
            **(self.client.metadata or {}),
            "prompt_version": prompts.VERSION,
            "agent": {"flow": flow, "tool_mode": self.tool_mode, "critic": self.critic},
            "goal": goal,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "elapsed_seconds": perf_counter() - started,
            "attempts": attempts,
            "validation_problems": validation_problems,
            "tool_definitions": tools,
            "tool_definitions_sha256": sha256_json(tools),
            "exchanges": self.exchanges,
            "tool_calls": self.calls,
            "facts": facts,
            "model_draft": answer,
            "answer": deepcopy(answer),
            "withheld": withheld,
            "critic_reviews": self.reviews,
            "validation": VALIDATION_NOTE,
            "usage": usage,
        }
        if statements is not None:
            result["statements"] = statements
        return result


def answer_text(answer):
    """Plain text of an answer with its evidence IDs, for chat history."""
    parts = []
    for section, items in answer.items():
        for item in items:
            prefix = "" if section == "answer" else f"{section.replace('_', ' ').capitalize()}: "
            parts.append(f"{prefix}{item['text']} (evidence: {', '.join(item['evidence_ids'])})")
    return "\n".join(parts)


class ChatSession:
    """Multi-turn questions over one snapshot; recent answers and their evidence carry over."""

    def __init__(self, analyst, preload=(), memory=3):
        self.analyst = analyst
        self.preload = list(preload)
        self.memory = memory
        self.turns = []

    def ask(self, question):
        """Answer using recent successful turns and the facts those answers cited."""
        history = []
        carried_fact_ids = []
        for turn in self.turns[-self.memory:]:
            previous_answer = turn["result"]["answer"]
            history.extend([
                {"role": "user", "content": turn["question"]},
                {"role": "assistant", "content": answer_text(previous_answer)},
            ])
            carried_fact_ids.extend(cited_ids(previous_answer))

        result = self.analyst.run(
            question,
            sections=ASK_SECTIONS,
            preload=[*self.preload, *carried_fact_ids],
            history=history,
        )
        self.turns.append({"question": question, "result": result})
        return result
