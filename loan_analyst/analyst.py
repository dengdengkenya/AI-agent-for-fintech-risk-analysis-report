"""The local analyst: citable facts, answer validation, and the tool-calling loop.

This is the only part of the program where model text is involved. It works in
two phases:

1. Investigation - the model may call allowlisted tools. Each successful call
   adds its fact sentences to the Ledger and gives them IDs such as ``t1.scope``.
2. Composition - the model writes a JSON answer that cites those IDs. Python
   checks every statement. One repair attempt is allowed; statements that still
   fail are withheld. If the main answer cannot be validated, the run fails.

The checks are grounding checks, not proof that an interpretation is correct.
"""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
import re
from time import perf_counter

from . import prompts
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
# The analyst loop
# ===========================================================================

REPORT_SECTIONS = {"answer": (1, 1), "findings": (2, 5), "caveats": (1, 3), "next_steps": (1, 3)}
ASK_SECTIONS = {"answer": (1, 1), "findings": (0, 4), "caveats": (0, 2), "next_steps": (0, 2)}
VALIDATION_NOTE = ("Structure, evidence references and quoted numeric magnitudes/units checked; "
                   "interpretation requires review.")
USAGE_KEYS = ("prompt_eval_count", "eval_count", "total_duration", "load_duration")


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


class Analyst:
    """Runs one goal through bounded tool rounds and a validated, structured answer."""

    def __init__(self, client, registry, context, ledger, *, max_rounds=4, max_calls=8,
                 on_event=_quiet):
        self.client = client
        self.registry = registry
        self.context = context
        self.ledger = ledger
        self.max_rounds = max_rounds
        self.max_calls = max_calls
        self.on_event = on_event
        # Facts this model has seen. It grows as tools run; only these IDs may
        # be cited in the answer.
        self.visible_fact_ids = []

    def run(self, goal, *, sections=ASK_SECTIONS, preload=(), history=()):
        """Investigate, compose and validate; return a JSON-safe analysis artifact."""
        started = perf_counter()

        self.visible_fact_ids = list(dict.fromkeys([*self.ledger.definition_ids, *preload]))
        tools = self.registry.definitions()
        exchanges = []
        calls = []

        self.investigate(goal, history, tools, exchanges, calls)
        answer, withheld, attempts, facts = self.compose(goal, sections, exchanges, history)

        statement_count = sum(len(statements) for statements in answer.values())
        self.on_event("answered", statements=statement_count, withheld=len(withheld))

        usage = {}
        for key in USAGE_KEYS:
            usage[key] = sum(exchange["usage"].get(key) or 0 for exchange in exchanges)

        return {
            "status": "complete",
            **(self.client.metadata or {}),
            "prompt_version": prompts.VERSION,
            "goal": goal,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "elapsed_seconds": perf_counter() - started,
            "attempts": len(attempts),
            "validation_problems": attempts,
            "tool_definitions": tools,
            "tool_definitions_sha256": sha256_json(tools),
            "exchanges": exchanges,
            "tool_calls": calls,
            "facts": facts,
            "model_draft": answer,
            "answer": deepcopy(answer),
            "withheld": withheld,
            "validation": VALIDATION_NOTE,
            "usage": usage,
        }

    def investigate(self, goal, history, tools, exchanges, calls):
        """Let the model request tools, keeping their replies in conversation order."""
        messages = [
            {"role": "system", "content": prompts.SYSTEM},
            *history,
            {"role": "user", "content": prompts.investigate(
                goal, self.ledger.select(self.visible_fact_ids))},
        ]

        for round_number in range(1, self.max_rounds + 1):
            self.on_event("investigating", round=round_number)
            reply = self.call_model(exchanges, "investigate", messages, tools=tools)
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
                    "content": self.execute_tool(call, calls),
                })

    def call_model(self, exchanges, phase, messages, **options):
        """Call the model and save the request, response and usage for review."""
        reply = self.client.chat(messages, **options)

        request = dict(reply["request"])
        if "tools" in request:
            # Stored once as tool_definitions; keep a verifiable reference here.
            request["tools"] = {"sha256": sha256_json(request["tools"])}

        exchanges.append({
            "phase": phase,
            "request": request,
            "usage": reply["usage"],
            "response": {"content": reply["content"], "tool_calls": reply["tool_calls"]},
        })
        return reply

    def execute_tool(self, call, calls):
        """Run one model-requested call through the registry; return the tool message text."""
        name = call["name"]
        arguments = call["arguments"]

        # Invalid and repeated calls consume the budget too. Requests beyond the
        # budget receive an error message without executing or recording a call.
        if len(calls) >= self.max_calls:
            return "ERROR budget_exhausted: the tool budget is used up; stop calling tools and answer."

        envelope = self.registry.call(name, arguments, self.context)
        if not envelope["ok"]:
            error = envelope["error"]
            calls.append({"tool": str(name)[:80], "arguments": short_arguments(arguments),
                          "error": error})
            self.on_event("tool_error", name=str(name)[:80], error=error)
            return f"ERROR {error['code']}: {error['message']}"

        call_id, repeated = self.ledger.record(envelope["tool"], envelope["arguments"],
                                               envelope["output"], origin="model")
        record = self.ledger.call(call_id)
        for fact_id in record["fact_ids"]:
            if fact_id not in self.visible_fact_ids:
                self.visible_fact_ids.append(fact_id)

        calls.append({"id": call_id, "tool": envelope["tool"], "arguments": envelope["arguments"],
                      "repeated": repeated})
        self.on_event("tool", call_id=call_id, name=envelope["tool"],
                      arguments=envelope["arguments"], repeated=repeated, title=record["title"])

        note = " (already computed; same facts as before)" if repeated else ""
        return (f"{call_id}: {record['title']}{note}\n"
                + prompts.fact_sheet(self.ledger.select(record["fact_ids"])))

    def compose(self, goal, sections, exchanges, history):
        """Draft an answer, allow one repair, then retain only validated statements."""
        # A fresh, compact prompt: earlier conversation for context, then every fact the model saw.
        facts = self.ledger.select(self.visible_fact_ids)
        schema = answer_schema(sections, list(facts))
        messages = [
            {"role": "system", "content": prompts.SYSTEM},
            *history,
            {"role": "user", "content": prompts.compose(goal, sections, facts)},
        ]

        attempts = []
        for attempt in range(2):  # one bounded repair; the snapshot is never reread
            self.on_event("composing", attempt=attempt + 1)
            reply = self.call_model(exchanges, "compose", messages, format=schema, num_predict=2048)

            try:
                value = read_json(reply["content"])
            except ValueError:
                value = None

            kept, withheld, problems = validate_answer(value, facts, sections)
            attempts.append(problems)
            if not problems:
                break
            if attempt == 0:
                messages.extend([
                    {"role": "assistant", "content": reply["content"]},
                    {"role": "user", "content": prompts.repair(problems)},
                ])

        # Other sections may lose unsupported statements; the main answer is
        # required, so losing it fails the run before report files are replaced.
        if kept is None or not kept["answer"]:
            raise AnalysisError("Model answer failed evidence validation: " + "; ".join(problems[:3]))
        return kept, withheld, attempts, facts


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
