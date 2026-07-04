# Contract: Investigation HTTP API (FR-024…FR-030)

**Contract type**: HTTP/JSON (FastAPI).
**Auth**: Bearer token (token → requester id mapping is loaded from `config/auth.yml`).

## Endpoints

### `POST /investigations`

Submit a new Investigation Request (FR-024).

Request:

```json
{
  "target_entity_id": "900123456-7",
  "agents": ["contracting", "logistics"],
  "scope": {
    "date_from": "2026-01-01",
    "date_to": "2026-06-01",
    "location": "Bogotá",
    "procedure": "cateterismo",
    "narrative": "Sospecha de uso indebido de mensajería"
  }
}
```

- `agents` is optional; `null`/omitted ⇒ all enabled agents (US2.AS3).
- `scope.*` fields are optional.

Response `202 Accepted`:

```json
{
  "request_id": "7a5b6f12-9e10-4d3a-8c1b-2a0b1f3d4e5a",
  "state": "queued",
  "status_url": "/investigations/7a5b6f12-9e10-4d3a-8c1b-2a0b1f3d4e5a"
}
```

Errors:
- `401 Unauthorized` — missing/invalid token.
- `403 Forbidden` — token is not authorised for this action.
- `422 Unprocessable Entity` — schema violation (e.g. invalid entity id).

### `GET /investigations/{request_id}`

Returns current state + (if `completed`/`failed`) the report reference (FR-026).

```json
{
  "request_id": "7a5b6f12-...",
  "state": "completed",
  "agents_ran": ["contracting", "logistics"],
  "report_id": "f1c4b5e2-...",
  "report_url": "/investigations/7a5b6f12-.../report"
}
```

### `GET /investigations/{request_id}/report`

Returns the full Investigation Report (FR-025).

```json
{
  "id": "f1c4b5e2-...",
  "request_id": "7a5b6f12-...",
  "target_entity_id": "900123456-7",
  "agents_ran": ["contracting", "logistics"],
  "signal_ids": ["9c6f1d3e-...", "5a01bd11-..."],
  "summary": "Se detectó un contrato por debajo del percentil 5 histórico...",
  "emitted_at": "2026-06-24T20:05:11Z"
}
```

### `GET /signals`

Query the Blackboard (FR-035). All filters are optional.

Query params:
- `entity_id` — filter by entity.
- `origin_kind` — `"autonomous-monitoring"` or `"investigation"`.
- `investigation_request_id` — filter to a specific investigation's signals.
- `since`, `until` — ISO-8601 timestamps.
- `limit`, `offset` — pagination.

Response:

```json
{
  "items": [ { /* Signal */ }, ... ],
  "next_offset": 240
}
```

### `GET /alerts`

List Critical Fraud Alerts.

Response:

```json
{
  "items": [
    {
      "id": "...",
      "entity_id": "900123456-7",
      "emitted_at": "2026-06-24T20:05:12Z",
      "investigation_request_id": "7a5b6f12-...",
      "contributing_signal_ids": ["9c6f1d3e-..."],
      "contributing_agent_ids": ["contracting"],
      "summary": "..."
    }
  ]
}
```

### `GET /alerts/{alert_id}`

Full alert with every contributing signal's evidence.

## Audit logging (FR-030, FR-011)

Every `POST /investigations` writes an `AuditLogEntry`:

```json
{
  "id": "...",
  "at": "2026-06-24T20:00:00Z",
  "actor": "user:esteban",
  "action": "investigation.submit",
  "target_entity_id": "900123456-7",
  "request_id": "7a5b6f12-...",
  "report_id": null
}
```

A second entry is written on completion with `action = investigation.complete` and the resolved `report_id`.

## Authentication & authorization (FR-029)

- Bearer token in `Authorization` header.
- Token → requester id mapping loaded from `config/auth.yml` at startup.
- Unauthorised requests return `401` / `403` and **produce zero signals** (SC-012).
- Audit log entry for `investigation.submit` is written **only after authn/z succeeds**.

## Rate limiting & timeouts

- Investigation default timeout: **5 minutes** (configurable). On timeout, the request transitions to `failed` with `summary` describing which agents succeeded and which were still running. Signals already produced remain on the Blackboard with origin `investigation:<request_id>` and may still emit alerts (spec Edge Case).
- Per-token rate limit: configurable; default 10 investigations/minute.
