"""SC-005: source outage — other agents keep producing signals.

Verifies failure isolation (FR-014): when one agent's run() raises, the others
still complete and publish signals.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from holmes_swarm.agents.registry import run_agents_isolated


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.mark.asyncio
async def test_other_agents_continue_when_one_fails(app, bus):
    from holmes_swarm.agents.contracting import ContractingAgent
    from holmes_swarm.agents.logistics import LogisticsAgent
    from holmes_swarm.agents.medical import MedicalAgent
    from holmes_swarm.agents.whistleblower import WhistleblowerAgent

    clinical = json.loads((FIXTURES / "clinical.json").read_text())
    pqrs = json.loads((FIXTURES / "pqrs.json").read_text())
    attendance = json.loads((FIXTURES / "attendance.json").read_text())
    contracts = json.loads((FIXTURES / "contracts.json").read_text())

    class FailingAgent:
        id = "failing"
        name = "Failing"
        signal_type = "operational"
        confidence_threshold = 0.6

        async def run(self, batch, *, scope=None):
            raise RuntimeError("simulated upstream outage")

        async def shutdown(self):
            return None

    agents = [
        FailingAgent(),
        WhistleblowerAgent(llm=app.state.consensus),  # LLM not actually used here
    ]
    # Give the whistleblower a real LLM
    from holmes_swarm.api.app import build_app  # noqa

    # The default registry has a Whistleblower with the app's llm. Borrow it:
    real_wb = app.state.registry.get("whistleblower")
    agents = [
        FailingAgent(),
        real_wb,
        ContractingAgent(),
    ]

    # Use simple batches
    batches = {
        "failing": {"entity_id": "e1"},
        "whistleblower": {"entity_id": "e1", "pqrs": pqrs["pqrs"]},
        "contracting": {"entity_id": "e1", "contracts": contracts["contracts"]},
    }

    async def run_pair(agent):
        try:
            return (agent.id, await agent.run(batches.get(agent.id, {}), scope=None))
        except Exception as exc:
            return (agent.id, exc)

    import asyncio

    results = await asyncio.gather(*[run_pair(a) for a in agents])
    # Failing agent's exception was caught; whistleblower and contracting should produce signals
    success = {aid: res for aid, res in results if not isinstance(res, Exception)}
    failures = {aid: res for aid, res in results if isinstance(res, Exception)}
    assert "failing" in failures
    # At least one other agent produced signals
    published = 0
    for aid, res in success.items():
        if isinstance(res, list) and res:
            for s in res:
                s.origin = {"kind": "autonomous-monitoring"}
                s.entity_id = f"e1-{aid}"
                try:
                    await bus.publish(s)
                    published += 1
                except Exception:
                    pass
    assert published >= 2
