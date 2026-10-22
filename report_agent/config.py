"""Settings read from environment variables, so one codebase runs on a laptop,
an Aliyun ECS instance, or a customer's server without code changes.

An optional `.env` file in the project root is read too (KEY=VALUE per line);
real environment variables win over it.
"""

from dataclasses import dataclass
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> dict[str, str]:
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Settings:
    ollama_host: str
    ollama_model: str
    ollama_think: bool
    ollama_num_ctx: int
    ollama_num_predict: int
    ollama_timeout: float
    db_path: Path
    output_dir: Path


def load_settings() -> Settings:
    file_values = _load_dotenv(PROJECT_ROOT / ".env")

    def get(name: str, default: str) -> str:
        return os.environ.get(name, file_values.get(name, default))

    return Settings(
        # Only a local / on-prem address belongs here: no data may leave the
        # company's network, so never point this at a public LLM API.
        ollama_host=get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/"),
        ollama_model=get("OLLAMA_MODEL", "qwen3:8b"),
        # Qwen3 "thinking" roughly doubles CPU time; off by default.
        ollama_think=get("OLLAMA_THINK", "false").lower() in ("1", "true", "yes"),
        ollama_num_ctx=int(get("OLLAMA_NUM_CTX", "8192")),
        ollama_num_predict=int(get("OLLAMA_NUM_PREDICT", "1500")),
        ollama_timeout=float(get("OLLAMA_TIMEOUT", "900")),
        db_path=Path(get("DEMO_DB", str(PROJECT_ROOT / "outputs" / "demo_synthetic.db"))),
        output_dir=Path(get("REPORT_DIR", str(PROJECT_ROOT / "outputs" / "reports"))),
    )
