"""One LLM client for every agent, on any OpenAI-compatible endpoint: local Ollama
(http://localhost:11434/v1), the Hugging Face router, xAI, Groq...

Each agent reads its own profile, falling back to the shared default:

    LLM_BASE_URL / LLM_API_KEY / LLM_MODEL                  default for everyone
    DIAGNOSTIC_RCA_LLM_MODEL=deepseek-r1:8b                 per-agent override
    GUARDRAIL_LLM_BASE_URL=... GUARDRAIL_LLM_MODEL=...      (any of the three)

    llm = for_agent("diagnostic_rca")
    text = llm.chat("prompt")                 # or a list of {"role", "content"} messages
    data = llm.chat_json(system, user)        # parsed JSON object

Reasoning models' <think>...</think> blocks are stripped from the answer and kept in
`llm.last_reasoning`. After a failure the profile is skipped for a cooldown, so a
missing model doesn't cost a timeout on every call. Every failure raises
LLMUnavailable: callers fall back to their rules.
"""
import json
import os
import re
import threading
import time
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()
COOLDOWN_SECONDS = float(os.getenv("LLM_COOLDOWN", "30"))
_THINK = re.compile(r"<think>(.*?)</think>", re.DOTALL)
_down_until: dict[str, float] = {}
_lock = threading.Lock()


class LLMUnavailable(RuntimeError):
    """The model could not answer (down, no key, rate limited, unparseable answer)."""


@dataclass(frozen=True)
class Profile:
    agent: str
    base_url: str
    api_key: str
    model: str
    timeout: float

    @property
    def label(self) -> str:
        host = re.sub(r"^https?://", "", self.base_url).split("/")[0]
        host = "ollama" if host.startswith(("localhost:11434", "127.0.0.1:11434")) else host.removeprefix("api.")
        return f"{host}:{self.model}"


def profile(agent: str | None = None) -> Profile:
    def pick(name: str, default: str) -> str:
        if agent and os.getenv(f"{agent.upper()}_LLM_{name}"):
            return os.environ[f"{agent.upper()}_LLM_{name}"]
        return os.getenv(f"LLM_{name}") or default

    return Profile(agent or "default",
                   pick("BASE_URL", "http://localhost:11434/v1").rstrip("/"),
                   pick("API_KEY", "ollama"),
                   pick("MODEL", "llama3.1:8b"),
                   float(pick("TIMEOUT", "60")))


def split_reasoning(text: str) -> tuple[str, str]:
    """(answer, reasoning) for models that think in <think> tags."""
    reasoning = "\n".join(m.strip() for m in _THINK.findall(text))
    answer = _THINK.sub("", text)
    if "<think>" in answer:  # unterminated block: everything after it is reasoning
        answer, _, rest = answer.partition("<think>")
        reasoning = (reasoning + "\n" + rest).strip()
    return answer.strip(), reasoning


def parse_json_object(text: str) -> dict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise LLMUnavailable(f"no JSON object in the answer: {text[:120]!r}") from None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise LLMUnavailable(f"invalid JSON in the answer: {e}") from None
    if not isinstance(data, dict):
        raise LLMUnavailable("the answer is not a JSON object")
    return data


class LLM:
    def __init__(self, prof: Profile):
        self.profile = prof
        self.last_reasoning = ""

    @property
    def label(self) -> str:
        return self.profile.label

    def _key(self) -> str:
        return f"{self.profile.base_url}|{self.profile.model}"

    def available(self) -> bool:
        return time.monotonic() >= _down_until.get(self._key(), 0)

    def chat(self, prompt_or_messages, json_mode: bool = False, temperature: float = 0,
             max_tokens: int | None = None) -> str:
        if not self.available():
            raise LLMUnavailable(f"{self.label} is cooling down after a failure")
        messages = ([{"role": "user", "content": prompt_or_messages}]
                    if isinstance(prompt_or_messages, str) else list(prompt_or_messages))
        kwargs = {"model": self.profile.model, "messages": messages, "temperature": temperature}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        try:
            from openai import OpenAI
            client = OpenAI(base_url=self.profile.base_url, api_key=self.profile.api_key or "none",
                            timeout=self.profile.timeout, max_retries=1)
            text = client.chat.completions.create(**kwargs).choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001 - connection, auth, rate limit, model missing
            with _lock:
                _down_until[self._key()] = time.monotonic() + COOLDOWN_SECONDS
            raise LLMUnavailable(f"{self.label}: {type(e).__name__}: {str(e)[:200]}") from None
        answer, self.last_reasoning = split_reasoning(text)
        if not answer:
            raise LLMUnavailable(f"{self.label} returned an empty answer")
        return answer

    def chat_json(self, system: str, user: str, temperature: float = 0) -> dict:
        return parse_json_object(self.chat([{"role": "system", "content": system},
                                            {"role": "user", "content": user}],
                                           json_mode=True, temperature=temperature))

    def check(self) -> tuple[bool, str]:
        """Reachability and key check: lists models, spends no tokens."""
        try:
            from openai import OpenAI
            OpenAI(base_url=self.profile.base_url, api_key=self.profile.api_key or "none",
                   timeout=5, max_retries=0).models.list()
            return True, ""
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {str(e)[:120]}"


def for_agent(agent: str | None = None) -> LLM:
    return LLM(profile(agent))


def reset_cooldowns() -> None:
    with _lock:
        _down_until.clear()
