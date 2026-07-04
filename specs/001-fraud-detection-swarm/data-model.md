# Data Model: Healthcare Fraud Detection Swarm

All entities are pydantic v2 models. UUIDs are generated server-side. Timestamps are UTC.

## Entity (key + attributes)

| Field        | Type                | Notes                                          |
|--------------|---------------------|------------------------------------------------|
| `id`         | `str` (stable)      | Tax ID for providers, professional license for individuals. Spec assumes stable unique ids are available (no name matching). |
| `type`       | `Literal["provider","individual"]` |                                     |
| `display_name` | `str`             | Best-effort, not used for matching.            |
| `created_at` | `datetime` (UTC)    | First time any signal referenced it.           |

## Signal

| Field           | Type                                                | Validation                                    |
|-----------------|-----------------------------------------------------|-----------------------------------------------|
| `id`            | `UUID4`                                             | Server-generated.                             |
| `entity_id`     | `str`                                               | FK → `Entity.id`.                             |
| `signal_type`   | `Literal["financial","physical","clinical","operational"]` | One per agent's declared type.      |
| `source_agent`  | `str`                                               | FK → `Agent.id`.                              |
| `confidence`    | `float`                                             | `0.0 ≤ c ≤ 1.0`; rejected otherwise (FR-006).  |
| `evidence`      | `EvidenceReference`                                 | Opaque payload owned by source agent.         |
| `emitted_at`    | `datetime` (UTC)                                    | Set by Blackboard at publish time.            |
| `origin`        | `Origin` (discriminated union, see below)           | **Required**; missing/unknown → reject (FR-015, FR-031). |
| `below_threshold` | `bool`                                           | `True` if confidence < agent's threshold.     |

### `Origin`

A pydantic discriminated union:
- `{"kind": "autonomous-monitoring"}`
- `{"kind": "investigation", "investigation_request_id": UUID4}`

Anything else → `ValidationError` at publish time, signal rejected with logged error.

## Agent

| Field                | Type                                              | Notes                                    |
|----------------------|---------------------------------------------------|------------------------------------------|
| `id`                 | `str`                                             | e.g. `contracting`, `medical`.           |
| `name`               | `str`                                             | Human-readable.                          |
| `signal_type`        | `Signal.signal_type` literal                       | Type of signal this agent emits.         |
| `enabled`            | `bool`                                            | Default `True`.                          |
| `confidence_threshold`| `float` (`0.0–1.0`)                               | Applied at publish time (FR-011).        |
| `internet_profile`   | `InternetProfile` (see below)                      | Default for new agents = `none` (FR-019).|

### `InternetProfile`

- `"none"` — agent receives no HTTP client.
- `"public_ro"` with `allowed_hosts: list[str]` of host patterns (regex / suffix match) — agent receives an allow-listed `httpx.AsyncClient` (FR-018).
- `"llm_only"` with `llm_endpoint: str` — agent may only call the configured LLM provider endpoint (used by Whistleblower when remote LLM is enabled).

## EvidenceReference

Opaque object owned by source agent. Blackboard treats it as `dict[str, Any]`. Examples:
- `{"contract_id": "C-12345", "platform": "SECOP"}`
- `{"attendance_row_ids": ["A-1","A-2"], "provider_id": "...", "locations": [...], "travel_minutes_required": 240, "travel_minutes_observed": 30}`
- `{"procedure_codes": ["93010"], "monthly_volume": 320, "specialty": "cardiologia", "tariff_source": "SOAT"}`
- `{"pqr_id": "P-7", "entities": [...], "modus_operandi": "uso de WhatsApp y auxiliares"}`

## CriticalFraudAlert

| Field                          | Type             | Notes                                                                                |
|--------------------------------|------------------|--------------------------------------------------------------------------------------|
| `id`                           | `UUID4`          |                                                                                      |
| `entity_id`                    | `str`            | FK → `Entity.id`.                                                                    |
| `emitted_at`                   | `datetime`       |                                                                                      |
| `investigation_request_id`     | `UUID4`          | **Required** — alert is unreachable without one (FR-008 / FR-034 / SC-014).          |
| `contributing_signal_ids`      | `list[UUID4]`    | Includes the new signal and any prior qualifying signals still in the staleness window (FR-009). |
| `contributing_agent_ids`       | `list[str]`      |                                                                                      |
| `summary`                      | `str`            | Human-readable.                                                                      |

## InvestigationRequest

| Field            | Type                                           | Notes                                                    |
|------------------|------------------------------------------------|----------------------------------------------------------|
| `id`             | `UUID4`                                        |                                                          |
| `requester_id`   | `str`                                          | From authenticated principal (FR-029).                  |
| `target_entity_id` | `str`                                        |                                                          |
| `agents`         | `list[str] \| None`                            | `None` = all enabled agents (spec US2.AS3).              |
| `scope`          | `Scope`                                        | date range, location, procedure, free-form narrative.    |
| `submitted_at`   | `datetime`                                     |                                                          |
| `state`          | `Literal["queued","running","awaiting-external-data","completed","failed"]` | (FR-026)                          |
| `report_id`      | `UUID4 \| None`                                | Set when state becomes `completed` / `failed`.           |

## InvestigationReport

| Field            | Type                | Notes                                                                 |
|------------------|---------------------|-----------------------------------------------------------------------|
| `id`             | `UUID4`             |                                                                       |
| `request_id`     | `UUID4`             | FK → `InvestigationRequest.id`.                                       |
| `target_entity_id` | `str`             |                                                                       |
| `agents_ran`     | `list[str]`         |                                                                       |
| `signal_ids`     | `list[UUID4]`       | All signals produced in this investigation scope (origin `investigation:<request_id>`). |
| `summary`        | `str`               |                                                                       |
| `emitted_at`     | `datetime`          |                                                                       |

## AuditLogEntry (FR-030)

| Field            | Type        |
|------------------|-------------|
| `id`             | `UUID4`     |
| `at`             | `datetime`  |
| `actor`          | `str`       | Requester id. |
| `action`         | `Literal["investigation.submit","investigation.complete","alert.emit", ...]` |
| `target_entity_id` | `str`    |
| `request_id`     | `UUID4 \| None` |
| `report_id`      | `UUID4 \| None` |

## Relationships (summary)

```
Entity 1 ──< Signal * >── 1 Agent
Signal * >── 1 InvestigationRequest (via origin.investigation_request_id)
InvestigationRequest 1 ── 1 InvestigationReport
InvestigationRequest 1 ──< AuditLogEntry *
Entity 1 ──< CriticalFraudAlert *
CriticalFraudAlert * >── 1 InvestigationRequest (originating case)
```

## State transitions

```
InvestigationRequest:
  queued ──► running ──► (awaiting-external-data)* ──► completed
                       └─► failed
                 ▲
                 └── (resumes on external data arrival)

Signal:
  (constructed by agent) ──[validate schema]──► (rejected: logged)  // FR-015
                                  │
                                  ▼
                          (validated) ──[dedup window check]──► (dropped, metric++) // FR-012
                                  │
                                  ▼
                          (persisted on Blackboard) ──[staleness filter]──► (eligible for consensus)

CriticalFraudAlert:
  emitted only when origin.kind == "investigation"
                  AND signal.confidence ≥ source_agent.confidence_threshold
                  AND signal not stale                                    // FR-008 / FR-033
```
