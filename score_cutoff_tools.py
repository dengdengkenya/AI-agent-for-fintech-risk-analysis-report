"""JSON-callable report tools for a future local LLM host.

The host executes calls; the model only chooses a registered name and JSON arguments.
This module does not contact an LLM, write reports, or accept arbitrary SQL.
The existing score_cutoff_simulation.py remains the source of calculation logic.
"""

import argparse
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace

from import_csv_to_mysql import quote_identifier
import score_cutoff_simulation as simulation
import score_cutoff_sql as database_sql


DICTIONARY_PATH = Path(__file__).resolve().with_name("score_cutoff_tool_dictionary.json")
_DICTIONARY = json.loads(DICTIONARY_PATH.read_text(encoding="utf-8"))
_TOOLS = {tool["function"]["name"]: tool["function"] for tool in _DICTIONARY["tools"]}


class ArgumentError(ValueError):
    """A tool request failed validation before database access."""


class ConfigurationError(RuntimeError):
    """The trusted host has not supplied the required database configuration."""


@dataclass(frozen=True)
class DatabaseConfig:
    """Trusted host settings; never include these in model-controlled arguments."""

    host: str = "localhost"
    port: int = 3306
    user: str = "root"
    database: str = "demo"
    accepted_table: str = "accepted"
    rejected_table: str = "rejected"
    query_timeout: int = 600

    def __post_init__(self):
        for field in ("host", "user", "database", "accepted_table", "rejected_table"):
            if not isinstance(getattr(self, field), str) or not getattr(self, field):
                raise ValueError(f"{field} must be a nonempty string")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("port must be an integer from 1 to 65535")
        if type(self.query_timeout) is not int or self.query_timeout < 1:
            raise ValueError("query_timeout must be a positive integer")
        for field in ("database", "accepted_table", "rejected_table"):
            quote_identifier(getattr(self, field))
        if self.accepted_table.casefold() == self.rejected_table.casefold():
            raise ValueError("Accepted and rejected tables must differ")


def load_tool_dictionary():
    """Return an independent copy of the catalog, definitions and goal guidance."""
    return deepcopy(_DICTIONARY)


def get_tool_definitions():
    """Return just the function schemas suitable for an Ollama chat tools list."""
    return deepcopy(_DICTIONARY["tools"])


