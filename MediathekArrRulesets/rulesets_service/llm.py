"""Client for a local LLM behind an OpenAI-compatible API (Ollama, LM Studio, llama.cpp server, vLLM, ...)."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, settings: Settings):
        self.base_url = settings.llm_base_url.rstrip("/")
        self.model = settings.llm_model
        self.api_key = settings.llm_api_key
        self.pause = settings.llm_pause_seconds

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.model)

    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {"model": self.model, "messages": messages, "temperature": 0.1,
                "response_format": {"type": "json_object"}}
        with httpx.Client(timeout=600) as c:
            r = c.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
            if r.status_code == 400:
                # some servers reject response_format; the prompt asks for JSON anyway
                body.pop("response_format")
                r = c.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"] or ""
        if self.pause > 0:
            time.sleep(self.pause)  # give the GPU a break between requests (LLM_PAUSE_SECONDS)
        return parse_json_object(content)


def parse_json_object(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model answer (handles ```json fences and chatter)."""
    text = text.strip()
    if "</think>" in text:  # reasoning models
        text = text.split("</think>", 1)[1]
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in model answer")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unterminated JSON object in model answer")
