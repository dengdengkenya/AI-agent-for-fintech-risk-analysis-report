"""Tool-calling analyst: investigate with registered tools, then answer with cited facts.

Phase 1 lets the local model call allowlisted tools over the in-memory snapshot. Phase 2
asks for a structured answer citing fact IDs. Python validates every statement. After
one repair attempt, statements that still fail are withheld and recorded; the run fails
only when the main answer itself cannot be grounded in the evidence.
"""

from copy import deepcopy
from datetime import datetime, timezone
import json
from time import perf_counter

from . import prompts
from .evidence import AnalysisError, answer_schema, validate_answer
from .jsonutil import read_json, sha256_json

REPORT_SECTIONS = {"answer": (1, 1), "findings": (2, 5), "caveats": (1, 3), "next_steps": (1, 3)}
ASK_SECTIONS = {"answer": (1, 1), "findings": (0, 4), "caveats": (0, 2), "next_steps": (0, 2)}
VALIDATION_NOTE = ("Structure, evidence references and quoted numeric magnitudes/units checked; "
                   "interpretation requires review.")
USAGE_KEYS = ("prompt_eval_count", "eval_count", "total_duration", "load_duration")


def _quiet(event, **details):
    pass


def cited_ids(answer):
    return list(dict.fromkeys(ref for items in answer.values() for item in items for ref in item["evidence_ids"]))


def _bounded(arguments):
    try:
        return json.dumps(arguments, ensure_ascii=False, allow_nan=False)[:500]
    except (TypeError, ValueError):
        return "<unserializable>"


class Analyst:
    """Runs one goal through bounded tool rounds and a validated, structured answer."""

    def __init__(self, client, registry, context, ledger, *, max_rounds=4, max_calls=8, on_event=_quiet):
        self.client, self.registry, self.context, self.ledger = client, registry, context, ledger
        self.max_rounds, self.max_calls, self.on_event = max_rounds, max_calls, on_event

    def run(self, goal, *, sections=ASK_SECTIONS, preload=(), history=()):
        """Investigate, compose and validate; returns a JSON-safe analysis artifact."""
        started = perf_counter()
        self._visible = list(dict.fromkeys([*self.ledger.definition_ids, *preload]))
        tools = self.registry.definitions()
        exchanges, calls = [], []
        messages = [{"role": "system", "content": prompts.SYSTEM}, *history,
                    {"role": "user", "content": prompts.investigate(goal, self.ledger.select(self._visible))}]
        for round_number in range(1, self.max_rounds + 1):
            self.on_event("investigating", round=round_number)
            reply = self._chat(exchanges, "investigate", messages, tools=tools)
            if not reply["tool_calls"]:
                break
            messages.append({"role": "assistant", "content": reply["content"], "tool_calls": [
                {"type": "function", "function": {"index": index, "name": call["name"], "arguments": call["arguments"]}}
                for index, call in enumerate(reply["tool_calls"])]})
            for call in reply["tool_calls"]:
                messages.append({"role": "tool", "tool_name": str(call["name"]), "content": self._execute(call, calls)})
        answer, withheld, attempts, facts = self._compose(goal, sections, exchanges, history)
        self.on_event("answered", statements=sum(map(len, answer.values())), withheld=len(withheld))
        return {
            "status": "complete", **(self.client.metadata or {}), "prompt_version": prompts.VERSION,
            "goal": goal, "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "elapsed_seconds": perf_counter() - started, "attempts": len(attempts), "validation_problems": attempts,
            "tool_definitions": tools, "tool_definitions_sha256": sha256_json(tools),
            "exchanges": exchanges, "tool_calls": calls, "facts": facts,
            "model_draft": answer, "answer": deepcopy(answer), "withheld": withheld, "validation": VALIDATION_NOTE,
            "usage": {key: sum(exchange["usage"].get(key) or 0 for exchange in exchanges) for key in USAGE_KEYS},
        }

    def _chat(self, exchanges, phase, messages, **options):
        reply = self.client.chat(messages, **options)
        request = dict(reply["request"])
        if "tools" in request:  # stored once as tool_definitions; keep a verifiable reference here
            request["tools"] = {"sha256": sha256_json(request["tools"])}
        exchanges.append({"phase": phase, "request": request, "usage": reply["usage"],
                          "response": {"content": reply["content"], "tool_calls": reply["tool_calls"]}})
        return reply

    def _execute(self, call, calls):
        """Run one model-requested call through the registry; return the tool message text."""
        name, arguments = call["name"], call["arguments"]
        if len(calls) >= self.max_calls:
            return "ERROR budget_exhausted: the tool budget is used up; stop calling tools and answer."
        envelope = self.registry.call(name, arguments, self.context)
        if not envelope["ok"]:
            error = envelope["error"]
            calls.append({"tool": str(name)[:80], "arguments": _bounded(arguments), "error": error})
            self.on_event("tool_error", name=str(name)[:80], error=error)
            return f"ERROR {error['code']}: {error['message']}"
        call_id, repeated = self.ledger.record(envelope["tool"], envelope["arguments"], envelope["output"],
                                               origin="model")
        record = self.ledger.call(call_id)
        self._visible.extend(fact_id for fact_id in record["fact_ids"] if fact_id not in self._visible)
        calls.append({"id": call_id, "tool": envelope["tool"], "arguments": envelope["arguments"],
                      "repeated": repeated})
        self.on_event("tool", call_id=call_id, name=envelope["tool"], arguments=envelope["arguments"],
                      repeated=repeated, title=record["title"])
        note = " (already computed; same facts as before)" if repeated else ""
        return f"{call_id}: {record['title']}{note}\n" + prompts.fact_sheet(self.ledger.select(record["fact_ids"]))

    def _compose(self, goal, sections, exchanges, history):
        # A fresh, compact prompt: earlier conversation for context, then every fact the model saw.
        facts = self.ledger.select(self._visible)
        schema = answer_schema(sections, list(facts))
        messages = [{"role": "system", "content": prompts.SYSTEM}, *history,
                    {"role": "user", "content": prompts.compose(goal, sections, facts)}]
        attempts = []
        for attempt in range(2):  # one bounded repair; the snapshot is never reread
            self.on_event("composing", attempt=attempt + 1)
            reply = self._chat(exchanges, "compose", messages, format=schema, num_predict=2048)
            try:
                value = read_json(reply["content"])
            except ValueError:
                value = None
            kept, withheld, problems = validate_answer(value, facts, sections)
            attempts.append(problems)
            if not problems:
                break
            if attempt == 0:
                messages += [{"role": "assistant", "content": reply["content"]},
                             {"role": "user", "content": prompts.repair(problems)}]
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
        self.analyst, self.preload, self.memory, self.turns = analyst, list(preload), memory, []

    def ask(self, question):
        history, carried = [], []
        for turn in self.turns[-self.memory:]:
            history += [{"role": "user", "content": turn["question"]},
                        {"role": "assistant", "content": answer_text(turn["result"]["answer"])}]
            carried += cited_ids(turn["result"]["answer"])
        result = self.analyst.run(question, sections=ASK_SECTIONS, preload=[*self.preload, *carried],
                                  history=history)
        self.turns.append({"question": question, "result": result})
        return result
