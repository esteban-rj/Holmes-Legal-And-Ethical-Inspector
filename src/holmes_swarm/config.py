"""Settings loaded from YAML (pydantic v2)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    provider: Literal["mock", "minimax"] = "mock"
    model: str = "MiniMax-M3"
    api_base: str = "https://api.minimax.example/v1"
    api_key_env: str = "MINIMAX_API_KEY"


class InternetProfile(BaseModel):
    """Per-agent internet-access policy.

    `kind`:
        - "none":      no internet (default).
        - "public_ro": read-only public sources.
        - "llm_only":  outbound to a configured LLM provider only.

    `allowed_hosts`: regex patterns the agent's httpx client will permit.
    `llm_endpoint`:  URL of the LLM provider (used to extend `allowed_hosts`
                     when `kind == "llm_only"`).
    `explore_allowed_hosts`: regex patterns for URLs the LLM is allowed to
                     invoke via `fetch_url` / `web_search`. SEPARATE from
                     `allowed_hosts` so each can be audited independently.
                     Empty list = the LLM cannot make outbound calls.
    """

    kind: Literal["none", "public_ro", "llm_only"] = "none"
    allowed_hosts: list[str] = Field(default_factory=list)
    llm_endpoint: str | None = None
    explore_allowed_hosts: list[str] = Field(default_factory=list)


class AgentConfig(BaseModel):
    enabled: bool = True
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    internet_profile: InternetProfile = Field(default_factory=InternetProfile)
    staleness_window_seconds: int | None = None  # optional per-agent override


class BlackboardConfig(BaseModel):
    dedup_window_seconds: int = 60
    staleness_window_seconds: int = 86400


class InvestigationsConfig(BaseModel):
    default_timeout_seconds: int = 300
    consensus_evaluation_interval_seconds: int = 60
    rate_limit_per_minute: int = 10


class AuthConfig(BaseModel):
    tokens: dict[str, str] = Field(default_factory=dict)


class Settings(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    blackboard: BlackboardConfig = Field(default_factory=BlackboardConfig)
    investigations: InvestigationsConfig = Field(default_factory=InvestigationsConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> Settings:
        text = Path(path).read_text(encoding="utf-8")
        # Substitute ${llm.api_base} etc.
        text = _expand_env(text, os.environ)
        data = yaml.safe_load(text) or {}
        return cls.model_validate(data)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


def _expand_env(text: str, env: dict[str, str]) -> str:
    out = text
    for k, v in env.items():
        out = out.replace("${" + k + "}", v)
    return out
