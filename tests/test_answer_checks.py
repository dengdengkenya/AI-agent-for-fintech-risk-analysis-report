"""Grounding checks for model statements and the citable fact ledger."""

import unittest

from loan_analyst.analyst import Ledger, answer_schema, check_statement, numeric_tokens, validate_answer
from loan_analyst.tools import ToolOutput

FACTS = {"t1.loss": "Loss proxy 13.80%; funded amount $34,000,792,025.",
         "t1.loans": "There are 2,260,179 retained loans.", "def.rule": "A rule without numbers."}
SECTIONS = {"answer": (1, 1), "findings": (0, 2)}


def item(text, *ids):
    return {"text": text, "evidence_ids": list(ids)}


class EvidenceTests(unittest.TestCase):
    def test_quoted_numbers_must_match_cited_facts_and_units(self):
        self.assertEqual(check_statement(item("The loss proxy is 13.8% across 2,260,179 loans.", "t1.loss",
                                              "t1.loans"), FACTS), [])
        for text in ("The loss is 14%.", "The loss is $13.80.", "The funded amount is $34 billion."):
            problems = check_statement(item(text, "t1.loss"), FACTS)
            self.assertTrue(problems and "not found in its cited facts" in problems[0], text)
        self.assertIn("quotes 2260179", check_statement(item("There are 2,260,179 loans.", "t1.loss"), FACTS)[0])
        self.assertEqual(numeric_tokens("13.80% and 13.8%"), numeric_tokens("13.8%"))

    def test_statement_structure_references_and_wording(self):
        invalid = [[], item("", "def.rule"), item("Text."), item("Text.", "unknown"),
                   item("Text.", "def.rule", "def.rule"), item("Text.", "a", "b", "c", "d", "e"),
                   {"text": "Text.", "evidence_ids": ["def.rule"], "extra": 1}, item("x" * 501, "def.rule"),
                   item("The 700 cutoff rejects more loans.", "def.rule")]
        for value in invalid:
            with self.subTest(value=value):
                self.assertTrue(check_statement(value, FACTS))

    def test_invalid_statements_are_withheld_and_limits_enforced(self):
        value = {"answer": [item("A grounded answer.", "def.rule")],
                 "findings": [item("Invented 99% loss.", "t1.loss"), item("Fine.", "def.rule"),
                              item("Also fine.", "t1.loans"), item("Over the limit.", "def.rule")]}
        kept, withheld, problems = validate_answer(value, FACTS, SECTIONS)
        self.assertEqual([entry["text"] for entry in kept["findings"]], ["Fine.", "Also fine."])
        self.assertEqual([entry["section"] for entry in withheld], ["findings", "findings"])
        self.assertTrue(any("findings[0]" in problem for problem in problems))
        self.assertTrue(any("at most 2" in problem for problem in problems))
        self.assertEqual(validate_answer([], FACTS, SECTIONS)[0], None)
        kept, _, problems = validate_answer({"findings": []}, FACTS, SECTIONS)
        self.assertEqual(kept["answer"], [])
        self.assertIn("answer needs at least 1 item", problems)

    def test_ledger_numbers_calls_and_deduplicates_repeats(self):
        ledger = Ledger({"rule": "A rule."})
        output = ToolOutput("Title", {"value": 1}, {"a": "Fact A.", "b": "Fact B."}, ("a",))
        self.assertEqual(ledger.record("tool", {"x": 1}, output, origin="report"), ("t1", False))
        self.assertEqual(ledger.record("tool", {"x": 1}, output, origin="model"), ("t1", True))
        self.assertEqual(ledger.record("tool", {"x": 2}, output, origin="model"), ("t2", False))
        self.assertEqual(list(ledger.facts), ["def.rule", "t1.a", "t1.b", "t2.a", "t2.b"])
        self.assertEqual(ledger.highlights("report"), ["t1.a"])
        self.assertEqual(ledger.call("t2")["origin"], "model")

    def test_structured_output_schema_limits_evidence_to_known_ids(self):
        schema = answer_schema(SECTIONS, ["def.rule", "t1.loss"])
        findings = schema["properties"]["findings"]
        self.assertEqual((findings["minItems"], findings["maxItems"]), (0, 2))
        self.assertEqual(findings["items"]["properties"]["evidence_ids"]["items"]["enum"], ["def.rule", "t1.loss"])
        self.assertEqual(schema["required"], ["answer", "findings"])


if __name__ == "__main__":
    unittest.main()
