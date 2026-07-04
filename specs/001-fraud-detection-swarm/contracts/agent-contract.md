# Contract: Standard Agent Contract (FR-003, FR-010)

**Contract type**: Python Protocol implemented by every agent class.
**Goal**: Adding a new agent requires zero modifications to the Blackboard, the Consensus Agent, or any other agent class.

## Interface

```python
from typing import Protocol, runtime_checkable
from holmes_swarm.blackboard.schema import Signal
from holmes_swarm.investigations.models import InvestigationScope

@runtime_checkable
class Agent(Protocol):
    id: str
    name: str
    signal_type: Literal["financial", "physical", "clinical", "operational"]
    confidence_threshold: float

    async def run(self, batch: Batch, *, scope: InvestigationScope | None = None) -> list[Signal]:
        """
        Process a batch of source data and produce zero or more Signals.

        - `scope=None` ⇒ signals MUST be emitted with origin `autonomous-monitoring`.
        - `scope=<InvestigationScope>` ⇒ signals MUST be emitted with origin
          `investigation:<scope.investigation_request_id>` and MUST NOT include
          any autonomous-origin signals in this call (FR-032).
        """
        ...

    async def shutdown(self) -> None: ...
```

`Batch` is an opaque source-specific payload (e.g. a list of contract dicts for `ContractingAgent`, attendance log rows for `LogisticsAgent`). The Blackboard does not interpret it.

## Registration

```python
from holmes_swarm.agents.registry import AgentRegistry

registry = AgentRegistry()
registry.register(MyNewAgent(...))
```

Registration:
- Adds the agent's id to the Blackboard topic list (its `signal_type`).
- Wires the agent to the bus so its published signals carry `source_agent = self.id`.
- Supplies an allow-listed HTTP client (or `None`) per the agent's `InternetProfile`.

## Lifecycle

1. `agent.setup()` is called once at startup (loads models, builds indexes).
2. `agent.run(batch, scope=...)` is called per ingestion cycle / per investigation.
3. `agent.shutdown()` is called on graceful shutdown (release indexes, flush logs).

## Failure handling (FR-014, FR-020)

- An exception inside `run(batch)` is caught by the swarm runner; the failure is logged, the agent's metrics are incremented (`agents.failures`), and the swarm continues. Other agents are unaffected.
- An outbound HTTP call to a non-allow-listed host raises `BlockedHostError` → caught and logged as a security event; the agent's run continues with the affected batch skipped (FR-018 / FR-020).

## What agents MUST NOT do

- Communicate directly with other agents. All inter-agent signalling goes through the Blackboard.
- Emit signals of a `signal_type` they did not declare.
- Bypass the Blackboard's validation/dedup pipeline (no "private" signal pathway; FR-027).
- Use unrestricted internet access (FR-019). Agents without `InternetProfile = none` only get the allow-listed client.

## Adding a new agent (the test that proves SC-002 / FR-010)

```python
class BedOccupancyAuditor:
    id = "bed_occupancy"
    name = "Bed Occupancy Auditor"
    signal_type = "operational"
    confidence_threshold = 0.75

    async def run(self, batch, *, scope=None): ...
    async def shutdown(self): ...

registry.register(BedOccupancyAuditor(...))
```

No edits to `Blackboard`, `ConsensusAgent`, or any existing agent are required.
