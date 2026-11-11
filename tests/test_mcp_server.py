"""The MCP stdio server, driven through in-memory streams. No client, network or MySQL."""

import io
import json
import unittest

from loan_analyst import cli, mcp_server
from loan_analyst.calculations import build_registry
from loan_analyst.snapshot import save_snapshot
from loan_analyst.tools import ToolContext
from tests.fixtures import make_snapshot, scenario, services

ARGS = ["--cutoffs", "600,700", "--baseline", "700", "--min-outcomes", "1", "--start-month", "2013-01"]


def request(number, method, params=None):
    message = {"jsonrpc": "2.0", "id": number, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message)


class McpServerTests(unittest.TestCase):
    def exchange(self, *lines):
        reader, writer = io.StringIO("\n".join(lines) + "\n"), io.StringIO()
        mcp_server.serve(build_registry(), ToolContext(make_snapshot(), scenario()), reader, writer)
        return [json.loads(line) for line in writer.getvalue().splitlines()]

    def test_handshake_lists_read_only_tools_and_ignores_notifications(self):
        replies = self.exchange(
            request(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "test", "version": "0"}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            "",
            request(2, "ping"),
            request(3, "tools/list"))
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3])  # the notification got no reply
        started = replies[0]["result"]
        self.assertEqual((started["protocolVersion"], started["capabilities"]),
                         ("2025-06-18", {"tools": {"listChanged": False}}))
        self.assertEqual(replies[1]["result"], {})
        tools = replies[2]["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], list(build_registry().names()))
        for tool in tools:
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
            self.assertTrue(tool["annotations"]["readOnlyHint"])
        newer = self.exchange(request(1, "initialize", {"protocolVersion": "2099-01-01"}))
        self.assertEqual(newer[0]["result"]["protocolVersion"], mcp_server.PROTOCOL_VERSIONS[0])

    def test_tool_calls_return_the_same_facts_as_the_registry(self):
        replies = self.exchange(request(1, "tools/call", {"name": "segment_performance",
                                                          "arguments": {"dimension": "grade"}}))
        result = replies[0]["result"]
        expected = build_registry().call("segment_performance", {"dimension": "grade"},
                                         ToolContext(make_snapshot(), scenario()))["output"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["facts"], expected.facts)
        self.assertEqual(result["structuredContent"]["result"], expected.result)
        self.assertIn("[grade_c] Grade C: 3 loans", result["content"][0]["text"])

    def test_bad_requests_get_sanitized_errors_and_never_run_anything(self):
        replies = self.exchange(
            request(1, "tools/call", {"name": "compare_score_cutoffs", "arguments": {"sql": "DROP TABLE x"}}),
            request(2, "tools/call", {"name": "execute_sql", "arguments": {}}),
            request(3, "tools/call", {"arguments": {}}),
            request(4, "resources/list"),
            "{not json",
            json.dumps([{"jsonrpc": "2.0", "id": 5, "method": "ping"}]),
            request(6, "tools/call", {"name": "mix_shift", "arguments": {"dimension": "grade"}}))
        self.assertTrue(replies[0]["result"]["isError"])
        self.assertIn("invalid_arguments", replies[0]["result"]["content"][0]["text"])
        self.assertNotIn("DROP", json.dumps(replies[0]))
        self.assertEqual([reply["error"]["code"] for reply in replies[1:6]],
                         [mcp_server.INVALID_PARAMS, mcp_server.INVALID_PARAMS, mcp_server.METHOD_NOT_FOUND,
                          mcp_server.PARSE_ERROR, mcp_server.INVALID_REQUEST])
        self.assertEqual((replies[4]["id"], replies[5]["id"]), (None, None))
        self.assertFalse(replies[6]["result"]["isError"])  # the server keeps working after errors

    def test_command_requires_a_saved_snapshot_and_keeps_stdout_for_the_protocol(self):
        wired = services(self)
        self.assertEqual(cli.main(["mcp", *ARGS], wired), 1)
        self.assertEqual(wired.printed, [])

        path = save_snapshot(make_snapshot(), wired.snapshot_dir)
        options = cli._options(cli.build_parser(), cli.build_parser().parse_args(
            ["mcp", *ARGS, "--snapshot", str(path), "--max-loss-rate", "0.25"]))
        reader = io.StringIO(request(1, "tools/call", {"name": "compare_score_cutoffs", "arguments": {}}) + "\n")
        writer = io.StringIO()
        self.assertEqual(mcp_server.run_mcp(options, wired, reader, writer), 0)
        reply = json.loads(writer.getvalue())
        self.assertEqual(reply["result"]["structuredContent"]["result"]["candidate_cutoff"], 600)
        self.assertEqual(wired.printed, [])
        self.assertTrue(any("MySQL is not queried" in line for line in wired.logged))


if __name__ == "__main__":
    unittest.main()
