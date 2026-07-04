"""Detection-improvement test case for the Cartel de la Cardiología scenario.

This file documents and exercises **proposed improvements** to the swarm's
detection capability. Each test:

  1. Demonstrates a gap in the *current* detection (false negatives or
     brittleness) on the cartel dataset.
  2. Demonstrates that a concrete proposed detection enhancement closes
     the gap.

The improvements are codified as opt-in agent extensions (or new rules in
existing agents) so the swarm's behavior remains strictly additive and
deterministic. None of these are enabled by default — they live behind
flags on the agent settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from holmes_swarm.agents._runtime import make_signal as _make_signal
from holmes_swarm.agents.contracting import ContractingAgent
from holmes_swarm.agents.logistics import LogisticsAgent
from holmes_swarm.agents.whistleblower import WhistleblowerAgent
from holmes_swarm.llm.mock_adapter import MockLLMClient

from ._cartel_helpers import load_case, make_secop_source

ENTITY_ID = "800555111-9"

CORPORA_DIR = Path(__file__).resolve().parent.parent / "src" / "holmes_swarm" / "rag" / "corpora"


# ---------------------------------------------------------------------------
# Improvement 1: Cross-entity monopoly detection
# ---------------------------------------------------------------------------
# Gap: today's `ContractingAgent` only checks monopoly *within* an entity
# (≥80% of that entity's contracts are the same procedure). It misses the
# case where a cartel of *several* providers coordinates to dominate the
# market for a given procedure.
#
# Improvement: add a `cross_entity` rule that detects when a small group of
# providers (e.g. 3) accounts for >=80% of the SECOP-integrado market for a
# procedure code, AND one of those providers is the entity under analysis.


@dataclass
class Improvement1Result:
    pattern: str
    gap_demonstrated: bool
    improvement_detects: bool


@pytest.mark.asyncio
async def test_improvement_cross_entity_monopoly_closes_gap(case_app):
    case = load_case()
    secop = make_secop_source()

    # The base agent has no cross-entity rule; verify it misses the cartel.
    contracting_baseline = ContractingAgent(secop_source=secop)
    sigs_baseline = await contracting_baseline.run(
        {"entity_id": ENTITY_ID, "contracts": case["contracts"]}, scope=None
    )
    cross_baseline = [
        s
        for s in sigs_baseline
        if s.evidence.get("pattern") in ("cartel_coordination", "cross_entity_monopoly")
    ]
    assert cross_baseline == [], "baseline should NOT detect cross-entity coordination"

    # The improved agent detects it.
    contracting_v2 = _make_cross_entity_contracting_agent(secop)
    sigs_v2 = await contracting_v2.run(
        {"entity_id": ENTITY_ID, "contracts": case["contracts"]}, scope=None
    )
    cross_v2 = [
        s
        for s in sigs_v2
        if s.evidence.get("pattern") in ("cartel_coordination", "cross_entity_monopoly")
    ]
    assert cross_v2, f"v2 should detect cartel coordination; got {[s.evidence for s in sigs_v2]}"


def _make_cross_entity_contracting_agent(secop) -> ContractingAgent:
    """Subclass adding a cross-entity monopoly rule (Improvement 1)."""
    return _CrossEntityContractingAgent(secop_source=secop, secop_pull_limit=100)


class _CrossEntityContractingAgent(ContractingAgent):
    """Cross-entity cartel-coordination detector.

    Adds: for a given procedure code, if the top-3 providers in the SECOP
    sample cover >=80% of contracts AND one of them is the entity under
    analysis, emit a `cross_entity_monopoly` signal.
    """

    def _make_signal(self, *, entity_id, confidence, evidence, scope):  # shim for LLM-driven base
        return _make_signal(
            agent_id=self.id,
            signal_type=self.signal_type,
            entity_id=entity_id,
            confidence=confidence,
            evidence=evidence,
            scope=scope,
            confidence_threshold=self.confidence_threshold,
        )

    async def run(self, batch: Any, *, scope: Any = None):  # type: ignore[override]  
        from collections import Counter

        signals = await super().run(batch, scope=scope)
        if not isinstance(batch, dict):
            return signals
        contracts = batch.get("contracts") or []
        codes = sorted({str(c.get("code")) for c in contracts if c.get("code")})
        entity_id = batch.get("entity_id") or (
            scope.target_entity_id if scope else None
        )
        for code in codes:
            try:
                sample = self._secop.fetch_for_entity(  # type: ignore[attr-defined]
                    entity_id="*", procedure_code=code, limit=100
                )
            except Exception:
                sample = []
            if len(sample) < 5:
                continue
            counts = Counter(r.entity_id for r in sample)
            top3_share = sum(v for _, v in counts.most_common(3)) / max(
                1, sum(counts.values())
            )
            top_providers = {pid for pid, _ in counts.most_common(3)}
            if (
                top3_share >= 0.8
                and entity_id in top_providers
                and len(sample) >= 5
            ):
                signals.append(
                    self._make_signal(  # type: ignore[attr-defined]
                        entity_id=entity_id,
                        confidence=0.85,
                        evidence={
                            "pattern": "cross_entity_monopoly",
                            "procedure_code": code,
                            "top3_share": round(top3_share, 3),
                            "providers_in_cartel": sorted(top_providers),
                            "sample_size": len(sample),
                        },
                        scope=scope,
                    )
                )
        return signals


# ---------------------------------------------------------------------------
# Improvement 2: LogisticsAgent — provider overlap
# ---------------------------------------------------------------------------
# Gap: the current logistics agent detects impossible *pairwise* movement,
# but it cannot detect the case where a single provider is "in two places
# at once" — i.e. two concurrent (`overlapping in time`) events at
# different locations, which is a strong operational signal in cartel
# stories (the concejal flagged multiple providers signing patient notes
# simultaneously).
#
# Improvement: add a `concurrent_presence` detector that fires when one
# `provider` appears in events whose time intervals overlap but whose
# locations are >= 5 km apart.


@dataclass
class Improvement2Result:
    pattern: str
    gap_demonstrated: bool
    improvement_detects: bool


@pytest.mark.asyncio
async def test_improvement_concurrent_presence_closes_gap(case_app):
    logistics = LogisticsAgent()
    events = [
        # Same provider, overlapping in time, far apart → must NOT trigger
        # the current rule (the agent looks at consecutive pairs only and
        # their delta is positive).
        {
            "ts": "2025-11-04T08:00:00+00:00",
            "location": {"lat": 4.7110, "lon": -74.0721},
            "provider": "med-A",
        },
        {
            "ts": "2025-11-04T08:00:00+00:00",
            "location": {"lat": 4.6530, "lon": -74.0830},
            "provider": "med-A",
        },
    ]
    sigs_baseline = await logistics.run({"entity_id": ENTITY_ID, "events": events}, scope=None)
    concurrent_baseline = [
        s for s in sigs_baseline if s.evidence.get("pattern") == "concurrent_presence"
    ]
    assert concurrent_baseline == [], "baseline must miss concurrent_presence"

    # Improved agent
    logistics_v2 = _make_concurrent_presence_agent()
    sigs_v2 = await logistics_v2.run({"entity_id": ENTITY_ID, "events": events}, scope=None)
    concurrent_v2 = [s for s in sigs_v2 if s.evidence.get("pattern") == "concurrent_presence"]
    assert concurrent_v2, "v2 must detect concurrent_presence"


def _make_concurrent_presence_agent() -> LogisticsAgent:
    """Subclass adding a concurrent-presence detector (Improvement 2)."""
    return _ConcurrentPresenceLogisticsAgent()


class _ConcurrentPresenceLogisticsAgent(LogisticsAgent):

    def _make_signal(self, *, entity_id, confidence, evidence, scope):  # shim for LLM-driven base
        return _make_signal(
            agent_id=self.id,
            signal_type=self.signal_type,
            entity_id=entity_id,
            confidence=confidence,
            evidence=evidence,
            scope=scope,
            confidence_threshold=self.confidence_threshold,
        )

    """Detects a provider being in two places at once (>=5 km, overlapping in time)."""

    async def run(self, batch: Any, *, scope: Any = None):  # type: ignore[override]
        signals = await super().run(batch, scope=scope)
        events = (batch or {}).get("events") or []
        parsed = []
        for e in events:
            try:
                ts = e["ts"]
                if isinstance(ts, str):
                    ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                parsed.append((e.get("provider"), ts, e.get("location") or {}))
            except Exception:
                continue
        by_prov: dict[str, list] = {}
        for p, t, loc in parsed:
            by_prov.setdefault(p or "?", []).append((t, loc))
        for prov, items in by_prov.items():
            items.sort(key=lambda x: x[0])
            for i in range(len(items) - 1):
                t1, loc1 = items[i]
                t2, loc2 = items[i + 1]
                if t2 <= t1:
                    km = self._haversine_km(  # type: ignore[attr-defined]
                        float(loc1.get("lat", 0) or 0),
                        float(loc1.get("lon", 0) or 0),
                        float(loc2.get("lat", 0) or 0),
                        float(loc2.get("lon", 0) or 0),
                    )
                    if km and km >= 5.0:
                        entity_id = (batch or {}).get("entity_id") or (
                            scope.target_entity_id if scope else ""
                        )
                        signals.append(
                            self._make_signal(  # type: ignore[attr-defined]
                                entity_id=entity_id,
                                confidence=0.9,
                                evidence={
                                    "pattern": "concurrent_presence",
                                    "provider": prov,
                                    "from": loc1,
                                    "to": loc2,
                                    "distance_km": round(km, 2),
                                    "timestamp": t1.isoformat(),
                                },
                                scope=scope,
                            )
                        )
        return signals


# ---------------------------------------------------------------------------
# Improvement 3: Whistleblower-agent text boost
# ---------------------------------------------------------------------------
# Gap: a single PQR mentioning "facturas falsas" is critical, but the
# current agent only emits a confidence-0.55 signal unless the LLM marks it
# as negative sentiment. Many cartel PQRs are short and ambiguous in
# sentiment.
#
# Improvement: add a `critical_pattern_boost` that escalates confidence to
# 0.85 when the body contains a known *high-severity* indicator such as
# "facturas falsas", "pago por whatsapp", "remisión coaccionada",
# "paciente referido por tercero".


@pytest.mark.asyncio
async def test_improvement_critical_pattern_boost_closes_gap(case_app):
    baseline_agent = WhistleblowerAgent(llm=MockLLMClient())
    pqr = [
        {
            "id": "PQR-X",
            "text": "Me pidieron un pago por whatsapp para remitir al paciente, "
            "las facturas parecen falsas.",
            "entity_id": ENTITY_ID,
        }
    ]
    baseline = await baseline_agent.run(
        {"entity_id": ENTITY_ID, "pqrs": pqr}, scope=None
    )
    # Baseline cap: 0.7 negative / 0.55 neutral.
    assert baseline, "baseline should emit at least one modus_operandi signal"
    baseline_max = max(s.confidence for s in baseline)
    assert baseline_max <= 0.75, (
        "baseline confidence should not exceed critical boost level"
    )

    whistleblower_v2 = _make_critical_boost_agent()
    boosted = await whistleblower_v2.run({"entity_id": ENTITY_ID, "pqrs": pqr}, scope=None)
    assert boosted, "boosted agent should emit at least one signal"
    boosted_max = max(s.confidence for s in boosted)
    assert boosted_max >= 0.85, f"boosted confidence should be >= 0.85; got {boosted_max}"


def _make_critical_boost_agent() -> WhistleblowerAgent:
    """Subclass that boosts PQR confidence on high-severity patterns (Improvement 3)."""
    return _CriticalPatternWhistleblowerAgent(llm=MockLLMClient())


class _CriticalPatternWhistleblowerAgent(WhistleblowerAgent):
    SEVERE = (
        "facturas falsas",
        "pago por whatsapp",
        "remisión coaccionada",
        "paciente referido por tercero",
        "comisión al médico",
        "cobro extra",
    )

    def _make_signal(self, *, entity_id, confidence, evidence, scope):  # shim for LLM-driven base
        return _make_signal(
            agent_id=self.id,
            signal_type=self.signal_type,
            entity_id=entity_id,
            confidence=confidence,
            evidence=evidence,
            scope=scope,
            confidence_threshold=self.confidence_threshold,
        )

    async def run(self, batch: Any, *, scope: Any = None):  # type: ignore[override]
        signals = await super().run(batch, scope=scope)
        pqrs = (batch or {}).get("pqrs") or []
        for pqr in pqrs:
            text = (pqr.get("body") or pqr.get("text") or "").lower()
            entity_id = (
                pqr.get("entity_id")
                or (batch or {}).get("entity_id")
                or (scope.target_entity_id if scope else "")
            )
            if not entity_id or not text:
                continue
            if any(sev in text for sev in self.SEVERE):
                signals.append(
                    self._make_signal(  # type: ignore[attr-defined]
                        entity_id=entity_id,
                        confidence=0.9,
                        evidence={
                            "pqr_id": pqr.get("id"),
                            "sentiment": "negative",
                            "critical_pattern": next(s for s in self.SEVERE if s in text),
                            "modus_operandi": ["critical_boost"],
                        },
                        scope=scope,
                    )
                )
        return signals


# ---------------------------------------------------------------------------
# Improvement 4: SECOP ranked-percentile audit
# ---------------------------------------------------------------------------
# Gap: the current `ContractingAgent` only flags prices BELOW 50% of the
# reference. This misses contracts that are merely 20-40% under the median
# but have other corroborating risk signals.
#
# Improvement: add a `ranked_percentile` rule that classifies each
# contract by its percentile band, and emits a *low-confidence* early
# warning when a contract is in the 25th-50th percentile (yellow) and a
# *high-confidence* alert only at <25th percentile.


@pytest.mark.asyncio
async def test_improvement_ranked_percentile_closes_gap(case_app):
    secop = make_secop_source()
    agent = _make_ranked_percentile_agent(secop)

    # Use a price that's at ~50% of the bundled reference (which puts it
    # in the 25-50 percentile band given the SECOP snapshot mix).
    sigs = await agent.run(
        {
            "entity_id": ENTITY_ID,
            "contracts": [{"code": "93010", "price": 600000, "platform": "SECOP"}],
        },
        scope=None,
    )
    band = [s for s in sigs if s.evidence.get("pattern") == "ranked_percentile"]
    assert band, f"v2 should emit a ranked-percentile band signal; got {[s.evidence for s in sigs]}"
    e = band[0].evidence
    assert e["band"] in ("yellow", "red"), f"unexpected band {e.get('band')}"
    # And contracts below the 25th percentile are still flagged "red".
    sigs2 = await agent.run(
        {
            "entity_id": ENTITY_ID,
            "contracts": [{"code": "93010", "price": 200000, "platform": "SECOP"}],
        },
        scope=None,
    )
    band2 = [s for s in sigs2 if s.evidence.get("pattern") == "ranked_percentile"]
    assert any(s.evidence.get("band") == "red" for s in band2), "extreme low price should be red"


def _make_ranked_percentile_agent(secop) -> ContractingAgent:
    """Subclass adding yellow/red percentile-band signals (Improvement 4)."""
    return _RankedPercentileContractingAgent(secop_source=secop, secop_pull_limit=100)


class _RankedPercentileContractingAgent(ContractingAgent):
    """Classifies each contract into a yellow/red percentile band vs SECOP market."""

    def _make_signal(self, *, entity_id, confidence, evidence, scope):  # shim for LLM-driven base
        return _make_signal(
            agent_id=self.id,
            signal_type=self.signal_type,
            entity_id=entity_id,
            confidence=confidence,
            evidence=evidence,
            scope=scope,
            confidence_threshold=self.confidence_threshold,
        )


    async def run(self, batch: Any, *, scope: Any = None):  # type: ignore[override]
        signals = await super().run(batch, scope=scope)
        contracts = (batch or {}).get("contracts") or []
        for c in contracts:
            code = str(c.get("code", ""))
            price = float(c.get("price", 0) or 0)
            if (
                not code
                or price <= 0
                or self._secop is None  # type: ignore[attr-defined]
            ):
                continue
            sample = self._secop.fetch_for_entity(  # type: ignore[attr-defined]
                entity_id="*", procedure_code=code, limit=100
            )
            prices = sorted(r.price for r in sample if r.price > 0)
            if len(prices) < 5:
                continue
            below = sum(1 for p in prices if p < price)
            rank = below / len(prices)
            if rank < 0.25:
                band = "red"
                confidence = 0.9
            elif rank < 0.50:
                band = "yellow"
                confidence = 0.55
            else:
                continue
            entity_id = (batch or {}).get("entity_id") or (
                scope.target_entity_id if scope else ""
            )
            signals.append(
                self._make_signal(  # type: ignore[attr-defined]
                    entity_id=entity_id,
                    confidence=confidence,
                    evidence={
                        "pattern": "ranked_percentile",
                        "procedure_code": code,
                        "price": price,
                        "band": band,
                        "percentile_rank": round(rank, 3),
                        "market_size": len(prices),
                    },
                    scope=scope,
                )
            )
        return signals


# ---------------------------------------------------------------------------
# Improvement summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_improvement_summary_table(case_app):
    """Compact summary of the four proposed improvements."""
    improvements = [
        (
            "cross_entity_monopoly",
            "contracting",
            "Top-3 providers cover >=80% of a procedure AND the entity is in the top-3.",
        ),
        (
            "concurrent_presence",
            "logistics",
            "A single provider appears in overlapping events at locations >=5km apart.",
        ),
        (
            "critical_pattern_boost",
            "whistleblower",
            "PQR body matches a high-severity indicator (e.g. 'facturas falsas').",
        ),
        (
            "ranked_percentile",
            "contracting",
            "Each contract is classified into a yellow/red percentile band vs SECOP market.",
        ),
    ]
    print("\n--- Proposed detection improvements ---")
    print(f"{'pattern':<28}{'agent':<14}rule")
    for p, a, rule in improvements:
        print(f"{p:<28}{a:<14}{rule}")
    assert len(improvements) == 4
