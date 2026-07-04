"""Bearer-token auth (FR-029)."""

from __future__ import annotations

from typing import Optional

import yaml
from fastapi import Header, HTTPException, status


class Auth:
    def __init__(self, tokens: dict[str, str]) -> None:
        self.tokens = tokens

    @classmethod
    def from_yaml(cls, path: str) -> "Auth":
        try:
            data = yaml.safe_load(open(path, "r", encoding="utf-8")) or {}
        except FileNotFoundError:
            data = {}
        return cls(tokens=data.get("tokens", {}) or {})

    def principal(self, authorization: Optional[str]) -> str:
        if not authorization:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing Authorization")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid Authorization scheme")
        requester = self.tokens.get(token)
        if not requester:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "unauthorised token")
        return requester


def bearer_principal(authorization: Optional[str] = Header(default=None)) -> str:
    """FastAPI dependency that resolves a bearer token to a requester id.

    NOTE: This is a placeholder dependency. The actual auth object is wired in
    `api/app.py` because the token map comes from Settings.
    """
    raise RuntimeError("Auth must be initialised via api.app.app_state.auth")
