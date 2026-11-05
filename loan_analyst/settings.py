"""Validated settings shared by the CLI, config files, tools and analyses.

Every entry point builds these three objects, so each rule is written once:

* DatabaseConfig  - where MySQL is, and which tables to read
* Scenario        - the analysis choices (cohort, cutoffs, loss ceiling, ...)
* ModelConfig     - which local Ollama model to use and how long to wait

They are ordinary classes with a linear ``__init__``: read the assignments and
checks from top to bottom. Passwords are deliberately not settings.
"""

import ipaddress
import math
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

from . import PROJECT_ROOT
from .database import quote_identifier

MONTH_PATTERN = "[12][0-9]{3}-(0[1-9]|1[0-2])"
DEFAULT_CUTOFFS = (600, 640, 660, 680, 700, 720, 740)
DEFAULT_MODEL = "qwen3:4b-instruct-2507-q4_K_M"


class SettingsError(ValueError):
    """A command-line value, config entry or tool argument is invalid."""


def require(condition, message):
    """Raise a SettingsError with a readable message when a rule fails."""
    if not condition:
        raise SettingsError(message)


def is_plain_integer(value):
    """True for 1, 2, 3... but not for True/False, which Python treats as numbers."""
    return type(value) is int


def fraction(name, value):
    """Validate a 0..1 fraction such as 0.08 (8%) and return it as a float."""
    require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1,
            f"{name} must be a fraction from 0 to 1 (0.08 means 8%)")
    return float(value)


def check_month(name, value):
    """Accept None or a real YYYY-MM month."""
    require(value is None or (isinstance(value, str) and re.fullmatch(MONTH_PATTERN, value) is not None),
            f"{name} must use YYYY-MM with a valid month, for example 2015-01")
    return value


def project_path(value):
    """Resolve a relative path from the project root, never the current directory."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def local_url(value):
    """Require an explicit loopback HTTP endpoint, so aggregates never leave the machine."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        is_loopback = host == "localhost" or (host is not None and ipaddress.ip_address(host).is_loopback)
        valid = (
            is_loopback
            and parsed.scheme == "http"
            and parsed.username is None
            and parsed.password is None
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
            and 1 <= (parsed.port or 80) <= 65535
        )
        if not valid:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise SettingsError("Ollama URL must be a loopback HTTP URL, e.g. http://localhost:11434") from None
    return value.rstrip("/")


class DatabaseConfig:
    """Trusted MySQL settings. The model can never change these."""

    def __init__(self, host="localhost", port=3306, user="root", database="demo",
                 accepted_table="accepted", rejected_table="rejected", query_timeout=600):
        for name, value in (("host", host), ("user", user), ("database", database),
                            ("accepted_table", accepted_table), ("rejected_table", rejected_table)):
            require(isinstance(value, str) and value, f"{name} must be a nonempty string")
        require(is_plain_integer(port) and 1 <= port <= 65535,
                "port must be an integer from 1 to 65535")
        require(is_plain_integer(query_timeout) and query_timeout >= 1,
                "query_timeout must be a positive integer")

        for name, value in (("database", database), ("accepted_table", accepted_table),
                            ("rejected_table", rejected_table)):
            try:
                quote_identifier(value)
            except ValueError as error:
                raise SettingsError(str(error)) from None
        require(accepted_table.casefold() != rejected_table.casefold(),
                "Accepted and rejected tables must differ")

        self.host = host
        self.port = port
        self.user = user
        self.database = database
        self.accepted_table = accepted_table
        self.rejected_table = rejected_table
        self.query_timeout = query_timeout

    def sources(self):
        """Database and table names only. Safe to save in evidence (no host or user)."""
        return {
            "database": self.database,
            "accepted_table": self.accepted_table,
            "rejected_table": self.rejected_table,
        }


