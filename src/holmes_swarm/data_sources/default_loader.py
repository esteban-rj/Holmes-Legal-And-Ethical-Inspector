"""Default demo data loader used when no caller-supplied loader is wired in.

Loads the JSON fixtures in `tests/fixtures/` and the cartel cardiologia case so
that a chat-style investigation request can produce signals without external
network access. If `HOLMES_FIXTURES_DIR` is set, that directory is used instead.

This is *not* production code — it's a convenience for the chat UI demo and the
dev environment. Production deployments should wire a real data_loader that
talks to the actual upstream sources.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional


_DEFAULT_FIXTURES_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures"


def _load_fixture(name: str, fixtures_dir: Path) -> Dict[str, Any]:
    try:
        return json.loads((fixtures_dir / name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def make_default_data_loader(
    fixtures_dir: Optional[Path] = None,
) -> Callable[[str], Dict[str, Any]]:
    fdir = fixtures_dir or Path(os.environ.get("HOLMES_FIXTURES_DIR", str(_DEFAULT_FIXTURES_DIR)))
    cartel = _load_fixture("cartel_cardiologia.json", fdir)
    contracts = _load_fixture("contracts.json", fdir)
    attendance = _load_fixture("attendance.json", fdir)
    clinical = _load_fixture("clinical.json", fdir)
    pqrs = _load_fixture("pqrs.json", fdir)

    cartel_entity_id = (cartel.get("entity") or {}).get("id", "")
    cartel_contracts = cartel.get("contracts") or contracts.get("contracts") or []

    def loader(entity_id: str) -> Dict[str, Any]:
        # If the requested entity matches the cartel case, return cartel-specific data.
        if entity_id and entity_id == cartel_entity_id:
            return {
                "entity_id": entity_id,
                "contracts": cartel_contracts,
                "events": cartel.get("events", attendance.get("events", [])),
                "specialty": (cartel.get("entity") or {}).get("specialty") or clinical.get("specialty"),
                "services": clinical.get("services", []),
                "procedures": clinical.get("procedures", [])[:3],
                "pqrs": cartel.get("pqrs", pqrs.get("pqrs", [])),
                "case_context": cartel.get("source", {}).get("context", ""),
            }
        # Generic fallback: bundle whatever fixtures exist.
        return {
            "entity_id": entity_id,
            "contracts": contracts.get("contracts", []),
            "events": attendance.get("events", []),
            "specialty": clinical.get("specialty"),
            "services": clinical.get("services", []),
            "procedures": clinical.get("procedures", [])[:3],
            "pqrs": pqrs.get("pqrs", []),
        }

    return loader