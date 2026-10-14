"""Grok client. xAI's API is OpenAI-compatible, so we use the OpenAI SDK with xAI's base URL."""

from __future__ import annotations

from openai import OpenAI

from drift_agent.config import LLMSettings


class GrokClient:
    def __init__(self, settings: LLMSettings):
        if not settings.api_key:
            raise RuntimeError("XAI_API_KEY is not set. Copy .env.example to .env and add your key.")
        self.model = settings.model
        self.client = OpenAI(api_key=settings.api_key, base_url=settings.base_url,
                             timeout=settings.timeout_seconds, max_retries=settings.max_retries)

    def chat(self, messages: list[dict], tools: list[dict] | None = None, temperature: float | None = None,
             tool_choice: str = "auto"):
        kwargs = {"model": self.model, "messages": messages}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        return self.client.chat.completions.create(**kwargs)

    def list_models(self) -> list[str]:
        return sorted(m.id for m in self.client.models.list().data)
