"""The allowlisted tools the local model may call.

A Tool pairs a JSON schema with a Python handler. The model can only ask for a
registered name and JSON arguments; this file checks the arguments before any
calculation runs and turns every failure into a safe JSON envelope.

Tools only read the in-memory Snapshot. They cannot reach MySQL, and they never
accept SQL, credentials, table names, paths or expansion assumptions.
"""

from copy import deepcopy
from collections.abc import Callable
from dataclasses import dataclass
import json
import math
import re

from .settings import Scenario, SettingsError
from .snapshot import Snapshot
from .support import read_json


class ArgumentError(ValueError):
    """A tool request failed validation before execution."""


class ToolUnavailable(LookupError):
    """The tool cannot run on this snapshot, for example because a column is absent."""


@dataclass(frozen=True)
class ToolOutput:
    """One calculation, prepared for two readers.

    ``result`` is structured data used by report tables. ``facts`` are short
    sentences the model may quote, keyed by local names such as "coverage".
    Ledger.record() adds a call prefix, so "coverage" becomes "t1.coverage".
    """

    title: str
    result: dict
    facts: dict[str, str]
    highlights: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolContext:
    """Host-controlled inputs shared by every call: the snapshot and the scenario.

    The model never supplies these. It can only override the small set of
    arguments that each tool's schema exposes.
    """

    snapshot: Snapshot
    scenario: Scenario


@dataclass(frozen=True)
class Tool:
    """A JSON schema plus the Python handler that implements one tool."""

    name: str
    description: str
    parameters: dict
    handler: Callable[..., ToolOutput]  # called as handler(context, **arguments)

    def definition(self):
        """The function description in the shape Ollama's chat API expects."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": deepcopy(self.parameters),
            },
        }


def object_schema(properties, required=()):
    """A small helper for the object schemas used by every tool."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _allowed_types(schema):
    kind = schema["type"]
    return kind if isinstance(kind, list) else [kind]


def _is_expected_type(value, expected):
    """Check one JSON type without accepting look-alikes such as True for 1."""
    if expected == "integer":
        return type(value) is int
    if expected == "number":
        return type(value) in (int, float)
    if expected == "boolean":
        return type(value) is bool
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    return False


def _validate_number(name, value, schema):
    if isinstance(value, float) and not math.isfinite(value):
        raise ArgumentError(f"{name} must be finite")
    if "minimum" in schema and value < schema["minimum"]:
        raise ArgumentError(f"{name} must be at least {schema['minimum']}")
    if "maximum" in schema and value > schema["maximum"]:
        hint = " (use a fraction: 0.08 means 8%)" if schema["maximum"] == 1 else ""
        raise ArgumentError(f"{name} must be at most {schema['maximum']}{hint}")


def _validate_string(name, value, schema):
    if "enum" in schema and value not in schema["enum"]:
        raise ArgumentError(f"{name} must be one of: {', '.join(schema['enum'])}")
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        raise ArgumentError(f"{name} exceeds its maximum length")
    if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
        raise ArgumentError(f"{name} must use YYYY-MM with a valid month")


def _validate_array(name, value, schema):
    minimum = schema.get("minItems", 0)
    maximum = schema.get("maxItems")
    if len(value) < minimum or (maximum is not None and len(value) > maximum):
        shown_maximum = maximum if maximum is not None else "any number of"
        raise ArgumentError(f"{name} must have {minimum} to {shown_maximum} items")
    for item in value:
        _validate_value(f"{name} item", item, schema["items"])


def _validate_value(name, value, schema):
    """Validate one value against the small JSON-schema subset the tools use."""
    types = _allowed_types(schema)
    if value is None:
        if "null" in types:
            return
        raise ArgumentError(f"{name} must not be null")

    expected = None
    for kind in types:
        if kind != "null":
            expected = kind
            break

    if not _is_expected_type(value, expected):
        article = "an" if expected[0] in "aeiou" else "a"
        raise ArgumentError(f"{name} must be {article} {expected}")

    if expected in ("integer", "number"):
        _validate_number(name, value, schema)
    elif expected == "string":
        _validate_string(name, value, schema)
    elif expected == "array":
        _validate_array(name, value, schema)


def validate_arguments(schema, arguments):
    """Return only the arguments the caller supplied and that passed validation."""
    if not isinstance(arguments, dict):
        raise ArgumentError("Tool arguments must be a JSON object")

    properties = schema["properties"]
    unknown = arguments.keys() - properties.keys()
    if unknown:
        raise ArgumentError("Unknown argument. Allowed: " + (", ".join(properties) or "none"))

    checked = {}
    for name, value in arguments.items():
        if value is None:
            # Small models often send null for every optional field. Treat that
            # as "use the session setting" instead of rejecting the whole call.
            continue
        _validate_value(name, value, properties[name])
        checked[name] = value

    missing = [name for name in schema.get("required", ()) if name not in checked]
    if missing:
        raise ArgumentError("Missing required argument: " + ", ".join(missing))
    return checked


def parse_arguments(text):
    """Parse the JSON string a model supplied for a tool call."""
    try:
        return read_json(text)
    except (ValueError, RecursionError):
        raise ArgumentError("Arguments must be a valid JSON object with unique keys") from None


def _error(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}


class ToolRegistry:
    """Allowlisted tools by name. This is the only path from the model to our code."""

    def __init__(self, tools=()):
        self._tools = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool):
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def names(self):
        return tuple(self._tools)

    def definitions(self):
        """Function schemas in the shape Ollama's chat API expects in ``tools``."""
        return [tool.definition() for tool in self._tools.values()]

    def call(self, name, arguments, context, *, sanitize=True):
        """Validate and run one tool, returning either a result or a safe error envelope.

        Host-chosen calls pass sanitize=False so an unexpected programming error
        is raised locally instead of being hidden behind a model-facing message.
        """
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return _error("unknown_tool", "Unknown tool. Available tools: " + ", ".join(self._tools))

        try:
            if isinstance(arguments, str):
                arguments = parse_arguments(arguments)
            checked = validate_arguments(tool.parameters, {} if arguments is None else arguments)
            output = tool.handler(context, **checked)
            # Refuse NaN/Infinity before a result can enter a conversation or evidence file.
            json.dumps({"result": output.result, "facts": output.facts}, allow_nan=False)
            return {"ok": True, "tool": name, "arguments": checked, "output": output}
        except (ArgumentError, SettingsError) as error:
            return _error("invalid_arguments", str(error))
        except ToolUnavailable as error:
            return _error("unavailable", str(error))
        except Exception:
            if not sanitize:
                raise
            # Raw exception text can quote internal values; never relay it to the model.
            return _error("execution_error",
                          "The tool failed unexpectedly; raw details are not returned.")
