# Quickstart: Healthcare Fraud Detection Swarm

This guide validates the swarm end-to-end on a clean checkout. It maps to **SC-001** (working prototype in ≤ 10 minutes) and **SC-005…SC-014**.

## Prerequisites

- Python 3.11+
- `pip` / `uv` / `poetry` (any one)
- Optional: a real LLM API key. Without one, the swarm runs in **mock-LLM mode** (offline; FR-022).

## 1. Install

```bash
git clone <repo-url> holmes-swarm
cd holmes-swarm
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## 2. Configure

Copy the example config and edit it:

```bash
cp config/example.yml config/local.yml
```

Default v1 config:

```yaml
llm:
  provider: mock          # change to "minimax" to use a real provider
  model: MiniMax-M3
  api_base: https://api.minimax.example/v1
  api_key_env: MINIMAX_API_KEY

agents:
  contracting:
    enabled: true
    confidence_threshold: 0.7
    internet_profile: { kind: public_ro, allowed_hosts: ["^secoop(?:\\..*)?$", "^api\\.secop\\.gov\\.co$"] }
  logistics:
    enabled: true
    confidence_threshold: 0.6
    internet_profile: { kind: public_ro, allowed_hosts: ["^router\\.project-osrm\\.org$", "^api\\.openrouteservice\\.org$"] }
  medical:
    enabled: true
    confidence_threshold: 0.75
    internet_profile: { kind: none }
  whistleblower:
    enabled: true
    confidence_threshold: 0.6
    internet_profile: { kind: llm_only, llm_endpoint: "${llm.api_base}" }

blackboard:
  dedup_window_seconds: 60
  staleness_window_seconds: 86400

investigations:
  default_timeout_seconds: 300
  consensus_evaluation_interval_seconds: 60
```

The example config sets `provider: mock` so the swarm runs fully offline. To switch to the real first-target model, set:

```yaml
llm:
  provider: minimax
  model: MiniMax-M3
  api_base: https://api.minimax.example/v1
```

…and export `MINIMAX_API_KEY=<your-key>`.

## 3. Run

Start the API:

```bash
uvicorn holmes_swarm.api.app:app --host 127.0.0.1 --port 8000
```

Or use the CLI:

```bash
holmes-swarm run --config config/local.yml
```

## 4. Validation scenarios

Each scenario below maps to a Success Criterion in the spec. Steps use the running API at `http://127.0.0.1:8000` with the seeded bearer token in `config/auth.yml`.

### Scenario A — End-to-end autonomous + investigation (SC-001, SC-003, SC-010)

1. Seed sample data:
   ```bash
   holmes-swarm seed --fixtures tests/fixtures/
   ```
2. Wait for the autonomous cycle (≤ 30s). Verify signals on the Blackboard:
   ```bash
   curl -H "Authorization: Bearer $TOKEN" \
        "http://127.0.0.1:8000/signals?entity_id=900123456-7"
   ```
   Expected: ≥ 1 signal per data domain, each with `origin.kind = "autonomous-monitoring"`.
3. Verify **no** alert was emitted:
   ```bash
   curl -H "Authorization: Bearer $TOKEN" \
        "http://127.0.0.1:8000/alerts?entity_id=900123456-7"
   ```
   Expected: empty list (SC-013).
4. Submit an investigation:
   ```bash
   curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
        -d '{"target_entity_id":"900123456-7","agents":["contracting","medical"]}' \
        http://127.0.0.1:8000/investigations
   ```
5. Poll status until `completed`:
   ```bash
   curl -H "Authorization: Bearer $TOKEN" \
        http://127.0.0.1:8000/investigations/<request_id>
   ```
6. Verify the alert:
   ```bash
   curl -H "Authorization: Bearer $TOKEN" \
        http://127.0.0.1:8000/alerts?entity_id=900123456-7
   ```
   Expected: ≥ 1 Critical Fraud Alert referencing the Investigation Request id (SC-004 / SC-014).

### Scenario B — Autonomous signals never trigger alerts (SC-013)

Run:

```bash
holmes-swarm seed --fixtures tests/fixtures/ --autonomous-flood 1000 --entity 900123456-7
holmes-swarm wait --seconds 120
holmes-swarm alerts --entity 900123456-7
```

Expected: zero alerts.

### Scenario C — Source outage (SC-005)

```bash
holmes-swarm simulate-outage secop 60   # 60s simulated outage
holmes-swarm status                     # other agents still publish
```

Expected: only `contracting` errors in the log; the other three agents continue producing signals.

### Scenario D — Plugin agent (SC-002 / FR-010)

Add `examples/bed_occupancy_agent.py` (a one-file agent implementing the `Agent` Protocol) to the registry:

```python
from holmes_swarm.agents.registry import registry
from examples.bed_occupancy_agent import BedOccupancyAuditor

registry.register(BedOccupancyAuditor())
holmes_swarm.run()
```

Expected: `bed_occupancy` produces `operational` signals on the Blackboard; the Consensus Agent handles them like any other; no edits to `Blackboard`, `ConsensusAgent`, or any existing agent.

### Scenario E — Unauthorised request (SC-012)

```bash
curl -X POST http://127.0.0.1:8000/investigations \
     -H "Content-Type: application/json" \
     -d '{"target_entity_id":"900123456-7"}'
```

Expected: `401` (or `403`); no signals produced; no audit log entry with `action=investigation.submit` (FR-029 / FR-030).

### Scenario F — Audit trail (SC-011)

```bash
holmes-swarm audit-log --since "$(date -u -d '1 hour ago' '+%Y-%m-%dT%H:%M:%SZ')"
```

Expected: every Investigation Request appears with `actor`, `target_entity_id`, `request_id`, and `report_id` once complete.

### Scenario G — Internet allow-list (FR-018)

```bash
holmes-swarm simulate-call contracting https://evil.example.com/contracts
```

Expected: `BlockedHostError` raised, logged as a security event; the swarm continues.

## 5. Where to read next

- Architecture: `README.md` (Mermaid diagram of the Blackboard + agents + consensus pipeline).
- Signal schema: `specs/001-fraud-detection-swarm/contracts/signal-schema.md`.
- Agent contract: `specs/001-fraud-detection-swarm/contracts/agent-contract.md`.
- Investigation API: `specs/001-fraud-detection-swarm/contracts/investigation-api.md`.
- Data model: `specs/001-fraud-detection-swarm/data-model.md`.
