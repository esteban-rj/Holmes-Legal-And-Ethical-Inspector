# Feature Specification: Healthcare Fraud Detection Swarm

**Feature Branch**: `001-fraud-detection-swarm`

**Created**: 2026-06-24

**Status**: Draft

**Input**: User description: "Desarrollar la arquitectura base y el código de un sistema 'Swarm' (Enjambre) diseñado para detectar fraudes en la contratación de salud pública (inspirado en el caso del 'Cartel de la Cardiología' en Bogotá). La arquitectura debe ser descentralizada, utilizando el patrón 'Blackboard' (Pizarrón) donde los agentes no se comunican directamente, sino que leen y escriben 'señales' en un entorno compartido. Debe ser limpia, modular (SOLID) y permitir la fácil integración de nuevos agentes en el futuro."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Continuous Multi-Source Monitoring (Priority: P1)

As a public health oversight authority (supervisor / regulator), I need the system to continuously and autonomously ingest data from multiple sources — public contracting platforms, hospital attendance/billing records, medical tariff manuals, and complaint channels — so that early fraud indicators are detected without manual coordination between teams.

**Why this priority**: This is the foundation. Without ingesting signals from the heterogeneous data sources, no fraud detection is possible. It is the minimum viable slice that proves the Blackboard + multi-agent pattern works end-to-end.

**Independent Test**: Can be fully tested by seeding sample contracts, attendance logs, clinical records and PQRs (anonymous complaints) into the platform; the swarm processes them and produces at least one signal per data domain.

**Acceptance Scenarios**:

1. **Given** the swarm is running with the Blackboard enabled, **When** a new public contract is ingested from a contracting source, **Then** the Contracting Agent publishes a financial signal on the Blackboard with confidence and evidence attached.
2. **Given** attendance logs for a provider contain two records at distant hospitals within an impossible time window, **When** the Logistics Agent processes the batch, **Then** a physical-impossibility signal is published on the Blackboard.
3. **Given** a provider bills procedures whose volume or specialty profile is clinically implausible, **When** the Medical Agent runs its coherence checks, **Then** a clinical signal is published.
4. **Given** an anonymous complaint channel contains text describing a modus operandi (e.g. use of messaging apps, use of auxiliaries), **When** the Whistleblower Agent processes the complaint, **Then** an operational signal is published identifying the entity and the extracted modus operandi.

---

### User Story 2 - Cross-Agent Consensus & Critical Alert Generation (Priority: P1)

As an investigator, I need the system to automatically compile evidence from multiple, independent agents and raise a critical fraud alert when an entity accumulates corroborating signals — so that I only review cases that already have multi-source backing, reducing false positives.

**Why this priority**: A single-source signal is too noisy to be actionable. Consensus across independent agents is the core value proposition of the swarm and the primary deliverable for the investigator workflow.

**Independent Test**: Can be fully tested by publishing synthetic signals for one entity (covering ≥3 distinct signal types, each above threshold) on the Blackboard and verifying that a Critical Fraud Alert is emitted.

**Acceptance Scenarios**:

1. **Given** an entity (e.g. provider tax ID or licensed professional) has signals from at least 3 distinct agent types that each exceed the configured confidence threshold, **When** the Consensus Agent observes the Blackboard, **Then** a Critical Fraud Alert is published containing the compiled evidence and the list of contributing agents.
2. **Given** an entity has signals from only 2 distinct agent types, **When** the Consensus Agent runs, **Then** no Critical Fraud Alert is emitted and the state remains at "pending consensus".
3. **Given** an entity has signals from ≥3 agents but at least one of them is below the configured confidence threshold, **When** the Consensus Agent runs, **Then** only qualifying signals are counted toward consensus and the alert (if emitted) reflects the filtered evidence.

---

### User Story 3 - Adding a New Detection Agent Without Core Changes (Priority: P2)

As a system maintainer, I need to be able to add a new specialized agent (e.g. a bed-occupancy auditor) by dropping in a new module — without modifying the Blackboard, the Consensus Agent or any other existing agent — so that detection capabilities can evolve incrementally.

**Why this priority**: Extensibility is the explicit architectural requirement (SOLID — Open/Closed Principle). It must be verifiable, otherwise future agents risk coupling and regressions.

**Independent Test**: Can be fully tested by introducing a new agent class that follows the standard agent contract, registering it with the Blackboard, and verifying it begins producing signals consumed by the Consensus Agent without any edit to existing core code.

**Acceptance Scenarios**:

1. **Given** the swarm is running, **When** a maintainer adds a new agent module implementing the standard agent contract and registers it, **Then** the new agent starts publishing signals on the Blackboard automatically.
2. **Given** the new agent is registered, **When** it emits a qualifying signal, **Then** the Consensus Agent counts it toward consensus exactly like any other agent (no special-casing).

---

### User Story 4 - Evidence Traceability & Audit Trail (Priority: P2)

