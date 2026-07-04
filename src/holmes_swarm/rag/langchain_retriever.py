"""Simple TF-style retriever over local markdown corpora.

Kept dependency-free for v1 (no langchain import required at runtime in this env).
Implements the same `Retriever` Protocol so a langchain-backed implementation can
swap in later without changing agent code.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Dict, List

from .base import Chunk, Retriever


_TOKEN = re.compile(r"[\wáéíóúñü]+", re.UNICODE | re.IGNORECASE)


def _tokenize(text: str) -> List[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


class InMemoryRetriever:
    def __init__(self, corpora: Dict[str, str]) -> None:
        self._corpora = corpora
        self._chunks: List[Chunk] = []
        for name, text in corpora.items():
            for para in _split_paragraphs(text):
                self._chunks.append(Chunk(text=para, source=name))

    async def retrieve(self, query: str, k: int = 5) -> List[Chunk]:
        q_tokens = Counter(_tokenize(query))
        if not q_tokens:
            return []
        scored: List[Chunk] = []
        for c in self._chunks:
            c_tokens = Counter(_tokenize(c.text))
            if not c_tokens:
                continue
            overlap = sum((q_tokens & c_tokens).values())
            if overlap == 0:
                continue
            score = overlap / (1 + len(c_tokens))
            scored.append(Chunk(text=c.text, source=c.source, score=score))
        scored.sort(key=lambda x: x.score, reverse=True)
        return scored[:k]


def _split_paragraphs(text: str) -> List[str]:
    parts = [p.strip() for p in re.split(r"\n\s*\n", text)]
    return [p for p in parts if p]


def load_default_retriever(corpora_dir: Path | str) -> Retriever:
    corpora_dir = Path(corpora_dir)
    corpora: Dict[str, str] = {}
    if corpora_dir.exists():
        for md in sorted(corpora_dir.glob("*.md")):
            corpora[md.name] = md.read_text(encoding="utf-8")
    return InMemoryRetriever(corpora)
