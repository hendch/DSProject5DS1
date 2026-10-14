"""One function, three backends: ask an LLM for a JSON object.

Pick the backend in .env (no code change):

    LLM_PROVIDER=ollama      local, free, nothing leaves the machine (default)
    LLM_PROVIDER=openai      any OpenAI-compatible API: Groq, OpenRouter, OpenAI,
                             Gemini's OpenAI endpoint... set LLM_BASE_URL + LLM_API_KEY
    LLM_PROVIDER=anthropic   Claude, set LLM_API_KEY

    LLM_MODEL=...            model id for that provider (OLLAMA_MODEL is used for ollama)

Written for the guardrail, but nothing in it is guardrail-specific: it can
move to shared/ if other agents want to switch too.
"""
import json
import os
import re

import httpx
import ollama
from dotenv import load_dotenv

load_dotenv()
PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()
API_KEY = os.getenv("LLM_API_KEY", "")
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
DEFAULT_MODELS = {"ollama": os.getenv("OLLAMA_MODEL", "llama3.2:3b"),
                  "openai": "llama-3.1-8b-instant",   # Groq's hosted Llama 3.1 8B
                  "anthropic": "claude-haiku-5-5"}
MODEL = (DEFAULT_MODELS["ollama"] if PROVIDER == "ollama"
         else os.getenv("LLM_MODEL") or DEFAULT_MODELS.get(PROVIDER, ""))
ANTHROPIC_URL = "https://api.anthropic.com/v1"


def label() -> str:
    """e.g. "ollama:llama3.2:3b", "x.ai:grok-…", "groq.com:llama-3.1-8b-instant"."""
    if PROVIDER == "openai":  # name the real provider, not the protocol
        host = httpx.URL(BASE_URL).host.removeprefix("api.")
        return f"{host}:{MODEL}"
    return f"{PROVIDER}:{MODEL}"


def chat_json(system: str, user: str, timeout: float = 60) -> dict:
    """Return the model's answer parsed as a JSON object. Raises on any failure
    (network, auth, rate limit, unparseable answer) so callers can fall back."""
    if PROVIDER == "ollama":
        resp = ollama.Client(timeout=timeout).chat(
            model=MODEL, format="json", options={"temperature": 0},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        return _parse(resp["message"]["content"])

    if not API_KEY:
        raise RuntimeError(f"LLM_PROVIDER={PROVIDER} needs LLM_API_KEY in .env")

    if PROVIDER == "openai":
        r = httpx.post(f"{BASE_URL}/chat/completions", timeout=timeout,
                       headers={"Authorization": f"Bearer {API_KEY}"},
                       json={"model": MODEL, "temperature": 0,
                             "response_format": {"type": "json_object"},
                             "messages": [{"role": "system", "content": system},
                                          {"role": "user", "content": user}]})
        _raise_for_status(r)
        return _parse(r.json()["choices"][0]["message"]["content"])

    if PROVIDER == "anthropic":
        r = httpx.post(f"{ANTHROPIC_URL}/messages", timeout=timeout,
                       headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01"},
                       json={"model": MODEL, "max_tokens": 512, "system": system,
                             "messages": [{"role": "user", "content": user}]})
        _raise_for_status(r)
        text = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
        return _parse(text)

    raise RuntimeError(f"unknown LLM_PROVIDER {PROVIDER!r} (use ollama, openai or anthropic)")


def check() -> tuple[bool, str]:
    """Cheap reachability/auth check (lists models, spends no tokens)."""
    try:
        if PROVIDER == "ollama":
            httpx.get("http://127.0.0.1:11434/api/tags", timeout=1).raise_for_status()
        elif not API_KEY:
            return False, "no LLM_API_KEY"
        elif PROVIDER == "openai":
            _raise_for_status(httpx.get(f"{BASE_URL}/models", timeout=3,
                                        headers={"Authorization": f"Bearer {API_KEY}"}))
        elif PROVIDER == "anthropic":
            _raise_for_status(httpx.get(f"{ANTHROPIC_URL}/models", timeout=3,
                                        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01"}))
        else:
            return False, f"unknown provider {PROVIDER!r}"
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:120]


def _raise_for_status(r: httpx.Response) -> None:
    if r.status_code >= 400:  # keep the provider's message, but never echo the key
        raise RuntimeError(f"HTTP {r.status_code} from {PROVIDER}: {r.text[:200]}")


def _parse(text: str) -> dict:
    """Models without a JSON mode sometimes wrap the object in prose or ``` fences."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise ValueError(f"no JSON object in LLM answer: {text[:120]!r}") from None
        data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("LLM answer is not a JSON object")
    return data
