"""Validated settings shared by the command line, config files, tools and analyses.

Every entry point builds these objects, so each rule is defined once. Passwords are
never settings: they come from MYSQL_PASSWORD or a local prompt when connecting.
"""

from dataclasses import asdict, dataclass, fields, replace
import ipaddress
import math
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

from . import PROJECT_ROOT
from .db import quote_identifier

MONTH_PATTERN = "[12][0-9]{3}-(0[1-9]|1[0-2])"
DEFAULT_CUTOFFS = (600, 640, 660, 680, 700, 720, 740)


class SettingsError(ValueError):
    """A command-line value, config file entry or tool argument is invalid."""


def _require(condition, message):
    if not condition:
        raise SettingsError(message)


def _integer(value):
    return type(value) is int  # bool is deliberately not an integer here


def _fraction(name, value):
    _require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1,
             f"{name} must be a fraction from 0 to 1 (0.08 means 8%)")
    return float(value)


def check_month(name, value):
    _require(value is None or (isinstance(value, str) and re.fullmatch(MONTH_PATTERN, value) is not None),
             f"{name} must use YYYY-MM with a valid month, for example 2015-01")
    return value


def project_path(value):
    """Resolve relative paths from the project root, never the current directory."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def local_url(value):
    """Require an explicit loopback HTTP endpoint so aggregates never reach a remote host."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        loopback = host == "localhost" or (host is not None and ipaddress.ip_address(host).is_loopback)
        if (not loopback or parsed.scheme != "http" or parsed.username is not None
                or parsed.password is not None or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or not 1 <= (parsed.port or 80) <= 65535):
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise SettingsError("Ollama URL must be a loopback HTTP URL, e.g. http://localhost:11434") from None
    return value.rstrip("/")


@dataclass(frozen=True)
class DatabaseConfig:
    """Trusted host settings; never part of model-controlled arguments."""

    host: str = "localhost"
    port: int = 3306
    user: str = "root"
    database: str = "demo"
    accepted_table: str = "accepted"
    rejected_table: str = "rejected"
    query_timeout: int = 600

    def __post_init__(self):
        for name in ("host", "user", "database", "accepted_table", "rejected_table"):
            _require(isinstance(getattr(self, name), str) and getattr(self, name), f"{name} must be a nonempty string")
        _require(_integer(self.port) and 1 <= self.port <= 65535, "port must be an integer from 1 to 65535")
        _require(_integer(self.query_timeout) and self.query_timeout >= 1, "query_timeout must be a positive integer")
        for name in ("database", "accepted_table", "rejected_table"):
            try:
                quote_identifier(getattr(self, name))
            except ValueError as error:
                raise SettingsError(str(error)) from None
        _require(self.accepted_table.casefold() != self.rejected_table.casefold(),
                 "Accepted and rejected tables must differ")

    def sources(self):
        """Database and table names only, safe for evidence files (no host or user)."""
        return {"database": self.database, "accepted_table": self.accepted_table,
                "rejected_table": self.rejected_table}


@dataclass(frozen=True)
class Scenario:
    """Analysis inputs. Expansion assumptions are explicit user inputs, never model choices."""

    cutoffs: tuple = DEFAULT_CUTOFFS
    baseline: int = 660
    start_month: str | None = None
    end_month: str | None = None
    max_loss_rate: float | None = None
    min_outcomes: int = 100
    assume_comparable_scores: bool = False
    rejected_loss_rate: float | None = None
    take_up_rate: float = 1.0

    def __post_init__(self):
        cutoffs = self.cutoffs
        if isinstance(cutoffs, str):
            try:
                cutoffs = [int(part.strip()) for part in cutoffs.split(",")]
            except ValueError:
                raise SettingsError("cutoffs must be comma-separated integers") from None
        _require(isinstance(cutoffs, (list, tuple)) and len(cutoffs) > 0
                 and all(_integer(cutoff) for cutoff in cutoffs) and _integer(self.baseline),
                 "cutoffs and baseline must be integers")
        cutoffs = tuple(sorted(set(cutoffs) | {self.baseline}))
        _require(len(cutoffs) <= 30 and all(300 <= cutoff <= 850 for cutoff in cutoffs),
                 "Use at most 30 cutoffs, all between 300 and 850, including baseline")
        object.__setattr__(self, "cutoffs", cutoffs)
        check_month("start_month", self.start_month)
        check_month("end_month", self.end_month)
        _require(not (self.start_month and self.end_month and self.start_month > self.end_month),
                 "start_month must not be after end_month")
        _require(_integer(self.min_outcomes) and self.min_outcomes >= 1, "min_outcomes must be a positive integer")
        for name in ("max_loss_rate", "rejected_loss_rate"):
            if getattr(self, name) is not None:
                object.__setattr__(self, name, _fraction(name, getattr(self, name)))
        object.__setattr__(self, "take_up_rate", _fraction("take_up_rate", self.take_up_rate))
        _require(type(self.assume_comparable_scores) is bool, "assume_comparable_scores must be true or false")
        _require(self.assume_comparable_scores == (self.rejected_loss_rate is not None),
                 "Use assume_comparable_scores and rejected_loss_rate together")
        _require(self.take_up_rate == 1 or self.assume_comparable_scores,
                 "take_up_rate requires the explicit rejected lending assumptions")

    def with_overrides(self, **changes):
        """Return a copy with changes applied; every rule is checked again."""
        return replace(self, **changes)

    @property
    def cohort(self):
        return f"{self.start_month or 'earliest available'} to {self.end_month or 'latest available'}"

    def to_dict(self):
        return {**asdict(self), "cutoffs": list(self.cutoffs)}


@dataclass(frozen=True)
class ModelConfig:
    """Local Ollama settings. Only loopback endpoints and installed local models are used."""

    url: str = "http://localhost:11434"
    model: str = "qwen3:4b"
    timeout: int = 300
    keep_alive: str = "5m"
    num_ctx: int = 16384
    max_tool_rounds: int = 4
    max_tool_calls: int = 8

    def __post_init__(self):
        object.__setattr__(self, "url", local_url(self.url))
        _require(isinstance(self.model, str) and self.model.strip(), "model must be a nonempty model tag")
        _require(isinstance(self.keep_alive, str) and self.keep_alive, "keep_alive must be a duration such as 5m")
        for name in ("timeout", "num_ctx", "max_tool_calls"):
            _require(_integer(getattr(self, name)) and getattr(self, name) >= 1, f"{name} must be a positive integer")
        _require(_integer(self.max_tool_rounds) and 0 <= self.max_tool_rounds <= 10,
                 "max_tool_rounds must be an integer from 0 to 10")


CONFIG_KEYS = {
    "database": {field.name for field in fields(DatabaseConfig)},
    "scenario": {field.name for field in fields(Scenario)},
    "model": {field.name for field in fields(ModelConfig)},
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
        _require(section in CONFIG_KEYS and isinstance(values, dict), f"{path.name}: unknown section [{section}]")
        for key in values:
            _require("password" not in key.casefold(),
                     "Passwords are never read from config files; use MYSQL_PASSWORD or the terminal prompt")
            _require(key in CONFIG_KEYS[section], f"{path.name}: unknown setting {section}.{key}")
    return data
