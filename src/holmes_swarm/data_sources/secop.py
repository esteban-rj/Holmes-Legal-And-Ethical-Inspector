"""SECOP Integrado data source.

Public reference:
    https://www.datos.gov.co/Estad-sticas-Nacionales/SECOP-Integrado/rpmr-utcd/about_data
SODA endpoint (JSON):
    https://www.datos.gov.co/resource/rpmr-utcd.json

This module wraps two things:

1.  A tolerant **normaliser** that turns a raw Socrata row into the
    `SECOPRecord` shape the Contracting Agent consumes.
2.  A small **source abstraction** (`SECOPSource`) with two adapters:
        - `SecopHttpSource`     -> live SODA call (allow-listed host)
        - `SecopOfflineCache`   -> pre-recorded JSON snapshot (no internet)

The Contracting Agent is wired in production to the **HTTP** source via the
allow-listed httpx client (see `config/example.yml` -> `contracting.internet_profile`).
For tests / air-gapped runs we use the **offline cache** adapter — it lets the
agent still consume SECOP-like records (mirroring the real schema) without any
network round trip. The remote `proveedor`/`nit_entidad` columns are mapped to
our internal `entity_id` (NIT, with dash) so signals from the live source and
the offline cache are interchangeable downstream.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # httpx is part of runtime deps
    import httpx  # type: ignore
except Exception:  # pragma: no cover - safety for weird envs
    httpx = None  # type: ignore


SECOP_SODA_BASE = "https://www.datos.gov.co/resource/rpmr-utcd.json"


# ---- Tolerated column-name aliases ------------------------------------------
# The public SECOP Integrado dataset exposes its columns under several names
# across releases. The normaliser accepts any of them so we don't break on
# upstream column renames.
_NIT_ALIASES = (
    "nit_entidad",
    "identificacion_del_contratista",
    "identificaci_n_del_contratista",
    "documento_proveedor",
    "nit_proveedor",
    "proveedor_documento",
)

_CODE_ALIASES = (
    "codigo_proceso",
    "codigo_de_proceso",
    "unspsc",
    "codigo_principal_de_categoria",
    "codigo",
)

_PRICE_ALIASES = (
    "valor_total_del_contrato",
    "valor_contrato",
    "valor_total_adjudicacion",
    "valor_adjudicacion",
    "valor",
    "precio",
)

_DATE_ALIASES = (
    "fecha_de_firma",
    "fecha_firma",
    "fecha_de_publicacion",
    "fecha_de_adjudicacion",
    "fecha_de_inicio_del_contrato",
    "fecha",
)

_PLATFORM_ALIASES = (
    "origen",
    "fuente",
    "nombre_entidad",
    "proceso_de_compra",
    "platform",
)

_ENT_ID_ALIASES = (
    "nit_entidad",
    "nit_de_la_entidad",
    "documento_entidad",
    "entidad_nit",
    "entity_id",
)


def _first_present(record: dict[str, Any], aliases: Sequence[str]) -> Any | None:
    """Return the value of the first matching (case- and underscore-insensitive) key."""
    lowered: dict[str, Any] = {}
    for k, v in record.items():
        if k is None:
            continue
        lowered[str(k).strip().lower().replace(" ", "_")] = v
    for alias in aliases:
        if alias in lowered and lowered[alias] not in (None, ""):
            return lowered[alias]
    return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.replace("$", "").replace(",", "").replace(" ", "").strip()
        try:
            return float(s)
        except Exception:
            return None
    return None


def _to_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _to_iso_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    s = str(value).strip()
    if not s:
        return None
    if "T" in s:
        return s
    return s  # YYYY-MM-DD is fine for downstream filtering


# ---- Data model --------------------------------------------------------------


@dataclass(frozen=True)
class SECOPRecord:
    """Normalised view of a single SECOP Integrado row."""

    entity_id: str  # proveedor NIT
    procedure_code: str  # código CUPS/UNSPSC-style procedure
    price: float  # COP
    contract_date: str | None = None
    platform: str = "SECOP"
    raw: dict[str, Any] = field(default_factory=dict)

    def to_contracting_batch_entry(self) -> dict[str, Any]:
        """Map into the dict shape consumed by `ContractingAgent.run()`."""
        return {
            "code": self.procedure_code,
            "price": self.price,
            "platform": self.platform,
            "contract_date": self.contract_date,
            "entity_id": self.entity_id,
        }


# ---- Normaliser --------------------------------------------------------------


def normalise_secop_record(raw: dict[str, Any]) -> SECOPRecord | None:
    """Coerce a raw Socrata row into a `SECOPRecord`. Returns None if unusable."""
    if not isinstance(raw, dict):
        return None

    entity_id = _to_str(_first_present(raw, _NIT_ALIASES))
    procedure_code = _to_str(_first_present(raw, _CODE_ALIASES))
    price = _to_float(_first_present(raw, _PRICE_ALIASES))
    if not entity_id or not procedure_code or price is None:
        return None

    contract_date = _to_iso_date(_first_present(raw, _DATE_ALIASES))
    platform = _to_str(_first_present(raw, _PLATFORM_ALIASES)) or "SECOP"

    return SECOPRecord(
        entity_id=entity_id,
        procedure_code=procedure_code,
        price=price,
        contract_date=contract_date,
        platform=platform,
        raw=raw,
    )


def load_secop_records(records: Iterable[dict[str, Any]]) -> list[SECOPRecord]:
    out: list[SECOPRecord] = []
    for r in records:
        n = normalise_secop_record(r)
        if n is not None:
            out.append(n)
    return out


# ---- Source interface + adapters --------------------------------------------


class SECOPSource:
    """Source of SECOP records for the Contracting Agent.

    Implementations may hit the live SODA endpoint or a local cache.
    """

    def fetch_for_entity(
        self,
        entity_id: str,
        *,
        limit: int = 50,
        procedure_code: str | None = None,
    ) -> list[SECOPRecord]:
        """Return SECOP records for a given entity (tax-id).

        Implementations MAY broaden filters based on `procedure_code`.
        """
        raise NotImplementedError


class SecopOfflineCache(SECOPSource):
    """Offline adapter backed by either a JSON file or an in-memory record list.

    The snapshot file is expected to be a list of raw Socrata rows — same shape
    as what the live endpoint returns. Adapter normalises each row, so the
    cache is a 1:1 mirror of SECOP Integrado for testing/demos.

    Constructors:
        `SecopOfflineCache(snapshot_path="…")`       — load from disk
        `SecopOfflineCache(records=[…])`              — in-memory, useful as a
                                                        no-network fallback
    """

    def __init__(
        self,
        *,
        snapshot_path: str | Path | None = None,
        records: Iterable[SECOPRecord] | None = None,
    ) -> None:
        self._path = Path(snapshot_path) if snapshot_path is not None else None
        self._records: list[SECOPRecord] = list(records) if records is not None else []
        self._loaded = records is not None

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if self._path is None:
            self._records = []
            self._loaded = True
            return
        if not self._path.exists():
            self._records = []
            self._loaded = True
            return
        try:
            raw = json.loads(self._path.read_text())
        except Exception:
            raw = []
        rows = raw if isinstance(raw, list) else raw.get("rows", [])
        self._records = load_secop_records(rows)
        self._loaded = True

    def add(self, records: Iterable[SECOPRecord]) -> None:
        """Append records directly (used in tests where we build them in-memory)."""
        self._records.extend(records)

    def fetch_for_entity(
        self,
        entity_id: str,
        *,
        limit: int = 50,
        procedure_code: str | None = None,
    ) -> list[SECOPRecord]:
        self._ensure_loaded()
        out: list[SECOPRecord] = []
        for r in self._records:
            # Accept sentinel entity ids that mean "all providers".
            if entity_id not in ("", "*"):
                if r.entity_id != entity_id:
                    continue
            if procedure_code and r.procedure_code != procedure_code:
                continue
            out.append(r)
            if len(out) >= limit:
                break
        return out


class SecopHttpSource(SECOPSource):
    """Live adapter. Hits the SECOP Integrado SODA endpoint with SoQL filters.

    All HTTP traffic MUST go through an allow-listed httpx.AsyncClient (see
    `holmes_swarm.net.allowlist_client`). `datos.gov.co` is added to the
    ContractingAgent's allow-list by config.
    """

    def __init__(
        self,
        *,
        http_client: Any,
        base_url: str = SECOP_SODA_BASE,
        app_token: str | None = None,
    ) -> None:
        if httpx is None:
            raise RuntimeError("httpx is required for SecopHttpSource")
        self._http = http_client
        self._base = base_url
        self._app_token = app_token

    async def fetch_for_entity(
        self,
        entity_id: str,
        *,
        limit: int = 50,
        procedure_code: str | None = None,
    ) -> list[SECOPRecord]:
        where_parts = [f"nit_entidad='{entity_id}'"]
        if procedure_code:
            where_parts.append(f"codigo_proceso='{procedure_code}'")
        params: dict[str, Any] = {
            "$limit": str(limit),
            "$where": " AND ".join(where_parts),
            "$order": "fecha_de_firma DESC",
        }
        if self._app_token:
            params["$$app_token"] = self._app_token
        # Network/HTTP errors propagate to the caller. Returning silently
        # would hide outages from the audit log and make "no SECOP data"
        # indistinguishable from "SECOP source down" — see review note.
        resp = await self._http.get(self._base, params=params)
        resp.raise_for_status()
        data = resp.json()
        return load_secop_records(data if isinstance(data, list) else [])


# ---- Factory used by app wiring --------------------------------------------


def make_default_secop_source(
    *,
    snapshot_path: str | Path | None = None,
    http_client: Any | None = None,
) -> SECOPSource:
    """Pick offline-cache vs live HTTP based on what's available.

    Order of preference:
      1. If `http_client` is provided -> live `SecopHttpSource`.
      2. Else if `snapshot_path` is provided and exists -> offline cache.
      3. Else return an empty in-memory cache (returns no rows; the agent
         falls back to its bundled `reference_prices`).
    """
    if http_client is not None:
        return SecopHttpSource(http_client=http_client)
    if snapshot_path is not None and Path(snapshot_path).exists():
        return SecopOfflineCache(snapshot_path=snapshot_path)
    return SecopOfflineCache(records=[])