As an auditor / prosecutor, I need every Critical Fraud Alert to be reproducible — i.e. I can see exactly which signals from which agents led to it, with their original evidence — so that the alert can be defended in legal or disciplinary proceedings.

**Why this priority**: Healthcare fraud cases often end in legal action. Without traceable evidence, alerts are not actionable.

**Independent Test**: Can be fully tested by triggering a Critical Fraud Alert from known synthetic signals and verifying the alert payload contains every contributing signal and its evidence reference.

**Acceptance Scenarios**:

1. **Given** a Critical Fraud Alert has been emitted, **When** the auditor inspects it, **Then** the alert contains: target entity identifier, timestamp of emission, list of contributing agents, and the original signal evidence for each.
2. **Given** a Critical Fraud Alert has been emitted, **When** the auditor requests the full signal log for the entity, **Then** every signal that contributed (and any that did not) can be retrieved, with timestamps.

---

### User Story 5 - Configurable Confidence & Consensus Threshold (Priority: P3)

As an operations lead, I need to tune the confidence thresholds per agent type and the minimum distinct-agent count for consensus — so that the system's sensitivity can be adjusted without redeploying code.

**Why this priority**: Useful in production but not blocking the MVP. Reasonable defaults should ship.

**Independent Test**: Can be fully tested by changing configuration values (via file/env), restarting the swarm, and verifying the new thresholds take effect.

**Acceptance Scenarios**:

1. **Given** the configuration defines a per-agent-type confidence threshold, **When** an agent publishes a signal, **Then** signals below threshold are still stored but flagged as "below threshold" and do not count toward consensus.
2. **Given** the configuration defines the minimum distinct agents required for consensus, **When** the Consensus Agent runs, **Then** it uses the configured value.

---

### Edge Cases

- **What happens when an external data source is unavailable** (e.g. SECOP API down)? The corresponding agent should log the failure, skip the batch, and not crash the swarm. Other agents continue operating.
- **What happens when the same entity receives duplicate signals** from the same agent? The Blackboard should deduplicate (by entity + agent + signal-type + short time window) so a single noisy batch cannot trigger consensus alone.
- **What happens when an agent produces a malformed signal**? The Blackboard should reject it with a validation error and log it; other agents are unaffected.
- **What happens when an entity has signals spanning a very long time**? Old signals (older than a configurable staleness window) should not contribute to new consensus decisions, to avoid stale evidence triggering alerts.
- **What happens when two entities are similarly named** (e.g. "Hospital San José S.A." vs "Clínica San José")? Entity resolution must rely on a unique identifier (e.g. tax ID / professional license), not on name matching.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST provide a shared Blackboard / Event Bus through which agents publish and observe structured signals; agents MUST NOT communicate directly with each other.
- **FR-002**: Every signal published on the Blackboard MUST conform to a documented schema that includes: target entity identifier, signal type, source agent, confidence score (0.0–1.0), supporting evidence reference, and emission timestamp.
- **FR-003**: The system MUST provide a common base contract for agents that handles Blackboard subscription and the execution loop; concrete agents MUST only implement detection-specific behaviour.
- **FR-004**: The system MUST include a Contracting Agent that detects monopolistic contracting patterns and abnormally low prices compared to a reference price history, emitting Financial Signals.
- **FR-005**: The system MUST include a Logistics Agent that detects physically impossible movement patterns (e.g. same provider at two distant locations within an infeasible travel time) using a travel-time source, emitting Physical Signals.
- **FR-006**: The system MUST include a Medical Agent that detects clinically implausible billing (volume, specialty mismatch, procedure profile) using a medical-tariff and guidelines knowledge base, emitting Clinical Signals.
- **FR-007**: The system MUST include a Whistleblower Agent that ingests anonymous complaints (PQRs) and extracts entities plus a modus operandi description using language analysis, emitting Operational Signals.
- **FR-008**: The system MUST include a Consensus Agent that, whenever an entity accumulates signals from at least N distinct agent types (configurable; default = 3) each above the configured confidence threshold, emits a Critical Fraud Alert aggregating the evidence.
- **FR-009**: Critical Fraud Alerts MUST be reproducible: each alert MUST reference the contributing signals, the contributing agents, and the entity identifier.
- **FR-010**: The system MUST allow registering new agent types at startup (or runtime, when supported) without modifications to the Blackboard, the Consensus Agent, or other existing agents.
- **FR-011**: The system MUST provide per-agent confidence thresholds and a configurable minimum distinct-agent count for consensus, applied at runtime from configuration.
- **FR-012**: The system MUST deduplicate near-simultaneous signals from the same agent for the same entity and signal type within a configurable time window.
- **FR-013**: The system MUST exclude signals older than a configurable staleness window from consensus decisions.
- **FR-014**: The system MUST continue operating when a single data source or single agent is unavailable, logging the failure without crashing the swarm.
- **FR-015**: The system MUST validate every signal against the schema at publish time and reject malformed ones with a logged validation error.
- **FR-016**: The system MUST provide a documented architecture diagram (Mermaid) and a README explaining how to install, configure, and run the swarm.