def _validate_value(name, value, schema):
    # Implements the schema keywords used by this catalog, without a new dependency.
    types = schema["type"]
    types = [types] if isinstance(types, str) else types
    if value is None:
        if "null" in types:
            return
        raise ArgumentError(f"{name} must not be null")
    expected = next(kind for kind in types if kind != "null")
    valid = {
        "integer": type(value) is int,
        "number": type(value) in (int, float),
        "boolean": type(value) is bool,
        "string": isinstance(value, str),
        "array": isinstance(value, list),
    }[expected]
    if not valid:
        raise ArgumentError(f"{name} must be {expected}")
    if expected in ("integer", "number"):
        if isinstance(value, float) and not math.isfinite(value):
            raise ArgumentError(f"{name} must be finite")
        if "minimum" in schema and value < schema["minimum"]:
            raise ArgumentError(f"{name} is below its minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ArgumentError(f"{name} is above its maximum")
    if expected == "string":
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ArgumentError(f"{name} exceeds its maximum length")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise ArgumentError(f"{name} must use YYYY-MM with a valid month")
    if expected == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", math.inf):
            raise ArgumentError(f"{name} has an invalid number of items")
        for item in value:
            _validate_value(f"{name} item", item, schema["items"])


def _validated_arguments(name, arguments):
    if not isinstance(arguments, dict):
        raise ArgumentError("Tool arguments must be a JSON object")
    schema = _TOOLS[name]["parameters"]
    properties = schema["properties"]
    if arguments.keys() - properties.keys():
        raise ArgumentError("Unknown argument; use only fields in this tool's parameters schema")
    result = {}
    for field, definition in properties.items():
        if field not in arguments and "default" not in definition:
            if field in schema.get("required", []):
                raise ArgumentError(f"Missing required argument: {field}")
            continue
        value = arguments.get(field, deepcopy(definition.get("default")))
        _validate_value(field, value, definition)
        result[field] = value
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ArgumentError("Duplicate JSON argument keys are not allowed")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ArgumentError("Nonfinite JSON numbers are not allowed")


class ScoreCutoffTools:
    """Allowlisted operations bound to host-controlled sources and credentials.

    connection_factory, if supplied by the host, takes DatabaseConfig and must
    return a fresh connection configured with a query timeout. Calls always close it.
    It is useful for tests or applications with their own credential management.
    """

    def __init__(self, config=None, *, demo=False, connection_factory=None):
        self.config = config if config is not None else DatabaseConfig()
        if not isinstance(self.config, DatabaseConfig) or type(demo) is not bool:
            raise ValueError("Expected DatabaseConfig and a boolean demo flag")
        self.demo = demo
        self.connection_factory = connection_factory
        self._handlers = {
            "inspect_sources": self.inspect_sources,
            "preview_score_cutoff_queries": self.preview_score_cutoff_queries,
            "get_score_cutoff_aggregates": self.get_score_cutoff_aggregates,
            "compare_score_cutoffs": self.compare_score_cutoffs,
        }

    def _options(self, name, arguments):
        settings = _validated_arguments(name, arguments)
        if "cutoffs" in settings:
            settings["cutoffs"] = sorted(set(settings["cutoffs"]) | {settings["baseline"]})
            if len(settings["cutoffs"]) > 30:
                raise ArgumentError("At most 30 cutoffs are allowed, including baseline")
            start, end = settings["start_month"], settings["end_month"]
            if start and end and start > end:
                raise ArgumentError("start_month must not be after end_month")
            if self.demo and name != "preview_score_cutoff_queries" and (start or end):
                raise ArgumentError("Synthetic fixtures have no monthly detail; omit date filters")
        if name == "compare_score_cutoffs":
            if settings["assume_comparable_scores"] != (settings["rejected_loss_rate"] is not None):
                raise ArgumentError("Supply assume_comparable_scores=true and rejected_loss_rate together")
            if settings["take_up_rate"] != 1 and not settings["assume_comparable_scores"]:
                raise ArgumentError("take_up_rate requires explicit rejected-lending assumptions")
        return SimpleNamespace(**asdict(self.config), **settings, demo=self.demo), settings

    def _connect(self):
        if self.connection_factory is not None:
            return self.connection_factory(self.config)
        password = os.environ.get("MYSQL_PASSWORD")
        if password is None:
            raise ConfigurationError("Set MYSQL_PASSWORD in the local host environment before live calls")
        return database_sql.connect_mysql(self.config, password)

    def _aggregates(self, options):
        if self.demo:
            return simulation.demo_aggregates(options)
        with closing(self._connect()) as connection:
            return database_sql.fetch_aggregates(connection, options)

    def _result(self, settings, **values):
        return {
            "mode": "synthetic_demo" if self.demo else "historical_mysql",
            "settings": settings,
            **values,
            "limitations": deepcopy(_DICTIONARY["interpretation_rules"]),
        }

    def inspect_sources(self, **arguments):
        """Inspect required columns and engines without scanning loan records."""
        self._options("inspect_sources", arguments)
        if self.demo:
            raise ArgumentError("Schema inspection requires live mode; demo fixtures have no database schema")
        with closing(self._connect()) as connection:
            sources = database_sql.inspect_sources(connection, self.config)
        return {"mode": "historical_mysql", "schema_checked": True, "sources": sources}

    def preview_score_cutoff_queries(self, **arguments):
        """Return parameterized query templates without database access."""
        options, settings = self._options("preview_score_cutoff_queries", arguments)
        queries = {}
        for kind in ("accepted", "rejected"):
            sql, params = database_sql.build_query(kind, options)
            queries[kind] = {"sql": sql, "params": list(params)}
        return {"mode": "query_preview", "settings": settings, "queries": queries}

    def get_score_cutoff_aggregates(self, **arguments):
        """Retrieve both sources in one read-only snapshot; return disjoint bands."""
        options, settings = self._options("get_score_cutoff_aggregates", arguments)
        return self._result(settings, aggregates=self._aggregates(options))

    def compare_score_cutoffs(self, **arguments):
        """Fetch aggregate evidence and compute cumulative scenarios deterministically."""
        options, settings = self._options("compare_score_cutoffs", arguments)
        results = simulation.analyze(self._aggregates(options), options)
        return self._result(settings, results=results)

    def call_tool(self, name, arguments):
        """Dispatch a model request, returning a JSON-safe success/error envelope."""
        if not isinstance(name, str) or name not in self._handlers:
            return {"ok": False, "error": {"code": "unknown_tool", "message": "Unknown tool name"}}
        try:
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments, object_pairs_hook=_unique_object,
                                           parse_constant=_reject_constant)
                except (ValueError, RecursionError) as error:
                    raise ArgumentError("Arguments must contain a valid JSON object with unique keys") from error
            checked = _validated_arguments(name, arguments)
            result = self._handlers[name](**checked)
            # Reject NaN/Infinity before a result can enter an LLM conversation.
            json.dumps(result, allow_nan=False)
            return {"ok": True, "result": result}
        except ArgumentError as error:
            return {"ok": False, "error": {"code": "invalid_arguments", "message": str(error)}}
        except ConfigurationError as error:
            return {"ok": False, "error": {"code": "configuration_error", "message": str(error)}}
        except Exception as error:
            # Connector failures can quote source text or credentials. Never relay them.
            code = "database_error" if type(error).__module__.startswith("mysql") else "execution_error"
            return {"ok": False, "error": {"code": code, "message": (
                "Operation failed. Check the local database, required schema and query timeout; "
                "raw exception details are not returned to the model.")}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list-tools", action="store_true", help="Print the full tool dictionary")
    action.add_argument("--call", choices=tuple(_TOOLS), help="Execute one registered tool")
    parser.add_argument("--arguments", default="{}", help="JSON arguments, or '-' to read from stdin")
    parser.add_argument("--demo", action="store_true", help="Use synthetic fixtures, with no MySQL calls")
    args = parser.parse_args(argv)
    if args.list_tools:
        print(json.dumps(load_tool_dictionary(), indent=2, allow_nan=False))
        return 0
    arguments = sys.stdin.read() if args.arguments == "-" else args.arguments
    response = ScoreCutoffTools(demo=args.demo).call_tool(args.call, arguments)
    print(json.dumps(response, indent=2, allow_nan=False))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