class Scenario:
    """The user's analysis choices.

    Cutoffs are inclusive minimum scores. The baseline is added automatically so
    every comparison has a reference point. Expansion assumptions are explicit
    user inputs, never model choices.
    """

    def __init__(self, cutoffs=DEFAULT_CUTOFFS, baseline=660, start_month=None,
                 end_month=None, max_loss_rate=None, min_outcomes=100,
                 assume_comparable_scores=False, rejected_loss_rate=None, take_up_rate=1.0):
        # A command-line value arrives as "600,640,660", a config file as a list.
        if isinstance(cutoffs, str):
            try:
                cutoffs = [int(part.strip()) for part in cutoffs.split(",")]
            except ValueError:
                raise SettingsError("cutoffs must be comma-separated integers") from None

        valid_cutoffs = (
            isinstance(cutoffs, (list, tuple))
            and len(cutoffs) > 0
            and all(is_plain_integer(cutoff) for cutoff in cutoffs)
            and is_plain_integer(baseline)
        )
        require(valid_cutoffs, "cutoffs and baseline must be integers")

        cutoffs = tuple(sorted(set(cutoffs) | {baseline}))
        require(len(cutoffs) <= 30 and all(300 <= cutoff <= 850 for cutoff in cutoffs),
                "Use at most 30 cutoffs, all between 300 and 850, including baseline")

        check_month("start_month", start_month)
        check_month("end_month", end_month)
        require(not (start_month and end_month and start_month > end_month),
                "start_month must not be after end_month")
        require(is_plain_integer(min_outcomes) and min_outcomes >= 1,
                "min_outcomes must be a positive integer")

        if max_loss_rate is not None:
            max_loss_rate = fraction("max_loss_rate", max_loss_rate)
        if rejected_loss_rate is not None:
            rejected_loss_rate = fraction("rejected_loss_rate", rejected_loss_rate)
        take_up_rate = fraction("take_up_rate", take_up_rate)

        require(type(assume_comparable_scores) is bool,
                "assume_comparable_scores must be true or false")
        require(assume_comparable_scores == (rejected_loss_rate is not None),
                "Use assume_comparable_scores and rejected_loss_rate together")
        require(take_up_rate == 1 or assume_comparable_scores,
                "take_up_rate requires the explicit rejected lending assumptions")

        self.cutoffs = cutoffs
        self.baseline = baseline
        self.start_month = start_month
        self.end_month = end_month
        self.max_loss_rate = max_loss_rate
        self.min_outcomes = min_outcomes
        self.assume_comparable_scores = assume_comparable_scores
        self.rejected_loss_rate = rejected_loss_rate
        self.take_up_rate = take_up_rate

    def with_overrides(self, **changes):
        """Return a copy with changes applied; every rule is checked again."""
        values = self.to_dict()
        values.update(changes)
        return Scenario(**values)

    @property
    def cohort(self):
        return f"{self.start_month or 'earliest available'} to {self.end_month or 'latest available'}"

    def to_dict(self):
        """Plain data, in the same field order every evidence file uses."""
        return {
            "cutoffs": list(self.cutoffs),
            "baseline": self.baseline,
            "start_month": self.start_month,
            "end_month": self.end_month,
            "max_loss_rate": self.max_loss_rate,
            "min_outcomes": self.min_outcomes,
            "assume_comparable_scores": self.assume_comparable_scores,
            "rejected_loss_rate": self.rejected_loss_rate,
            "take_up_rate": self.take_up_rate,
        }


class ModelConfig:
    """Local Ollama settings. Only loopback endpoints and local models are allowed."""

    model = DEFAULT_MODEL  # class-level default so error messages can name it

    def __init__(self, url="http://localhost:11434", model=DEFAULT_MODEL, timeout=300,
                 keep_alive="5m", num_ctx=16384, max_tool_rounds=4, max_tool_calls=8):
        url = local_url(url)
        require(isinstance(model, str) and model.strip(), "model must be a nonempty model tag")
        require(isinstance(keep_alive, str) and keep_alive,
                "keep_alive must be a duration such as 5m")
        for name, value in (("timeout", timeout), ("num_ctx", num_ctx),
                            ("max_tool_calls", max_tool_calls)):
            require(is_plain_integer(value) and value >= 1, f"{name} must be a positive integer")
        require(is_plain_integer(max_tool_rounds) and 0 <= max_tool_rounds <= 10,
                "max_tool_rounds must be an integer from 0 to 10")

        self.url = url
        self.model = model
        self.timeout = timeout
        self.keep_alive = keep_alive
        self.num_ctx = num_ctx
        self.max_tool_rounds = max_tool_rounds
        self.max_tool_calls = max_tool_calls


CONFIG_KEYS = {
    "database": {"host", "port", "user", "database", "accepted_table", "rejected_table", "query_timeout"},
    "scenario": {"cutoffs", "baseline", "start_month", "end_month", "max_loss_rate", "min_outcomes",
                 "assume_comparable_scores", "rejected_loss_rate", "take_up_rate"},
    "model": {"url", "model", "timeout", "keep_alive", "num_ctx", "max_tool_rounds", "max_tool_calls"},
    "report": {"output_dir", "sections"},
}


def load_config(path):
    """Read TOML defaults as {section: {key: value}}; unknown keys and passwords are errors."""
    path = Path(path)
    try:
        with open(path, "rb") as stream:
            data = tomllib.load(stream)
    except tomllib.TOMLDecodeError as error:
        raise SettingsError(f"{path.name}: invalid TOML ({error})") from None

    for section, values in data.items():
        require(section in CONFIG_KEYS and isinstance(values, dict),
                f"{path.name}: unknown section [{section}]")
        for key in values:
            require("password" not in key.casefold(),
                    "Passwords are never read from config files; use MYSQL_PASSWORD or the terminal prompt")
            require(key in CONFIG_KEYS[section], f"{path.name}: unknown setting {section}.{key}")
    return data