### Key Entities *(include if feature involves data)*

- **Signal**: A single observation emitted by an agent. Attributes: unique id, target entity (foreign key to Entity), signal type (Financial / Physical / Clinical / Operational), source agent id, confidence (0.0–1.0), evidence reference (pointer to underlying data: contract id, log row id, document excerpt, etc.), emitted at (timestamp).
- **Entity**: The subject being investigated. Attributes: unique identifier (tax ID for providers, professional license for individuals), entity type (provider, individual professional), display name.
- **Critical Fraud Alert**: A consolidated finding produced by the Consensus Agent. Attributes: id, target entity, emitted at, list of contributing signal ids, list of contributing agent ids, summary.
- **Agent**: A detector in the swarm. Attributes: id, name, signal type it produces, enabled flag, confidence threshold.
- **Evidence Reference**: A pointer to underlying source material (e.g. contract id, attendance log id, document excerpt, page reference to a tariff manual). Treated as opaque payload by the Blackboard; the originating agent owns its interpretation.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A working prototype of the swarm can be brought up from a clean checkout and demonstrate end-to-end detection — i.e. ingest sample data from at least 4 sources, produce signals from all 4 corresponding agents, and emit at least one Critical Fraud Alert — within 10 minutes of following the README.
- **SC-002**: Adding a new agent type (concrete class implementing the standard agent contract) requires zero modifications to the Blackboard, the Consensus Agent, or any other existing agent class. The new agent's signals count toward consensus the same as any other.
- **SC-003**: For an entity that has accumulated signals from ≥3 distinct agent types above threshold, a Critical Fraud Alert is produced within a configurable evaluation interval (default ≤ 60 seconds) of the qualifying signal being published.
- **SC-004**: 100% of emitted Critical Fraud Alerts include, in their payload, the full list of contributing signal ids, contributing agent ids, and the entity identifier — verifiable by inspection of the alert.
- **SC-005**: When any single external data source becomes unavailable (simulated), the swarm continues to operate and other agents continue producing signals — verifiable by an integration test that disables one source and asserts the others still publish.
- **SC-006**: Malformed signals (missing required fields, out-of-range confidence, etc.) are rejected at publish time without crashing the publisher agent and without contaminating the Blackboard — verifiable by a unit test on the publish API.
- **SC-007**: The README, Mermaid architecture diagram, and module-level documentation together allow a new developer to add a new agent type without reading implementation code of existing agents — verifiable by a written walk-through in the README.
- **SC-008**: Configuration changes (per-agent thresholds, consensus count, staleness window) take effect without code changes — verifiable by changing configuration values and observing different consensus behaviour.

## Assumptions

- **Target users**: Public oversight authorities, internal compliance / fraud-investigation teams at health insurers or EPS, and audit bodies. End-user is not the general public.
- **Data sources for v1 prototype**: Public contracting data (e.g. SECOP-style mock data), synthetic attendance/billing logs, a small embedded tariff/guidelines reference, and synthetic PQRs. Real external integrations are out of scope for v1.
- **Deployment shape for v1**: Single-process prototype using an in-memory Blackboard. The Blackboard interface MUST be implementable over an external message broker later (Redis/Kafka/RabbitMQ) without changing agent code.
- **Entity identification**: A unique stable identifier (tax ID / professional license) is available for every entity referenced by signals. Name-based matching is not relied upon.
- **Language**: All on-the-wire signal data, evidence payloads, and logs are Spanish-friendly (UTF-8). Internal code identifiers are English.
- **Security & privacy v1**: The prototype runs in a controlled environment with synthetic data. Production-grade PII handling, encryption at rest, and access control are explicitly out of scope for v1 but flagged as required for any real deployment.
- **LLM/RAG dependency**: The Medical Agent and Whistleblower Agent will use pluggable knowledge bases / language models. For v1, the LLM client is an interface with a deterministic mock implementation; swapping in a real provider is a configuration change.
- **External APIs**: Maps/travel-time and contracting APIs are abstracted behind interfaces with mock implementations in v1.
- **Volume profile for v1**: Prototype-scale (thousands of records, not millions). Horizontal scaling, partitioning, and high-throughput message broker tuning are out of scope for v1 but the design MUST NOT preclude them.
- **Time zone**: All timestamps are stored in UTC; localised display is a presentation-layer concern outside this feature.

## Out of Scope (v1)

- Real-time production deployments against live SECOP / Maps / LLM providers.
- PII anonymisation, encryption, key management, role-based access control.
- Horizontal scaling, partitioning, message broker tuning for high throughput.
- Investigation UI / case management workflow beyond emitting alerts and a CLI/log sink.
- Legal-chain-of-custody / e-signature on alerts (basic reproducibility is in scope; legal-grade is not).