"""Data sources for the Holmes Swarm agents.

These are thin, side-effect-isolated loaders that the agent(s) — primarily the
Contracting Agent — consult to enrich their analysis with **real public data**
rather than mocked inputs.

The first data source is SECOP Integrado (Colombia's open public-procurement
dataset published by `datos.gov.co`):

    https://www.datos.gov.co/Estad-sticas-Nacionales/SECOP-Integrado/rpmr-utcd

The Socrata SODA endpoint is:

    https://www.datos.gov.co/resource/rpmr-utcd.json

We do a tolerant normalisation because the schema of the public dataset is
broad and the agent only needs a small slice: entity tax-id (NIT), procedure
code, price, contract date and platform.
"""

from .secop import (
    SecopOfflineCache,
    SECOPRecord,
    SECOPSource,
    load_secop_records,
    make_default_secop_source,
    normalise_secop_record,
)

__all__ = [
    "SECOPRecord",
    "SECOPSource",
    "SecopOfflineCache",
    "load_secop_records",
    "make_default_secop_source",
    "normalise_secop_record",
]
