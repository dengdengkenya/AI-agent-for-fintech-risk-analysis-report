"""Minimal client for a local Ollama server (https://github.com/ollama/ollama/blob/main/docs/api.md).

Only talks to the address in OLLAMA_HOST. There is deliberately no support
for hosted LLM APIs: report data must stay on the company's own machines.
"""

from dataclasses import dataclass
import json
import re
import time

import requests

from .config import Settings


class LLMError(RuntimeError):
    pass


@dataclass
class LLMReply:
    text: str
    seconds: float  # wall-clock, including model load
    prompt_tokens: int
    output_tokens: int
    load_seconds: float
    prompt_tokens_per_s: float
    output_tokens_per_s: float

    def summary(self) -> str:
        return (f"{self.seconds:.1f}s (load {self.load_seconds:.1f}s, "
                f"prompt {self.prompt_tokens} tok @ {self.prompt_tokens_per_s:.1f} tok/s, "
                f"output {self.output_tokens} tok @ {self.output_tokens_per_s:.1f} tok/s)")


class OllamaClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self.http = requests.Session()
        self.http.trust_env = False  # never route model traffic via a system proxy

    def list_models(self) -> list[str]:
        try:
            r = self.http.get(f"{self.s.ollama_host}/api/tags", timeout=10)
            r.raise_for_status()
        except requests.RequestException as e:
            raise LLMError(f"Cannot reach Ollama at {self.s.ollama_host}: {e}") from e
        return [m["name"] for m in r.json().get("models", [])]

    def chat(self, system: str, user: str, *, json_mode=False, temperature=0.2,
             num_predict: int | None = None) -> LLMReply:
        body = {
            "model": self.s.ollama_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "think": self.s.ollama_think,
            "options": {
                "temperature": temperature,
                "num_ctx": self.s.ollama_num_ctx,
                "num_predict": num_predict or self.s.ollama_num_predict,
            },
        }
        if json_mode:
            body["format"] = "json"
        started = time.perf_counter()
        data = self._post(body)
        seconds = time.perf_counter() - started

        ns = 1e9
        prompt_tokens = data.get("prompt_eval_count", 0)
        output_tokens = data.get("eval_count", 0)
        prompt_s = data.get("prompt_eval_duration", 0) / ns
        output_s = data.get("eval_duration", 0) / ns
        return LLMReply(
            # Some Qwen3 builds still emit an empty <think></think> block.
            text=re.sub(r"<think>.*?</think>", "", data.get("message", {}).get("content", ""), flags=re.S).strip(),
            seconds=seconds,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            load_seconds=data.get("load_duration", 0) / ns,
            prompt_tokens_per_s=prompt_tokens / prompt_s if prompt_s else 0.0,
            output_tokens_per_s=output_tokens / output_s if output_s else 0.0,
        )

    def _post(self, body: dict) -> dict:
        url = f"{self.s.ollama_host}/api/chat"
        try:
            r = self.http.post(url, json=body, timeout=self.s.ollama_timeout)
            if r.status_code == 400 and "think" in r.text and "think" in body:
                # Older Ollama versions or non-thinking models reject the field.
                body = {k: v for k, v in body.items() if k != "think"}
                r = self.http.post(url, json=body, timeout=self.s.ollama_timeout)
            if r.status_code == 404:
                raise LLMError(f"Model '{self.s.ollama_model}' not found on {self.s.ollama_host}. "
                               f"Run: ollama pull {self.s.ollama_model}  (or set OLLAMA_MODEL)")
            r.raise_for_status()
        except requests.RequestException as e:
            raise LLMError(f"Ollama request to {url} failed: {e}") from e
        try:
            return r.json()
        except json.JSONDecodeError as e:
            raise LLMError(f"Ollama returned non-JSON: {r.text[:200]}") from e
