"""The model-callable tool boundary: catalog, validation and sanitized dispatch."""

import json
import unittest

from loan_analyst.calculations import build_registry
from loan_analyst.tools import Tool, ToolContext, ToolOutput, ToolRegistry, object_schema
from tests.fixtures import make_snapshot, scenario

FORBIDDEN = {"sql", "query", "password", "host", "user", "output_dir", "path", "database", "table",
             "accepted_table", "rejected_table", "assume_comparable_scores", "rejected_loss_rate", "take_up_rate"}


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry()
        self.context = ToolContext(make_snapshot(), scenario())

    def call(self, name, arguments):
        return self.registry.call(name, arguments, self.context)

    def test_catalog_is_complete_and_exposes_no_host_controlled_settings(self):
        definitions = self.registry.definitions()
        self.assertEqual({item["function"]["name"] for item in definitions}, {
            "describe_data", "compare_score_cutoffs", "sweep_loss_ceilings", "segment_performance",
            "rejected_applications", "vintage_trend", "loss_concentration", "mix_shift"})
        for item in definitions:
            with self.subTest(tool=item["function"]["name"]):
                self.assertEqual(item["type"], "function")
                self.assertTrue(item["function"]["description"])
                parameters = item["function"]["parameters"]
                self.assertEqual(parameters["type"], "object")
                self.assertFalse(parameters["additionalProperties"])
                self.assertFalse(FORBIDDEN & parameters["properties"].keys())
        definitions[0]["function"]["name"] = "mutated"
        self.assertNotIn("mutated", {item["function"]["name"] for item in self.registry.definitions()})
        with self.assertRaises(ValueError):
            self.registry.register(Tool("describe_data", "duplicate", object_schema({}), lambda context: None))

    def test_every_tool_runs_on_a_snapshot_with_json_safe_results(self):
        examples = {"describe_data": {}, "compare_score_cutoffs": {"max_loss_rate": .25},
                    "sweep_loss_ceilings": {"ceilings": [.1, .3]}, "segment_performance": {"dimension": "grade"},
                    "rejected_applications": {"by": "application_year"}}
        for name, arguments in examples.items():
            with self.subTest(tool=name):
                response = self.call(name, arguments)
                self.assertTrue(response["ok"], response)
                output = response["output"]
                self.assertTrue(output.title and output.facts)
                self.assertTrue(set(output.highlights) <= set(output.facts))
                json.dumps({"result": output.result, "facts": output.facts}, allow_nan=False)

    def test_omitted_and_null_arguments_keep_session_settings(self):
        session = self.call("compare_score_cutoffs", {})["output"]
        nulls = self.call("compare_score_cutoffs", {"cutoffs": None, "start_month": None, "max_loss_rate": None})
        self.assertEqual(nulls["arguments"], {})
        self.assertEqual(nulls["output"].result, session.result)
        changed = self.call("compare_score_cutoffs", json.dumps({"start_month": "2015-01", "baseline": 600}))
        self.assertTrue(changed["ok"], changed)
        self.assertEqual([row["cutoff"] for row in changed["output"].result["scenarios"]], [600, 700])
        self.assertIn("2015-01", changed["output"].title)

    def test_invalid_model_arguments_fail_before_execution_without_echoing_values(self):
        invalid = [
            {"cutoffs": "600,700"}, {"cutoffs": []}, {"cutoffs": [True]}, {"cutoffs": [700.0]}, {"cutoffs": [299]},
            {"cutoffs": [851]}, {"cutoffs": [None]}, {"cutoffs": list(range(600, 631))}, {"baseline": True},
            {"baseline": "660"}, {"min_outcomes": 0}, {"min_outcomes": True}, {"max_loss_rate": True},
            {"max_loss_rate": float("nan")}, {"max_loss_rate": float("inf")}, {"max_loss_rate": -0.1},
            {"max_loss_rate": 8}, {"start_month": "2016-13"}, {"start_month": "2016-1"},
            {"start_month": "2016-01\n"}, {"end_month": 2016}, {"start_month": "2017-01", "end_month": "2016-12"},
            {"assume_comparable_scores": True}, {"rejected_loss_rate": .1}, {"take_up_rate": .5},
            {"host": "another-server"}, {"password": "must-not-echo"}, {"sql": "SELECT * FROM accepted"},
            {"output_dir": "../outside"}, {"demo": True}, [], "[]", "{", '{"baseline": 660, "baseline": 700}',
            '{"max_loss_rate": NaN}', '{"max_loss_rate": Infinity}',
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                response = self.call("compare_score_cutoffs", arguments)
                self.assertFalse(response["ok"], response)
                self.assertEqual(response["error"]["code"], "invalid_arguments")
                self.assertNotIn("must-not-echo", json.dumps(response))
        self.assertIn("fraction", self.call("compare_score_cutoffs", {"max_loss_rate": 8})["error"]["message"])
        self.assertEqual(self.call("segment_performance", {})["error"]["code"], "invalid_arguments")
        self.assertEqual(self.call("segment_performance", {"dimension": "zip_code"})["error"]["code"],
                         "invalid_arguments")

    def test_unknown_tools_unavailable_segments_and_failures_are_contained(self):
        for name in ("execute_sql", "__class__", "_tools", None, ["describe_data"]):
            response = self.call(name, {})
            self.assertEqual(response["error"]["code"], "unknown_tool")
        context = ToolContext(make_snapshot(dimensions=()), scenario())
        response = self.registry.call("segment_performance", {"dimension": "term"}, context)
        self.assertEqual(response["error"]["code"], "unavailable")

        def broken(context, **arguments):
            raise RuntimeError("raw-secret-detail")

        registry = ToolRegistry([Tool("broken", "fails", object_schema({}), broken),
                                 Tool("nan", "bad number", object_schema({}),
                                      lambda context: ToolOutput("t", {"value": float("nan")}, {}))])
        response = registry.call("broken", {}, self.context)
        self.assertEqual(response["error"]["code"], "execution_error")
        self.assertNotIn("raw-secret-detail", json.dumps(response))
        self.assertEqual(registry.call("nan", {}, self.context)["error"]["code"], "execution_error")
        with self.assertRaisesRegex(RuntimeError, "raw-secret-detail"):
            registry.call("broken", {}, self.context, sanitize=False)


if __name__ == "__main__":
    unittest.main()
