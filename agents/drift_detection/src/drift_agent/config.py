"""Configuration: config.yaml thresholds, .env secrets and project paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

PROJECT_ROOT = Path(os.environ.get("DRIFT_AGENT_HOME", Path(__file__).resolve().parents[2]))


@dataclass(frozen=True)
class Paths:
    """`root` holds the dataset; `workspace` (default: root/data and root/models) holds
    everything a run produces, so the evaluation can run in an isolated folder."""

    root: Path
    workspace: Path | None = None

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def raw_csv(self) -> Path:
        return self.data / "dataset.csv"

    @property
    def splits(self) -> Path:
        return self.data / "splits"

    @property
    def batches(self) -> Path:
        return (self.workspace or self.data) / "batches"

    @property
    def models(self) -> Path:
        return self.workspace / "models" if self.workspace else self.root / "models"

    @property
    def db(self) -> Path:
        return (self.workspace or self.data) / "drift_agent.db"

    def ensure(self) -> None:
        for p in (self.data, self.splits, self.batches, self.models):
            p.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class LLMSettings:
    api_key: str | None
    model: str
    base_url: str
    timeout_seconds: float
    max_retries: int

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class Settings:
    raw: dict[str, Any]
    paths: Paths

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    @property
    def llm(self) -> LLMSettings:
        """Read fresh on every use, so adding or changing the key in .env needs no restart.

        Precedence: real environment variables, then .env, then config.yaml defaults.
        """
        file_values = dotenv_values(self.paths.root / ".env") if (self.paths.root / ".env").exists() else {}

        def pick(name: str) -> str | None:
            return os.environ.get(name) or file_values.get(name) or None

        cfg = self.raw["agent"]["llm"]
        return LLMSettings(
            api_key=pick("XAI_API_KEY"),
            model=pick("XAI_MODEL") or cfg["model"],
            base_url=pick("XAI_BASE_URL") or cfg["base_url"],
            timeout_seconds=float(cfg["timeout_seconds"]),
            max_retries=int(cfg["max_retries"]),
        )

    @property
    def features(self) -> list[str]:
        return list(self.raw["data"]["numeric"]) + list(self.raw["data"]["categorical"])

    @property
    def numeric(self) -> list[str]:
        return list(self.raw["data"]["numeric"])

    @property
    def categorical(self) -> list[str]:
        return list(self.raw["data"]["categorical"])

    @property
    def target(self) -> str:
        return self.raw["data"]["target"]

    @property
    def scenarios(self) -> dict[str, dict]:
        return self.raw["simulation"]["scenarios"]


def load_settings(root: Path | None = None, config_file: Path | None = None) -> Settings:
    root = Path(root) if root else PROJECT_ROOT
    cfg_path = config_file or root / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    paths = Paths(root)
    paths.ensure()
    return Settings(raw=raw, paths=paths)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
