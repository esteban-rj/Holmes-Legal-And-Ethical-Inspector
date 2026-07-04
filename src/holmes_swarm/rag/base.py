"""RAG retriever interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Protocol


@dataclass
class Chunk:
    text: str
    source: str = ""
    score: float = 0.0


class Retriever(Protocol):
    async def retrieve(self, query: str, k: int = 5) -> List[Chunk]: ...
