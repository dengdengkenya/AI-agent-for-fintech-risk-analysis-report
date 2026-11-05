"""Model-callable tool registry: one definition per tool, validated before execution.

A Tool pairs a JSON schema with a host handler. The model chooses a registered name and
JSON arguments; the host validates them and runs the handler over the in-memory
snapshot, returning citable facts. No tool accepts SQL, credentials, table names or
paths, and no tool can reach MySQL: the snapshot was read before the analyst starts
requesting tools.
"""

from copy import deepcopy
from collections.abc import Callable
from dataclasses import dataclass
import json
import math
import re

from .jsonutil import read_json
from .cube import Snapshot
from .settings import Scenario, SettingsError


class ArgumentError(ValueError):
    """A tool request failed validation before execution."""


class ToolUnavailable(LookupError):
    """The tool cannot run on this snapshot, for example an optional column is absent."""


@dataclass(frozen=True)
class ToolOutput:
    """One calculation, presented for two readers.

    Report renderers use the structured result. The model sees only fact sentences,
    keyed by local names such as "coverage". Ledger.record() adds a call prefix
    (for example, "t1.coverage") so the model can cite each sentence uniquely.
    """

    title: str
    result: dict
    facts: dict[str, str]
    highlights: tuple[str, ...] = ()  # fact keys worth preloading into a report-writing prompt


@dataclass(frozen=True)
class ToolContext:
    """Host-controlled inputs shared by every call: the snapshot and session scenario."""

    snapshot: Snapshot
    scenario: Scenario


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., ToolOutput]  # called as handler(context, **arguments)

    def definition(self):
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": deepcopy(self.parameters)}}


def object_schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def _types(schema):
    return schema["type"] if isinstance(schema["type"], list) else [schema["type"]]


def _validate_value(name, value, schema):
    # Implements the schema keywords used by the registered tools, without a dependency.
    types = _types(schema)
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
        raise ArgumentError(f"{name} must be {'an' if expected[0] in 'aeiou' else 'a'} {expected}")
    if expected in ("integer", "number"):
        if isinstance(value, float) and not math.isfinite(value):
            raise ArgumentError(f"{name} must be finite")
        if "minimum" in schema and value < schema["minimum"]:
            raise ArgumentError(f"{name} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            hint = " (use a fraction: 0.08 means 8%)" if schema["maximum"] == 1 else ""
            raise ArgumentError(f"{name} must be at most {schema['maximum']}{hint}")
    if expected == "string":
        if "enum" in schema and value not in schema["enum"]:
            raise ArgumentError(f"{name} must be one of: {', '.join(schema['enum'])}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ArgumentError(f"{name} exceeds its maximum length")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise ArgumentError(f"{name} must use YYYY-MM with a valid month")
    if expected == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", math.inf):
            raise ArgumentError(f"{name} must have {schema.get('minItems', 0)} to "
                                f"{schema.get('maxItems', 'any number of')} items")
        for item in value:
            _validate_value(f"{name} item", item, schema["items"])


def validate_arguments(schema: dict, arguments: object) -> dict:
    """Return only supplied, valid arguments; omitted ones fall back to session settings."""
    if not isinstance(arguments, dict):
        raise ArgumentError("Tool arguments must be a JSON object")
    properties = schema["properties"]
    if arguments.keys() - properties.keys():
        raise ArgumentError("Unknown argument. Allowed: " + (", ".join(properties) or "none"))
    checked = {}
    for name, value in arguments.items():
        if value is None:
            continue  # small models often send null for every optional field: keep the session value
        _validate_value(name, value, properties[name])
        checked[name] = value
    missing = [name for name in schema.get("required", ()) if name not in checked]
    if missing:
        raise ArgumentError("Missing required argument: " + ", ".join(missing))
    return checked


def parse_arguments(text):
    try:
        value = read_json(text)
    except (ValueError, RecursionError):
        raise ArgumentError("Arguments must be a valid JSON object with unique keys") from None
    return value


def _error(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}


class ToolRegistry:
    """Allowlisted tools by name; the only way a model request reaches host code."""

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
        """Function schemas in the shape Ollama's chat API expects in `tools`."""
        return [tool.definition() for tool in self._tools.values()]

    def call(self, name, arguments, context, *, sanitize=True):
        """Validate and run one tool; every failure becomes a JSON-safe error envelope.

        Host-chosen calls pass sanitize=False so unexpected errors surface locally instead.
        """
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return _error("unknown_tool", "Unknown tool. Available tools: " + ", ".join(self._tools))
        try:
            if isinstance(arguments, str):
                arguments = parse_arguments(arguments)
            checked = validate_arguments(tool.parameters, {} if arguments is None else arguments)
            output = tool.handler(context, **checked)
            # Reject NaN/Infinity before a result can enter a model conversation or evidence file.
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
            return _error("execution_error", "The tool failed unexpectedly; raw details are not returned.")
