"""The tool-calling analyst loop, with a scripted local model and no services."""

import json
import unittest

from loan_analyst.analyst import (ASK_SECTIONS, REPORT_SECTIONS, AnalysisError, Analyst, ChatSession, Ledger,
                                  cited_ids, route)
from loan_analyst.calculations import DEFINITIONS, build_registry
from loan_analyst.workflow import REPORT_CALLS, REPORT_TOPICS, run_calls
from loan_analyst.tools import ToolContext
from tests.fixtures import (ScriptedModel, allowed_ids, auto, broken, calls, done, grounded, make_snapshot, planned,
                            scenario, statement, statement_ids, verdicts)


def allowed_ids_of(request):
    """Evidence IDs a writer request may cite, from whichever section it has."""
    section = next(iter(request["format"]["properties"].values()))
    return section["items"]["properties"]["evidence_ids"]["items"]["enum"]


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

    def test_schema_mode_plans_with_constrained_json_instead_of_native_tool_calls(self):
        replies = [planned(("segment_performance", {"dimension": "grade"}), ("execute_sql", {"sql": "SELECT 1"})),
                   planned(), grounded]
        result = self.analyst(replies, tool_mode="schema").run("Which grade loses most?")
        first, second, compose = self.model.requests
        self.assertNotIn("tools", first)
        variants = first["format"]["properties"]["calls"]["items"]["anyOf"]
        self.assertEqual({variant["properties"]["tool"]["enum"][0] for variant in variants},
                         set(self.registry.names()))
        self.assertIn("- segment_performance:", first["messages"][-1]["content"])
        self.assertIn("[t1.grade_c] Grade C", second["messages"][-1]["content"])
        self.assertIn("ERROR unknown_tool", second["messages"][-1]["content"])
        self.assertEqual([call.get("id", call.get("error", {}).get("code")) for call in result["tool_calls"]],
                         ["t1", "unknown_tool"])
        self.assertIn("t1.grade_c", allowed_ids(compose))
        self.assertEqual([exchange["phase"] for exchange in result["exchanges"]], ["plan", "plan", "compose"])
        self.assertEqual(result["agent"], {"flow": "ask", "tool_mode": "schema", "critic": False})
        # A reply that is not a plan simply ends planning; it can never run anything.
        self.assertEqual(self.analyst([broken(), grounded], tool_mode="schema").run("Question")["tool_calls"], [])

    def test_critic_flags_trigger_one_repair_and_then_withhold(self):
        overreach = statement("Raising the cutoff will reduce future losses.", "def.retained")
        replies = [done(),
                   lambda payload: grounded(payload, findings=[overreach]),
                   lambda payload: verdicts(payload, s2="overreach"),
                   lambda payload: grounded(payload, findings=[overreach]),
                   lambda payload: verdicts(payload, s1="overreach")]
        result = self.analyst(replies, critic=True).run("Summarize", sections=REPORT_SECTIONS)
        review = self.model.requests[2]
        self.assertEqual(statement_ids(review), ["s1", "s2", "s3", "s4"])
        self.assertIn("[s2] Raising the cutoff will reduce future losses.", review["messages"][-1]["content"])
        self.assertIn("[def.retained] A score cutoff comparison", review["messages"][-1]["content"])
        self.assertIn("critic: overreach - checked", self.model.requests[3]["messages"][-1]["content"])
        # Statements accepted in the first review are not sent to the critic again.
        self.assertEqual(statement_ids(self.model.requests[4]), ["s1"])
        self.assertEqual(result["answer"]["findings"], [])
        self.assertEqual((result["withheld"][0]["item"], result["withheld"][0]["problems"]),
                         (overreach, ["critic: overreach - checked"]))
        self.assertEqual([review["verdict"] for review in result["critic_reviews"]],
                         ["supported", "overreach", "supported", "supported", "overreach"])
        self.assertEqual([exchange["phase"] for exchange in result["exchanges"]],
                         ["investigate", "compose", "critic:compose", "compose", "critic:compose"])

    def test_verbatim_statements_skip_the_critic_and_missing_verdicts_fail_closed(self):
        copied = statement(DEFINITIONS["loss_proxy"].split(". ")[0] + ".", "def.loss_proxy")
        only_copied = lambda payload: {"content": json.dumps({"answer": [copied]})}
        result = self.analyst([done(), only_copied], critic=True).run("What is the loss proxy?")
        self.assertEqual(len(self.model.requests), 2)  # nothing to review
        self.assertEqual(result["critic_reviews"], [])

        own_words = lambda payload: {"content": json.dumps({"answer": [statement("Losses are measured.",
                                                                                 "def.loss_proxy")]})}
        no_verdicts = {"content": json.dumps({"verdicts": []})}
        with self.assertRaises(AnalysisError):
            self.analyst([done(), own_words, no_verdicts, own_words, broken()], critic=True).run("Question")

    def test_report_is_written_topic_by_topic_and_assembled_by_selection(self):
        analyst = self.analyst([planned(("vintages", "vintage_trend", {"period": "quarter"}),
                                        ("tradeoff", "mix_shift", {"dimension": "term"}))],
                               tool_mode="schema", critic=True)
        self.model.default = auto
        run_calls(self.registry, self.context, self.ledger, REPORT_CALLS)
        result = analyst.run_report("Write the report", REPORT_TOPICS, preload=self.ledger.highlights("report"))

        stages = [exchange["phase"] for exchange in result["exchanges"]]
        self.assertEqual(stages[:2], ["plan", "plan"])
        for topic in REPORT_TOPICS:
            self.assertIn(f"write:{topic.name}", stages)
        self.assertEqual(stages[-2:], ["edit", "critic:edit"])

        requests = {exchange["phase"]: exchange["request"] for exchange in result["exchanges"]}
        vintage_ids, rejected_ids = allowed_ids_of(requests["write:vintages"]), allowed_ids_of(requests["write:rejected"])
        new_call = result["tool_calls"][0]["id"]
        self.assertIn(f"{new_call}.scope", vintage_ids)       # the planner's call went to its topic
        mix_call = result["tool_calls"][1]["id"]
        self.assertIn(f"{mix_call}.scope", vintage_ids)       # a tool the named topic does not own is rerouted
        self.assertIn("def.seasoning", vintage_ids)
        self.assertNotIn("def.rejected_scores", vintage_ids)  # each writer sees only its own facts
        self.assertTrue(all(ref.startswith(("def.rejected_scores", "t7.")) for ref in rejected_ids))

        statements = result["statements"]
        self.assertEqual({item["topic"] for item in statements}, {topic.name for topic in REPORT_TOPICS})
        edit = requests["edit"]["format"]["properties"]
        self.assertEqual(edit["findings"]["items"]["enum"],
                         [item["id"] for item in statements if item["section"] == "findings"])
        self.assertEqual((edit["findings"]["minItems"], edit["findings"]["maxItems"]), (2, 5))
        placed = [item for item in statements if item["placement"] == "findings"]
        self.assertEqual([item["text"] for item in placed], [item["text"] for item in result["answer"]["findings"]])
        self.assertEqual(set(result["answer"]), set(REPORT_SECTIONS))
        self.assertTrue(set(cited_ids(result["answer"])) <= set(result["facts"]))
        self.assertEqual(result["agent"]["flow"], "report")
        json.dumps(result, allow_nan=False)

    def test_editor_cannot_invent_statements_and_short_selections_are_filled_in_topic_order(self):
        def editor(payload):
            properties = payload["format"]["properties"]
            if "findings" not in properties or "properties" in properties["findings"]["items"]:
                return auto(payload)
            reply = json.loads(auto(payload)["content"])
            return {"content": json.dumps({**reply, "findings": ["invented.9"], "caveats": []})}

        analyst = self.analyst([], max_rounds=0)
        self.model.default = editor
        run_calls(self.registry, self.context, self.ledger, REPORT_CALLS)
        result = analyst.run_report("Write the report", REPORT_TOPICS, preload=self.ledger.highlights("report"))
        self.assertIn("findings: select at least 2 of the listed statement IDs", result["validation_problems"]["edit"][1])
        fallback = [item for item in result["statements"] if item.get("placed_by") == "fallback"]
        self.assertEqual(sorted(item["section"] for item in fallback), ["caveats", "findings", "findings"])
        self.assertEqual(len(result["answer"]["findings"]), 2)
        self.assertNotIn("invented", json.dumps(result["answer"]))

    def test_topics_without_data_are_skipped_and_no_findings_fails_the_report(self):
        self.analyst([], max_rounds=0)
        self.context = ToolContext(make_snapshot(dimensions=()), scenario())
        analyst = Analyst(self.model, self.registry, self.context, self.ledger, max_rounds=0)
        self.model.default = auto
        run_calls(self.registry, self.context, self.ledger, REPORT_CALLS)
        result = analyst.run_report("Write the report", REPORT_TOPICS, preload=self.ledger.highlights("report"))
        self.assertNotIn("segments", {item["topic"] for item in result["statements"]})

        failing = self.analyst([], max_rounds=0)
        self.model.default = broken
        run_calls(self.registry, self.context, self.ledger, REPORT_CALLS)
        with self.assertRaisesRegex(AnalysisError, "no report topic produced a validated finding"):
            failing.run_report("Write the report", REPORT_TOPICS, preload=self.ledger.highlights("report"))

    def test_every_topic_uses_standard_report_calls_and_registered_tools(self):
        definitions = {f"def.{key}" for key in DEFINITIONS}
        names = set(build_registry().names())
        for topic in REPORT_TOPICS:
            with self.subTest(topic=topic.name):
                self.assertTrue(all(call in REPORT_CALLS for call in topic.calls))
                self.assertTrue(set(topic.tools) <= names)
                self.assertTrue({f"def.{key}" for key in topic.definitions} <= definitions)
        self.assertEqual(route(REPORT_TOPICS, "loss_concentration", {"dimension": "grade"}), "segments")
        self.assertEqual(route(REPORT_TOPICS, "segment_performance", {"dimension": "issue_year"}), "vintages")
        self.assertEqual(route(REPORT_TOPICS, "sweep_loss_ceilings", {}), "tradeoff")
        self.assertIsNone(route(REPORT_TOPICS, "execute_sql", {}))

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
