"""Settings loaded from YAML (pydantic v2)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, field_validator


def _normalize_api_base(raw: str) -> str:
    """Ensure `api_base` is a valid absolute http(s) URL.

    Accepts bare hosts (e.g. `api.minimax.com`) and prepends `https://`.
    Raises ``ValueError`` on anything that can't be parsed, so misconfig
    fails loudly at startup instead of producing ``UnsupportedProtocol``
    errors deep inside the adapter.
    """
    candidate = raw.strip()
    if not candidate:
        raise ValueError("api_base is empty")
    # Reject unexpanded ${...} placeholders from the YAML loader — those
    # would silently parse as netloc="${MINIMAX_API_BASE}" and only blow
    # up inside the httpx adapter with UnsupportedProtocol.
    if "${" in candidate:
        raise ValueError(
            f"api_base contains an unexpanded ${{...}} placeholder: {raw!r}. "
            "Set the referenced env var before loading the config."
        )
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            f"api_base must be an absolute http(s) URL, got {raw!r}"
        )
    return candidate.rstrip("/")


def _default_api_base() -> str:
    """Default LLM endpoint.

    Resolution order:
      1. `MINIMAX_API_BASE` env var (set this when running locally so the
         adapter doesn't hit a placeholder hostname).
      2. Built-in placeholder — kept here so configs that explicitly opt
         into `provider: minimax` fail loudly (DNS error) instead of silently
         using a wrong host. The previous default `api.minimax.example`
         caused "nodename nor servname provided" because it never resolved.
    """
    env_val = os.environ.get("MINIMAX_API_BASE")
    if env_val:
        return _normalize_api_base(env_val)
    return "https://api.minimax.example/v1"


class LLMConfig(BaseModel):
    provider: Literal["mock", "minimax"] = "mock"
    # Base model id. When `thinking` is true the adapter automatically uses
    # the `-thinking` variant of the same model (e.g. MiniMax-M3-thinking),
    # which the provider exposes for chain-of-thought style reasoning that
    # arrives in a separate `reasoning_content` field.
    model: str = "MiniMax-M3"
    api_base: str = Field(default_factory=_default_api_base)
    api_key_env: str = "MINIMAX_API_KEY"
    # When true, the adapter sends the thinking variant and surfaces the
    # model's chain-of-thought tokens as `kind=thinking` agent_thought
    # events so the UI can render them in the per-agent transcript.
    thinking: bool = True
    # Optional override for the thinking variant id; otherwise we append
    # "-thinking" to `model`.
    thinking_model: str | None = None
    # Reasoning effort forwarded to providers that accept it (low|medium|high).
    reasoning_effort: Literal["low", "medium", "high"] | None = "medium"

    @field_validator("api_base")
    @classmethod
    def _validate_api_base(cls, value: str) -> str:
        return _normalize_api_base(value)


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
    `unrestricted_web`: when true, removes the `fetch_url` / `web_search`
                     host allow-list for this agent (LLM may hit any host).
                     The outbound httpx client (`allowed_hosts`) is
                     unaffected, so this is a deliberate escape hatch for
                     `fetch_url` / `web_search` only. Intended for trusted,
                     non-production environments (debugging, demos). MUST
                     remain false in any deployment subject to FR-019.
    """

    kind: Literal["none", "public_ro", "llm_only"] = "none"
    allowed_hosts: list[str] = Field(default_factory=list)
    llm_endpoint: str | None = None
    explore_allowed_hosts: list[str] = Field(default_factory=list)
    unrestricted_web: bool = False

    @field_validator("llm_endpoint")
    @classmethod
    def _validate_llm_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_api_base(value)


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
        # Pass 1: env var substitution (${MINIMAX_API_BASE}, etc.).
        text = _expand_env(text, os.environ)
        data = yaml.safe_load(text) or {}
        # Pass 2: YAML-internal references (${llm.api_base}, etc.). These
        # let one section reuse another value without copy-paste, e.g.
        # `llm_endpoint: ${llm.api_base}` for `llm_only` agents.
        _resolve_yaml_refs(data)
        return cls.model_validate(data)

    def get(self, key: str, default: Any | None = None) -> Any:
        return getattr(self, key, default)


_PLACEHOLDER_RE = __import__("re").compile(r"\$\{([^}]+)\}")


def _expand_env(text: str, env: dict[str, str]) -> str:
    out = text
    for k, v in env.items():
        out = out.replace("${" + k + "}", v)
    return out


def _resolve_yaml_refs(node: Any) -> None:
    """In-place replace `${a.b.c}` placeholders inside `node` using values
    from elsewhere in the same YAML mapping.

    Walks dicts and lists, leaving scalars alone except for string leaves
    that contain a placeholder. A reference like `${llm.api_base}` looks
    up `data["llm"]["api_base"]`. Unknown references are left as-is so
    the validator surfaces them with the original text.
    """
    def _walk(value: Any, root: dict[str, Any]) -> Any:
        if isinstance(value, dict):
            return {k: _walk(v, root) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v, root) for v in value]
        if isinstance(value, str) and "${" in value:
            return _PLACEHOLDER_RE.sub(
                lambda m: _resolve_ref(m.group(1).strip(), root, m.group(0)),
                value,
            )
        return value

    if not isinstance(node, dict):
        return
    resolved = _walk(node, node)
    node.clear()
    node.update(resolved)


def _resolve_ref(path: str, root: dict[str, Any], original: str) -> str:
    cur: Any = root
    for part in [p.strip() for p in path.split(".")]:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return original  # leave untouched; validator will catch it
    return cur if isinstance(cur, str) else original
