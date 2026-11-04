"""Citable fact ledger and validation of model statements.

Facts are deterministic sentences written by tools or definitions. A model statement
must cite facts by ID, and every number it quotes must appear, with its unit, in the
cited facts. These are grounding checks, not proof that an interpretation is right.
"""

from decimal import Decimal
import re

from .jsonutil import canonical


class AnalysisError(RuntimeError):
    """The model could not produce a validated answer; safe to show to the user."""


_NUMERIC_TOKEN = re.compile(
    r"(?<![\w.])(?P<currency>\$)?[+-]?(?P<number>[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)(?P<percent>%)?")
_DECISIONS = re.compile(r"\b(accepts|rejects|approves|denies)\b", re.IGNORECASE)


def numeric_tokens(text):
    """Numeric magnitudes with units: 13.8% matches 13.80%, but 14% or $13.80 do not."""
    return {(Decimal(match["number"].replace(",", "")),
             "USD" if match["currency"] else "%" if match["percent"] else "number")
            for match in _NUMERIC_TOKEN.finditer(text)}


def _show(token):
    value, unit = token
    return f"${value:f}" if unit == "USD" else f"{value:f}%" if unit == "%" else f"{value:f}"


class Ledger:
    """Citable facts by ID (def.<key> or t<n>.<key>) and the tool calls that produced them."""

    def __init__(self, definitions=None):
        self.facts, self.calls, self._signatures = {}, [], {}
        self.definition_ids = [f"def.{key}" for key in (definitions or {})]
        self.facts.update(zip(self.definition_ids, (definitions or {}).values()))

    def record(self, tool, arguments, output, *, origin):
        """Store a successful call once; an identical repeat returns (first ID, True)."""
        signature = (tool, canonical(arguments))
        if signature in self._signatures:
            return self._signatures[signature], True
        call_id = f"t{len(self.calls) + 1}"
        fact_ids = [f"{call_id}.{key}" for key in output.facts]
        self.facts.update(zip(fact_ids, output.facts.values()))
        self.calls.append({"id": call_id, "tool": tool, "arguments": arguments, "origin": origin,
                           "title": output.title, "fact_ids": fact_ids,
                           "highlights": [f"{call_id}.{key}" for key in output.highlights if key in output.facts],
                           "result": output.result})
        self._signatures[signature] = call_id
        return call_id, False

    def call(self, call_id):
        return next(call for call in self.calls if call["id"] == call_id)

    def select(self, ids):
        return {fact_id: self.facts[fact_id] for fact_id in ids}

    def highlights(self, origin="report"):
        return [fact_id for call in self.calls if call["origin"] == origin for fact_id in call["highlights"]]


def answer_schema(sections, evidence_ids):
    """Structured-output schema; evidence IDs are restricted to facts the model has seen."""
    # Select the source before drafting the claim in constrained generation.
    item = {"type": "object", "additionalProperties": False, "required": ["evidence_ids", "text"],
            "properties": {"evidence_ids": {"type": "array", "minItems": 1, "maxItems": 4,
                                            "items": {"type": "string", "enum": list(evidence_ids)}},
                           "text": {"type": "string", "minLength": 1, "maxLength": 500}}}
    return {"type": "object", "additionalProperties": False, "required": list(sections),
            "properties": {name: {"type": "array", "minItems": low, "maxItems": high, "items": item}
                           for name, (low, high) in sections.items()}}


def check_statement(item, facts):
    """Problems with one statement; an empty list means it may be shown."""
    if not isinstance(item, dict) or set(item) != {"text", "evidence_ids"}:
        return ["each item needs exactly text and evidence_ids"]
    text, references = item["text"], item["evidence_ids"]
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 500:
        return ["text must be a short nonempty paragraph"]
    if (not isinstance(references, list) or not 1 <= len(references) <= 4
            or any(not isinstance(ref, str) for ref in references) or len(set(references)) != len(references)):
        return ["cite one to four distinct evidence IDs"]
    unknown = [ref for ref in references if ref not in facts]
    if unknown:
        return ["cites unknown evidence " + ", ".join(ref[:40] for ref in unknown[:3])]
    problems = []
    if _DECISIONS.search(text):
        problems.append("describe retained historical loans, not accept/reject/approve/deny decisions")
    unsupported = numeric_tokens(text) - numeric_tokens(" ".join(facts[ref] for ref in references))
    if unsupported:
        problems.append("quotes " + ", ".join(sorted(map(_show, unsupported))) + " not found in its cited facts")
    return problems


def validate_answer(value, facts, sections):
    """Return (kept, withheld, problems); kept holds only statements that pass every check."""
    if not isinstance(value, dict):
        return None, [], ["the response must be a JSON object with the required sections"]
    problems = [f"unexpected section {str(name)[:40]}" for name in value if name not in sections]
    kept, withheld = {}, []
    for name, (low, high) in sections.items():
        items = value.get(name, [])
        if not isinstance(items, list):
            problems.append(f"{name} must be a list")
            items = []
        kept[name] = []
        for index, item in enumerate(items):
            issues = check_statement(item, facts)
            if not issues and len(kept[name]) >= high:
                issues = [f"{name} allows at most {high} items"]
            if issues:
                problems.append(f"{name}[{index}]: " + "; ".join(issues))
                withheld.append({"section": name, "item": item, "problems": issues})
            else:
                kept[name].append(item)
        if len(items) < low:
            problems.append(f"{name} needs at least {low} item{'' if low == 1 else 's'}")
    return kept, withheld, problems
