"""Chat endpoint: translates natural-language into an investigation request.

Uses the configured LLMClient to extract a structured intent (target entity id +
optional scope). Falls back to a deterministic regex-based parser if the LLM
returns nothing usable, so the endpoint works offline (mock provider).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from ..llm.base import LLMClient, Message
from ..agents.registry import AgentRegistry


SYSTEM_PROMPT = """You are an assistant for a healthcare fraud-detection swarm.
Given a Spanish (or mixed Spanish/English) investigation request, extract:
- target_entity_id: the most specific identifier mentioned (tax ID / NIT, professional license / cédula, or the full person/company name if no ID is present). Use the literal name as the id if no number is given.
- display_name: a short label for the subject.
- location: any geographic context (city, institution, subred, ESE, etc.).
- procedure: any medical procedure, contract type, or clinical context.
- date_from / date_to: any date range if mentioned (ISO yyyy-mm-dd).
- narrative: a 1-sentence summary of the user's concern.
- agents: which agents to run. Allowed ids are: contracting, logistics, medical, whistleblower. Default = all four if the user does not specify.

Respond ONLY with a single JSON object. No prose. Example:
{"target_entity_id":"Ciro Alfonso Gómez Meisel","display_name":"Dr. Ciro Alfonso Gómez Meisel","location":"SUBRED INTEGRADA DE SERVICIOS DE SALUD NORTE Y SUR - Clínica Meisel SAS","procedure":null,"date_from":null,"date_to":null,"narrative":"Movimientos alarmantes en la subred norte y sur.","agents":["contracting","logistics","medical","whistleblower"]}
"""


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="User's natural-language investigation request.")
    auto_submit: bool = Field(default=True, description="If true, immediately submit an investigation and return request_id.")


class ChatParsed(BaseModel):
    target_entity_id: str
    display_name: Optional[str] = None
    location: Optional[str] = None
    procedure: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    narrative: Optional[str] = None
    agents: Optional[list[str]] = None
    confidence: str = "low"  # low | medium | high — how confident the parser is


class ChatResponse(BaseModel):
    parsed: ChatParsed
    request_id: Optional[str] = None
    status_url: Optional[str] = None
    stream_url: Optional[str] = None
    message: str = ""


_AGENT_HINTS = {
    "contracting": [
        "contrat", "secop", "licit", "adjudic", "monopolio", "precio",
        "factur", "adjudicac", "sospechos", "alarmant", "irregular",
        "subred integrada", "subred norte", "subred sur",
    ],
    "logistics": [
        "traslad", "movimient", "geograf", "distancia", "viaje", "ruta",
        "transporte", "imposible", "tiempo",
    ],
    "medical": [
        "clínic", "procedim", "diagnóst", "pacient", "tarif", "soat",
        "iss", "manual", "prescripc", "coherencia", "clínica",
    ],
    "whistleblower": [
        "pqrs", "queja", "denunci", "reclam", "whatsapp", "telegram",
        "ciudadano", "usuario",
    ],
}


def _guess_agents(text: str) -> list[str]:
    t = text.lower()
    hits: list[str] = []
    for aid, keys in _AGENT_HINTS.items():
        if any(k in t for k in keys):
            hits.append(aid)
    return hits or ["contracting", "logistics", "medical", "whistleblower"]


_NIT_RE = re.compile(r"\b(\d{9,12}-\d)\b")
_CC_RE = re.compile(r"\b(?:c[ée]dula|cc)\s*[:\.]?\s*(\d{6,12})", re.IGNORECASE)
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_LOCATION_HINTS = re.compile(
    r"((?:subred(?:\s+integrada(?:\s+de\s+servicios\s+de\s+salud)?(?:\s+norte(?:\s+y\s+sur)?|\s+sur(?:\s+y\s+norte)?)?))|"
    r"ese\s+[\wáéíóúñ]+|"
    r"cl[íi]nica\s+[\wáéíóúñ]+(?:\s+SAS)?|"
    r"hospital\s+[\wáéíóúñ]+(?:\s+[\wáéíóúñ]+){0,2}|"
    r"\bips\s+[\wáéíóúñ]+)",
    re.IGNORECASE,
)


def _fallback_parse(text: str) -> ChatParsed:
    """Deterministic offline parser. Used when the LLM does not return JSON."""
    t = text.strip()

    # Identifier (NIT or cédula) if present
    target_id: Optional[str] = None
    display_name: Optional[str] = None
    m = _NIT_RE.search(t)
    if m:
        target_id = m.group(1)
    else:
        m = _CC_RE.search(t)
        if m:
            target_id = m.group(1)

    # Try to extract a person's / company's display name from common Spanish patterns
    name_patterns = [
        # Dr. / Señor + at least two capitalized words, each starting with uppercase
        r"(?:se[ñn]or(?:a)?|dr|dra)\.?\s+((?:[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+\s+){1,6}[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+)",
        # "de la CLINICA SAS" pattern (e.g., "IPS CardioVital")
        r"(?:ips\s+|empresa\s+|sociedad\s+)([A-ZÁÉÍÓÚÑ][\wáéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+){0,3})",
    ]
    for pat in name_patterns:
        m = re.search(pat, t, re.IGNORECASE)
        if m:
            display_name = m.group(1).strip()
            break

    if not target_id:
        # Use display_name or first capitalized chunk as the entity id
        if display_name:
            # Trim trailing "de la ..."
            display_name = re.sub(r"\s+de\s+la\s+\w+\s*$", "", display_name, flags=re.IGNORECASE).strip()
            target_id = display_name
        else:
            caps = re.findall(r"\b([A-ZÁÉÍÓÚÑ][\wáéíóúñ]{2,}(?:\s+[A-ZÁÉÍÓÚÑ][\wáéíóúñ]{2,}){1,5})\b", t)
            # Prefer the first chunk that doesn't contain lowercase stopwords at the end
            if caps:
                caps.sort(key=lambda s: (-len(s), s.count(" de ")))
                display_name = caps[0]
                target_id = caps[0]
            else:
                # Last resort: hash the entire text so it's stable
                target_id = t[:120]

    # Location: prefer the longest match (e.g., "SUBRED INTEGRADA..." over "clínica X")
    matches = list(_LOCATION_HINTS.finditer(t))
    location = max((m.group(1).strip() for m in matches), key=len, default=None)

    # Date range
    dates = _DATE_RE.findall(t)
    date_from = dates[0] if dates else None
    date_to = dates[1] if len(dates) > 1 else None

    return ChatParsed(
        target_entity_id=target_id,
        display_name=display_name,
        location=location,
        procedure=None,
        date_from=date_from,
        date_to=date_to,
        narrative=t,
        agents=_guess_agents(t),
        confidence="medium",
    )


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Pull a JSON object out of an LLM response (handles ```json fences)."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        # Find the first { ... } block
        first = text.find("{")
        last = text.rfind("}")
        if first != -1 and last > first:
            text = text[first : last + 1]
    try:
        return json.loads(text)
    except Exception:
        return None


async def parse_chat(
    llm: LLMClient,
    registry: AgentRegistry,
    message: str,
) -> ChatParsed:
    """Try the LLM first; fall back to the deterministic parser if it fails."""
    valid_agents = {a.id for a in registry.all() if a.id != "consensus"}
    try:
        resp = await llm.chat(
            messages=[
                Message(role="system", content=SYSTEM_PROMPT),
                Message(role="user", content=message),
            ],
            temperature=0.0,
            max_tokens=400,
        )
        data = _extract_json(resp.text)
        if data and "target_entity_id" in data:
            agents = data.get("agents") or _guess_agents(message)
            agents = [a for a in agents if a in valid_agents] or list(valid_agents)
            return ChatParsed(
                target_entity_id=str(data["target_entity_id"]).strip(),
                display_name=data.get("display_name"),
                location=data.get("location"),
                procedure=data.get("procedure"),
                date_from=data.get("date_from"),
                date_to=data.get("date_to"),
                narrative=data.get("narrative") or message,
                agents=agents,
                confidence="high" if data.get("target_entity_id") else "low",
            )
    except Exception:
        pass
    return _fallback_parse(message)