"""AgentRegistry — runtime registration (FR-010).

Adding a new agent requires zero modifications to Blackboard, Consensus Agent, or
any other agent class.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from ..blackboard.queue_bus import QueueBus
from .base import Agent


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}

    def register(self, agent: Agent) -> None:
        if agent.id in self._agents:
            raise ValueError(f"agent already registered: {agent.id}")
        self._agents[agent.id] = agent

    def unregister(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)

    def get(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    def all(self) -> list[Agent]:
        return list(self._agents.values())

    def ids(self) -> Iterable[str]:
        return self._agents.keys()

    async def shutdown_all(self) -> None:
        for a in self._agents.values():
            try:
                await a.shutdown()
            except Exception:
                pass


async def run_agents_isolated(
    agents: Iterable[Agent],
    *,
    bus: QueueBus,
    batch: Any,
    scope: Any = None,
) -> list[Any]:
    """Run each agent in its own asyncio.Task; failures are caught (FR-014).

    Returns a list of `(agent_id, signals_or_exc)` tuples.
    """
    results: list[Any] = []

    async def _one(agent: Agent) -> Any:
        return (agent.id, await agent.run(batch, scope=scope))

    tasks = [asyncio.create_task(_one(a)) for a in agents]
    for t in asyncio.as_completed(tasks):
        try:
            results.append(await t)
        except Exception as exc:
            results.append(("unknown", exc))
    return results
