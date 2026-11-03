"""The tool-calling analyst loop, with a scripted local model and no services."""

import json
import unittest

from loan_analyst.agent import ASK_SECTIONS, REPORT_SECTIONS, Analyst, ChatSession, cited_ids
from loan_analyst.analyses import DEFINITIONS, build_registry
from loan_analyst.evidence import AnalysisError, Ledger
from loan_analyst.pipeline import REPORT_CALLS, run_calls
from loan_analyst.tools import ToolContext
from tests.fixtures import ScriptedModel, allowed_ids, calls, done, grounded, make_snapshot, scenario, statement


class AnalystTests(unittest.TestCase):
    def analyst(self, replies, **limits):
        self.model = ScriptedModel(replies)
        self.model.preflight()
        self.registry, self.ledger = build_registry(), Ledger(DEFINITIONS)
        self.context = ToolContext(make_snapshot(), scenario())
        return Analyst(self.model, self.registry, self.context, self.ledger, **limits)

    def test_model_tool_calls_run_and_their_facts_become_citable(self):
        def answer(payload):
            self.assertIn("t1.score_band_600_699", allowed_ids(payload))
            return grounded(payload, answer=[statement("The 600-699 band has a 40.00% principal-loss proxy.",
                                                       "t1.score_band_600_699")])

        analyst = self.analyst([calls(("segment_performance", {"dimension": "score_band"})), done(), answer])
        result = analyst.run("Which score band has the highest losses?")
        self.assertEqual(result["answer"]["answer"][0]["evidence_ids"], ["t1.score_band_600_699"])
        self.assertEqual(result["tool_calls"], [{"id": "t1", "tool": "segment_performance",
                                                 "arguments": {"dimension": "score_band"}, "repeated": False}])
        self.assertEqual(self.ledger.call("t1")["origin"], "model")
        investigate, after_tool, compose = self.model.requests
        self.assertIn("tools", investigate)
        self.assertNotIn("format", investigate)
        tool_message = after_tool["messages"][-1]
        self.assertEqual((tool_message["role"], tool_message["tool_name"]), ("tool", "segment_performance"))
        self.assertIn("[t1.score_band_600_699] Score band 600-699", tool_message["content"])
        self.assertEqual(after_tool["messages"][-2]["tool_calls"][0]["function"]["name"], "segment_performance")
        self.assertIn("format", compose)
        self.assertNotIn("tools", compose)
        self.assertEqual(result["model_draft"], result["answer"])
        self.assertEqual(result["usage"]["eval_count"], 60)
        self.assertEqual([exchange["phase"] for exchange in result["exchanges"]],
                         ["investigate", "investigate", "compose"])
        self.assertEqual(result["exchanges"][0]["request"]["tools"], {"sha256": result["tool_definitions_sha256"]})
        json.dumps(result, allow_nan=False)

    def test_invalid_repeated_and_excess_calls_are_bounded(self):
        replies = [calls(("compare_score_cutoffs", {"max_loss_rate": 8}), ("describe_data", {}),
                         ("describe_data", {})),
                   calls(("execute_sql", {"sql": "DROP TABLE accepted"}), ("describe_data", {"end_month": "2015-12"})),
                   calls(("describe_data", {})), grounded]
        result = self.analyst(replies, max_rounds=3, max_calls=4).run("Is the data complete?")
        records = result["tool_calls"]
        self.assertEqual(records[0]["error"]["code"], "invalid_arguments")
        self.assertEqual((records[1]["id"], records[2]["id"], records[2]["repeated"]), ("t1", "t1", True))
        self.assertEqual(records[3]["error"]["code"], "unknown_tool")
        self.assertEqual(len(records), 4)  # later requests exceeded the budget and never ran
        self.assertEqual(len(self.ledger.calls), 1)
        self.assertEqual(len(self.model.requests), 4)  # three tool rounds at most, then the answer
        round_three = self.model.requests[2]["messages"]
        self.assertIn("fraction", round_three[3]["content"])
        self.assertTrue(round_three[-2]["content"].startswith("ERROR unknown_tool"))
        self.assertTrue(round_three[-1]["content"].startswith("ERROR budget_exhausted"))
        self.assertIn("format", self.model.requests[-1])

    def test_one_repair_then_unsupported_statements_are_withheld(self):
        bad_number = statement("Raising the cutoff cuts losses by 99%.", "def.retained")
        replies = [done(),
                   lambda payload: grounded(payload, findings=[bad_number]),
                   lambda payload: grounded(payload, findings=[
                       bad_number, statement("Losses use only resolved loans.", "def.loss_proxy")])]
        result = self.analyst(replies).run("Summarize", sections=REPORT_SECTIONS)
        self.assertEqual(result["attempts"], 2)
        repair = self.model.requests[2]["messages"][-1]["content"]
        self.assertIn("quotes 99% not found in its cited facts", repair)
        self.assertEqual([item["text"] for item in result["answer"]["findings"]], ["Losses use only resolved loans."])
        self.assertEqual(result["withheld"][0]["item"], bad_number)
        self.assertTrue(result["validation_problems"][1])

    def test_answer_that_cannot_be_grounded_fails_the_run(self):
        for final in ({"content": "not json"},
                      lambda payload: grounded(payload, answer=[statement("Approves 5 loans.", "def.retained")])):
            with self.subTest(final=final), self.assertRaises(AnalysisError):
                self.analyst([done(), final, final]).run("Question")

    def test_model_cannot_cite_facts_it_was_never_shown(self):
        def cite_hidden(payload):
            return grounded(payload, findings=[statement("A detail.", "t2.accepted_600")])

        analyst = self.analyst([done(), cite_hidden, cite_hidden])
        run_calls(self.registry, self.context, self.ledger, REPORT_CALLS)
        result = analyst.run("Report", preload=self.ledger.highlights("report"))
        self.assertIn("t2.accepted_700", result["facts"])  # baseline fact is a preloaded highlight
        self.assertNotIn("t2.accepted_600", result["facts"])
        self.assertIn("unknown evidence t2.accepted_600", result["withheld"][0]["problems"][0])

    def test_zero_tool_rounds_only_interpret_preloaded_facts(self):
        result = self.analyst([grounded], max_rounds=0).run("Question")
        self.assertEqual(len(self.model.requests), 1)
        self.assertEqual(result["tool_calls"], [])

    def test_chat_sessions_carry_recent_answers_and_their_evidence(self):
        analyst = self.analyst([calls(("describe_data", {})),
                                lambda payload: grounded(payload, answer=[statement("Data overview.", "t1.snapshot")]),
                                done(), grounded], max_rounds=1)
        session = ChatSession(analyst)
        first = session.ask("What data is loaded?")
        second = session.ask("And the loss rule?")
        for request in self.model.requests[2:]:  # the follow-up's tool round and its answer both see history
            self.assertEqual([message["role"] for message in request["messages"]],
                             ["system", "user", "assistant", "user"])
        self.assertIn("evidence: t1.snapshot", self.model.requests[2]["messages"][2]["content"])
        self.assertIn("t1.snapshot", second["facts"])
        self.assertEqual(cited_ids(first["answer"])[0], "t1.snapshot")
        self.assertEqual(ASK_SECTIONS["answer"], (1, 1))


if __name__ == "__main__":
    unittest.main()
