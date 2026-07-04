# Contract: Signal Schema

**Contract type**: JSON / Pydantic model published on the Blackboard.
**Owner**: The system (Blackboard). All agents MUST conform.

## Topology

- One `Blackboard` per process. Topics are signal-type strings (`financial`, `physical`, `clinical`, `operational`). A topic also exists `alerts` for emitted `CriticalFraudAlert` events.
- Each consumer subscribes to one or more topics; each gets its own `asyncio.Queue`.

## JSON shape (Signal)

```json
{
  "id": "9c6f1d3e-1f4d-4d2a-9b1c-1c4e6f8a9b10",
  "entity_id": "900123456-7",
  "signal_type": "financial",
  "source_agent": "contracting",
  "confidence": 0.87,
  "evidence": { "contract_id": "C-12345", "platform": "SECOP" },
  "emitted_at": "2026-06-24T20:00:00Z",
  "origin": { "kind": "autonomous-monitoring" },
  "below_threshold": false
}
```

`origin` examples:

```json
{ "kind": "autonomous-monitoring" }
```

```json
{ "kind": "investigation", "investigation_request_id": "7a5b6f12-9e10-4d3a-8c1b-2a0b1f3d4e5a" }
```

## Field rules (enforced at publish time)

| Field           | Rule                                                                |
|-----------------|---------------------------------------------------------------------|
| `entity_id`     | Non-empty string.                                                   |
| `signal_type`   | Must be one of the four declared literals.                          |
| `source_agent`  | Must equal the id of the registered publishing agent.               |
| `confidence`    | `0.0 ≤ c ≤ 1.0`. Out-of-range → reject.                              |
| `evidence`      | Object (any shape). Blackboard does not interpret.                  |
| `emitted_at`    | Server-assigned UTC timestamp; agent-supplied value is ignored.     |
| `origin`        | MUST be one of the two shapes above; missing/unknown → reject (FR-015, FR-031). |
| `below_threshold` | Computed server-side from `confidence` vs. source agent's threshold. |

## Validation outcomes

- **Valid**: signal is published to the topic; dedup logic runs; if it survives, it is persisted.
- **Validation error**: signal is rejected with `PublishRejected(reason="validation")`; the agent's `publish` call raises; the failure is logged with signal id (None if missing) and reason. The agent's run continues.

## Dedup rule (FR-012)

Dedup key = `(entity_id, source_agent, signal_type, time_bucket)`.

- `time_bucket` = floor(`emitted_at` / `dedup_window_seconds`).
- Only the first signal with a given key within the same bucket survives. Subsequent are dropped and counted in `metrics.signals_dropped_dedup`.
- A dropped dedup MUST NOT cause a duplicate Critical Fraud Alert (consensus dedup is shared).

## Staleness rule (FR-013)

A signal is stale when `now - emitted_at > config.staleness_window`. Stale signals are excluded from consensus evaluation but remain queryable (FR-035).

## Error responses (returned by `Blackboard.publish`)

```python
class PublishRejected(Exception):
    def __init__(self, reason: Literal["validation", "duplicate_dropped", "rate_limited"]): ...

class BlockedHostError(Exception):
    """Raised by allow-listed httpx client when an agent targets a non-allow-listed host."""
```

## Versioning

- Schema version lives on the Signal envelope as `"schema_version": "1"` (default for v1).
- Blackboard accepts only the current schema version; future versions are additive-only and announced with a deprecation window.
