"""structlog setup with redaction (FR-021)."""

from __future__ import annotations

from typing import Any, Dict

import structlog


_SENSITIVE_KEYS = {
    "pqr_text", "pqr_body", "narrative", "phi",
    "patient_name", "patient_id", "document", "ssn", "tax_id",
}


def _redact(_, __, event_dict: Dict[str, Any]) -> Dict[str, Any]:
    for k in list(event_dict.keys()):
        if k.lower() in _SENSITIVE_KEYS:
            event_dict[k] = "[REDACTED]"
    return event_dict


def configure_logging(json: bool = True, level: str = "INFO") -> None:
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        _redact,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    if json:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(__import__("logging"), level)),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "holmes_swarm"):
    return structlog.get_logger(name)
