# Feature Specification: Healthcare Fraud Detection Swarm

**Feature Branch**: `001-fraud-detection-swarm`

**Created**: 2026-06-24

**Status**: Draft

**Input**: User description: "Desarrollar la arquitectura base y el código de un sistema 'Swarm' (Enjambre) diseñado para detectar fraudes en la contratación de salud pública (inspirado en el caso del 'Cartel de la Cardiología' en Bogotá). La arquitectura debe ser descentralizada, utilizando el patrón 'Blackboard' (Pizarrón) donde los agentes no se comunican directamente, sino que leen y escriben 'señales' en un entorno compartido. Debe ser limpia, modular (SOLID) y permitir la fácil integración de nuevos agentes en el futuro."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Continuous Multi-Source Monitoring as Observation Only (Priority: P1)

As a public health oversight authority (supervisor / regulator), I need the system to continuously and autonomously ingest data from multiple sources — public contracting platforms, hospital attendance/billing records, medical tariff manuals, and complaint channels — so that suspicious patterns are surfaced as **observations** on the Blackboard, ready to be queried and used as evidence when an investigation is opened.

**Why this priority**: This is the foundation. Without ingesting signals from the heterogeneous data sources, investigators would have nothing to point an investigation at. It is the minimum viable slice that proves the Blackboard + multi-agent pattern works end-to-end.

**Important constraint — no autonomous alerts**: Signals produced by autonomous monitoring MUST NOT, on their own, cause a Critical Fraud Alert to be emitted. Autonomous signals are **observations only**. A Critical Fraud Alert is emitted ONLY when a signal originates from a user-initiated Investigation Request (see User Story 2). This is a deliberate governance choice: only human-initiated cases become formal alerts.

**Independent Test**: Can be fully tested by seeding sample contracts, attendance logs, clinical records and PQRs into the platform; the swarm processes them and produces at least one signal per data domain, each tagged with origin `autonomous-monitoring`, and NO Critical Fraud Alert is emitted regardless of how many signals accumulate.

**Acceptance Scenarios**:

1. **Given** the swarm is running with the Blackboard enabled, **When** a new public contract is ingested from a contracting source, **Then** the Contracting Agent publishes a financial signal on the Blackboard with confidence and evidence attached, tagged with origin `autonomous-monitoring`, and no Critical Fraud Alert is emitted.
2. **Given** attendance logs for a provider contain two records at distant hospitals within an impossible time window, **When** the Logistics Agent processes the batch, **Then** a physical-impossibility signal is published on the Blackboard (tagged `autonomous-monitoring`) and no Critical Fraud Alert is emitted.
3. **Given** a provider bills procedures whose volume or specialty profile is clinically implausible, **When** the Medical Agent runs its coherence checks, **Then** a clinical signal is published (tagged `autonomous-monitoring`) and no Critical Fraud Alert is emitted.
4. **Given** an anonymous complaint channel contains text describing a modus operandi (e.g. use of messaging apps, use of auxiliaries), **When** the Whistleblower Agent processes the complaint, **Then** an operational signal is published identifying the entity and the extracted modus operandi, tagged with origin `autonomous-monitoring`, and no Critical Fraud Alert is emitted.
5. **Given** the swarm has accumulated many `autonomous-monitoring` signals for an entity over time, **When** the Consensus Agent runs, **Then** no Critical Fraud Alert is emitted — `autonomous-monitoring` signals are excluded from the alert-emission pipeline by rule.
6. **Given** an investigator queries the Blackboard for all signals (autonomous and investigation-origin) for a specific entity, **When** the query is executed, **Then** the system returns the complete signal history for that entity, regardless of origin, so it can be used as context when opening an investigation.

---

### User Story 2 - User-Initiated Investigation On-Demand (Priority: P1)

As an investigator or oversight officer, I need to open an ad-hoc investigation for a specific entity (provider tax ID or professional license) on demand — by selecting which agents should run, supplying any context (date range, suspected procedure, location), and getting back a consolidated investigation report — so that I can act on a tip, complaint, or hunch without waiting for the swarm to surface something autonomously.

**Why this priority**: Passive monitoring alone misses targeted leads that no automated detector is currently watching for. Investigators must be able to point the swarm at a specific entity and get a focused, reproducible result. This is also the primary entry point for any external case-management system that integrates with the swarm.

**Independent Test**: Can be fully tested by submitting an investigation request for a specific entity, observing that the selected agents process it and that an Investigation Report is returned within a configurable timeout, even if the swarm has never seen the entity before.

**Acceptance Scenarios**:

1. **Given** the investigator is authenticated and authorised, **When** they submit an investigation request for a specific entity with a selected subset of agents and a date range, **Then** each selected agent runs an ad-hoc analysis for that entity within the requested scope, the Blackboard receives the produced signals, and the system returns an Investigation Report containing: target entity, list of agents run, list of signals produced (with confidence), summary of findings, and reference evidence.
2. **Given** an investigation request is in progress, **When** the investigator queries the request status, **Then** the system returns the current state (`queued`, `running`, `awaiting-external-data`, `completed`, `failed`) and the agents that have already produced signals.
3. **Given** an investigation request specifies no agents, **When** the request is submitted, **Then** the system runs all enabled agents for that entity by default and documents the set used in the resulting report.
4. **Given** an investigation request is submitted for an entity the system has never seen before, **When** agents run, **Then** the system creates the entity record (identified by the supplied stable id) and the produced signals are stored against it; no prior history is required.
5. **Given** an investigation request is submitted by an unauthorised user, **When** the request reaches the swarm, **Then** it is rejected with an authentication / authorisation error and no signals are produced.
6. **Given** an ad-hoc investigation produces one or more signals at or above their agent's confidence threshold, **When** the Consensus Agent observes the Blackboard, **Then** a Critical Fraud Alert for that entity is emitted through the same alert pipeline as autonomously-detected alerts (single flow, no parallel path).

---

### User Story 3 - Immediate Fraud Alert on Any Agent Report (Investigation-Origin Only) (Priority: P1)

As an investigator, I need the system to raise a Critical Fraud Alert as soon as any single agent, running in the context of one of my open Investigation Requests, reports a qualifying incident for the entity under investigation — so that high-confidence fraud indicators from a case I have explicitly opened are surfaced immediately, without waiting for cross-source corroboration that may never arrive.

**Why this priority**: Speed of detection within an active case is critical — evidence can be destroyed, contracts can be restructured, and ongoing harm to patients escalates the longer fraud continues. A single high-confidence signal from a specialized detector within an open investigation is sufficient to warrant an immediate alert for that case.

**Origin restriction**: ONLY signals whose origin is `investigation:<investigation_request_id>` (i.e. signals produced inside the scope of a user-initiated Investigation Request) participate in alert emission. Signals with origin `autonomous-monitoring` are stored as observations but MUST NOT trigger a Critical Fraud Alert. This is enforced by the Consensus Agent.

**Independent Test**: Can be fully tested by opening an Investigation Request for an entity, having an agent produce a single qualifying signal (above its configured confidence threshold) within that investigation's scope, and verifying that a Critical Fraud Alert is emitted for that entity tied to the originating Investigation Request. Also verifiable: the same qualifying signal published with origin `autonomous-monitoring` does NOT emit an alert.

**Acceptance Scenarios**:

1. **Given** an Investigation Request is open for an entity, **When** any agent publishes a signal for that entity within the investigation's scope whose confidence meets or exceeds that agent's configured confidence threshold, **Then** the Consensus Agent emits a Critical Fraud Alert for that entity containing the compiled evidence, the source agent, and a reference to the originating Investigation Request.
2. **Given** the same qualifying signal published above would have been `autonomous-monitoring`, **When** the Consensus Agent runs, **Then** NO Critical Fraud Alert is emitted — origin gating blocks alert emission for non-investigation signals.
3. **Given** an entity has investigation-origin signals from only a single agent, **When** that agent publishes a qualifying signal, **Then** a Critical Fraud Alert is still emitted (no multi-agent corroboration is required within an investigation).
4. **Given** an agent publishes an investigation-origin signal whose confidence is below that agent's configured confidence threshold, **When** the Consensus Agent runs, **Then** no Critical Fraud Alert is emitted for that signal on its own (the signal is stored for traceability but does not trigger an alert).
5. **Given** a Critical Fraud Alert has already been emitted for an entity within an investigation, **When** a subsequent qualifying investigation-origin signal arrives for the same entity from a different agent, **Then** the alert payload is enriched (additional contributing agent and signal appended) and a new alert emission is recorded; alerts are not silently suppressed on repeat.
6. **Given** an Investigation Request is closed / completed, **When** the Consensus Agent runs subsequently, **Then** no new alerts are emitted from signals tied to that closed investigation unless and until a new Investigation Request is opened for the same entity.

---

### User Story 4 - Adding a New Detection Agent Without Core Changes (Priority: P2)

As a system maintainer, I need to be able to add a new specialized agent (e.g. a bed-occupancy auditor) by dropping in a new module — without modifying the Blackboard, the Consensus Agent or any other existing agent — so that detection capabilities can evolve incrementally.

**Why this priority**: Extensibility is the explicit architectural requirement (SOLID — Open/Closed Principle). It must be verifiable, otherwise future agents risk coupling and regressions.

**Independent Test**: Can be fully tested by introducing a new agent class that follows the standard agent contract, registering it with the Blackboard, and verifying it begins producing signals consumed by the Consensus Agent without any edit to existing core code.

**Acceptance Scenarios**:

1. **Given** the swarm is running, **When** a maintainer adds a new agent module implementing the standard agent contract and registers it, **Then** the new agent starts publishing signals on the Blackboard automatically.
2. **Given** the new agent is registered, **When** it emits a qualifying signal, **Then** the Consensus Agent handles it exactly like any other agent (no special-casing).

---

### User Story 5 - Evidence Traceability & Audit Trail (Priority: P2)

As an auditor / prosecutor, I need every Critical Fraud Alert to be reproducible — i.e. I can see exactly which signals from which agents led to it, with their original evidence — so that the alert can be defended in legal or disciplinary proceedings.

**Why this priority**: Healthcare fraud cases often end in legal action. Without traceable evidence, alerts are not actionable.

**Independent Test**: Can be fully tested by triggering a Critical Fraud Alert from known synthetic signals and verifying the alert payload contains every contributing signal and its evidence reference.

**Acceptance Scenarios**:

1. **Given** a Critical Fraud Alert has been emitted, **When** the auditor inspects it, **Then** the alert contains: target entity identifier, timestamp of emission, list of contributing agents, and the original signal evidence for each.
2. **Given** a Critical Fraud Alert has been emitted, **When** the auditor requests the full signal log for the entity, **Then** every signal that contributed (and any that did not) can be retrieved, with timestamps.

---

### User Story 6 - Configurable Per-Agent Confidence Threshold (Priority: P3)

As an operations lead, I need to tune the confidence threshold per agent type — so that the system's sensitivity to each detection signal can be adjusted independently without redeploying code.

**Why this priority**: Useful in production but not blocking the MVP. Reasonable defaults should ship.

**Independent Test**: Can be fully tested by changing per-agent threshold values (via file/env), restarting the swarm, and verifying the new thresholds take effect.

**Acceptance Scenarios**:

1. **Given** the configuration defines a per-agent confidence threshold, **When** an agent publishes a signal, **Then** signals below threshold are still stored but flagged as "below threshold" and MUST NOT trigger a Critical Fraud Alert on their own.
2. **Given** the configuration defines a staleness window, **When** the Consensus Agent re-evaluates an entity, **Then** signals older than the staleness window are excluded from the alert aggregation.

---

### Edge Cases

- **What happens when an external data source is unavailable** (e.g. SECOP API down)? The corresponding agent should log the failure, skip the batch, and not crash the swarm. Other agents continue operating.
- **What happens when the same entity receives duplicate signals** from the same agent? The Blackboard should deduplicate (by entity + agent + signal-type + short time window) so a single noisy batch does not produce a flood of redundant alerts.
- **What happens when an agent produces a malformed signal**? The Blackboard should reject it with a validation error and log it; other agents are unaffected.
- **What happens when an entity has signals spanning a very long time**? Old signals (older than a configurable staleness window) should not contribute to new consensus decisions, to avoid stale evidence triggering alerts.
- **What happens when two entities are similarly named** (e.g. "Hospital San José S.A." vs "Clínica San José")? Entity resolution must rely on a unique identifier (e.g. tax ID / professional license), not on name matching.
- **What happens when an Investigation Request times out or fails partway** (e.g. one selected agent is down)? The system returns a `completed-with-partial-results` or `failed` report listing which agents succeeded and which failed; partial signals already produced remain on the Blackboard with origin `investigation:<request_id>`, and the alert pipeline may still emit alerts for any qualifying investigation-origin signals produced before the failure.
- **What happens when an agent tries to publish a signal while no Investigation Request is open**? The signal is published with origin `autonomous-monitoring` and is observation-only; the Consensus Agent will never emit a Critical Fraud Alert from it.
- **What happens when a signal is published with an unknown or missing origin**? The Blackboard rejects the signal at validation time with a logged error; no signal of unknown origin is ever persisted or consumed by the Consensus Agent.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST provide a shared Blackboard / Event Bus through which agents publish and observe structured signals; agents MUST NOT communicate directly with each other.
- **FR-002**: Every signal published on the Blackboard MUST conform to a documented schema that includes: target entity identifier, signal type, source agent, confidence score (0.0–1.0), supporting evidence reference, and emission timestamp.
- **FR-003**: The system MUST provide a common base contract for agents that handles Blackboard subscription and the execution loop; concrete agents MUST only implement detection-specific behaviour.
- **FR-004**: The system MUST include a Contracting Agent that detects monopolistic contracting patterns and abnormally low prices compared to a reference price history, emitting Financial Signals.
- **FR-005**: The system MUST include a Logistics Agent that detects physically impossible movement patterns (e.g. same provider at two distant locations within an infeasible travel time) using a travel-time source, emitting Physical Signals.
- **FR-006**: The system MUST include a Medical Agent that detects clinically implausible billing (volume, specialty mismatch, procedure profile) using a medical-tariff and guidelines knowledge base, emitting Clinical Signals.
- **FR-007**: The system MUST include a Whistleblower Agent that ingests anonymous complaints (PQRs) and extracts entities plus a modus operandi description using language analysis, emitting Operational Signals.
- **FR-008**: The system MUST include a Consensus Agent that emits a Critical Fraud Alert ONLY when a signal whose origin is `investigation:<investigation_request_id>` (i.e. produced inside the scope of a user-initiated, open Investigation Request) is published for an entity whose confidence meets or exceeds that agent's configured confidence threshold. Signals with origin `autonomous-monitoring` MUST NOT trigger alerts. Multi-agent corroboration is NOT required to trigger an alert within an investigation.
- **FR-009**: Critical Fraud Alerts MUST be reproducible: each alert MUST reference the contributing signals, the contributing agents, and the entity identifier. When additional qualifying signals for the same entity arrive, the alert MUST be enriched and a new alert emission recorded (alerts are not suppressed on repeat).
- **FR-010**: The system MUST allow registering new agent types at startup (or runtime, when supported) without modifications to the Blackboard, the Consensus Agent, or other existing agents.
- **FR-011**: The system MUST provide per-agent confidence thresholds applied at runtime from configuration; signals below their agent's threshold are stored for traceability but MUST NOT trigger a Critical Fraud Alert on their own.
- **FR-012**: The system MUST deduplicate near-simultaneous signals published by the same source agent, referring to the same `Entity` (identified by its stable identifier — tax ID for providers, professional license for individuals), and of the same signal type, when they arrive within a configurable dedup time window. Only the first qualifying signal in the window is retained; subsequent duplicates are dropped and counted in a metric, but a deduped signal MUST NOT cause a duplicate Critical Fraud Alert to be emitted for the same entity while the original signal is still within the staleness window.
- **FR-013**: The system MUST support a configurable staleness window that limits how far back signals are considered when enriching or re-evaluating alerts for an entity.
- **FR-014**: The system MUST continue operating when a single data source or single agent is unavailable, logging the failure without crashing the swarm.
- **FR-015**: The system MUST validate every signal against the schema at publish time and reject malformed ones with a logged validation error.
- **FR-016**: The system MUST provide a documented architecture diagram (Mermaid) and a README explaining how to install, configure, and run the swarm.

### Agent Internet Access *(mandatory)*

Public internet access is a privileged capability that is granted to a subset of agents. Each agent's internet access is declared in configuration, enforced by the system, and limited to the specific endpoints required for that agent's detection responsibility. Agents MUST NOT have unrestricted internet access.

**Per-agent access determination:**

| Agent | Internet Access Required | Endpoints / Resources | Justification |
|-------|--------------------------|-----------------------|---------------|
| Contracting Agent | **YES — public read-only** | Public contracting data platforms (e.g. SECOP, Colombia Compra Eficiente) — open data endpoints returning JSON / XML | Detects monopolistic contracting patterns and abnormally low prices; requires fetching contract records, pricing, and provider metadata from public sources. |
| Logistics Agent | **YES — third-party API** | Maps / travel-time APIs (e.g. OSRM public demo, OpenRouteService, Google Maps Directions) | Computes travel time between two locations to detect physically impossible movements. Requires calling a routing service unless a pre-computed travel-time matrix is supplied. |
| Medical Agent | **NO — fully local** | None (only local medical-tariff manuals, SOAT/ISS guidelines, and the local RAG knowledge base) | All required reference material (tariffs, clinical guidelines) is embedded locally as the v1 knowledge base. The agent must remain hermetic for clinical-governance and reproducibility reasons — no PHI or query may leave the host. |
| Whistleblower Agent | **OPTIONAL — only for LLM inference** | LLM provider endpoint used for sentiment analysis and NER (e.g. OpenAI-compatible API, on-prem inference endpoint). Disabled by default. | If a remote LLM is configured for sentiment / entity extraction, the agent needs outbound HTTPS to that single endpoint. If a local model is configured (default for v1), no internet access is required. PQR text MUST NEVER be sent to any endpoint other than the configured LLM provider. |
| Consensus Agent | **NO** | None | Operates exclusively on signals already on the Blackboard; no external calls. |

**Access-control requirements:**

- **FR-017**: The system MUST declare per-agent internet access requirements in configuration, including an explicit allow-list of permitted endpoint hostnames / URL patterns for each agent.
- **FR-018**: The system MUST enforce internet access at runtime so that an agent can only reach the endpoint patterns declared in its configuration; any attempt to reach a non-allow-listed host MUST be blocked and logged as a security event.
- **FR-019**: Agents MUST NOT have unrestricted ("any host / any port") internet access. The default for any newly registered agent MUST be "no internet access".
- **FR-020**: When an agent's internet endpoint is unreachable (timeout, DNS failure, HTTP error), the agent MUST log the failure, degrade gracefully (skip the affected batch, fall back to cached data when available), and MUST NOT crash the swarm — consistent with FR-014.
- **FR-021**: All outbound internet calls by agents MUST be logged with: timestamp, agent id, target endpoint, response status, and bytes exchanged. Sensitive payload content (e.g. PQR text, patient-identifiable clinical data) MUST NOT be included in logs; a redacted reference (signal id) is sufficient.
- **FR-022**: The Medical Agent and Consensus Agent MUST be operable in a fully air-gapped / offline deployment. The Whistleblower Agent MUST be operable offline when a local LLM is configured.
- **FR-023**: The configuration MUST support disabling internet access for any individual agent (including the Contracting and Logistics agents) at runtime, so deployments with stricter network policies can still run the swarm in a degraded but functional mode using only locally cached data.

### User-Initiated Investigations *(mandatory)*

- **FR-024**: The system MUST expose an authenticated investigation API by which an authorised user can submit an Investigation Request specifying: target entity identifier (tax ID or professional license), optionally a subset of agents to run, optionally a date range or other scope, and optionally free-form context (suspected procedure, location, narrative).
- **FR-025**: Each Investigation Request MUST produce an Investigation Report that includes: target entity, the set of agents that ran, the list of signals produced (with confidence and evidence reference), a human-readable summary of findings, and a stable reference to the request so the report can be retrieved later.
- **FR-026**: The investigation API MUST support querying the status of a request (`queued`, `running`, `awaiting-external-data`, `completed`, `failed`) and retrieving its report once available, including asynchronously (poll-based or callback-based) since an investigation may take longer than the request-response cycle.
- **FR-027**: Agents selected for an ad-hoc investigation MUST run with the same standard agent contract and MUST publish their findings to the Blackboard in the same signal schema as autonomous detections; there MUST NOT be a separate signal pathway for ad-hoc runs.
- **FR-028**: Signals produced by an ad-hoc investigation MUST flow into the same Blackboard, share the same signal schema (with origin set to `investigation:<investigation_request_id>`), and MUST be subject to the same dedup (FR-012) and staleness (FR-013) rules as autonomous signals. These investigation-origin signals are the ONLY signals that participate in alert emission (FR-008).
- **FR-029**: The system MUST enforce authentication and authorisation on the investigation API; unauthorised requests MUST be rejected without producing any signals or side effects.
- **FR-030**: The system MUST audit-log every Investigation Request with: requester id, timestamp, target entity, agents requested, and the resulting report id, so that an audit trail of who initiated which investigation exists.

### Signal Origin & Alert Gating *(mandatory)*

- **FR-031**: Every Signal MUST carry an `origin` attribute. Allowed values: `autonomous-monitoring` (produced by an agent during routine ingestion) or `investigation:<investigation_request_id>` (produced by an agent while executing a specific user-initiated Investigation Request). The Blackboard MUST reject any signal with a missing or unknown origin value.
- **FR-032**: Autonomous agents (those not currently executing an Investigation Request) MUST publish signals with origin `autonomous-monitoring`. Agents executing inside the scope of an open Investigation Request MUST publish signals with origin `investigation:<investigation_request_id>` and MUST NOT publish autonomous-origin signals while doing so.
- **FR-033**: The Consensus Agent MUST filter signals by origin before applying the alert-emission rule (FR-008). Signals with origin `autonomous-monitoring` MUST be excluded from alert emission regardless of confidence, multiplicity, or staleness.
- **FR-034**: An alert emitted for an entity MUST reference the originating Investigation Request id in its payload, so that any alert can be traced back to the human-initiated case that produced it.
- **FR-035**: Signals with origin `autonomous-monitoring` MUST remain queryable and visible to investigators (as evidence context) for an entity, even though they do not trigger alerts. The query API MUST support filtering by origin.

### Key Entities *(include if feature involves data)*

- **Signal**: A single observation emitted by an agent. Attributes: unique id, target entity (foreign key to Entity), signal type (Financial / Physical / Clinical / Operational), source agent id, confidence (0.0–1.0), evidence reference (pointer to underlying data: contract id, log row id, document excerpt, etc.), emitted at (timestamp), **origin** (`autonomous-monitoring` or `investigation:<investigation_request_id>` — required; controls whether the signal can trigger an alert).
- **Entity**: The subject being investigated. Attributes: unique identifier (tax ID for providers, professional license for individuals), entity type (provider, individual professional), display name.
- **Critical Fraud Alert**: A consolidated finding produced by the Consensus Agent. Attributes: id, target entity, emitted at, originating Investigation Request id (the open case that produced the qualifying signal — alerts MUST NOT exist without one), list of contributing signal ids, list of contributing agent ids, summary.
- **Agent**: A detector in the swarm. Attributes: id, name, signal type it produces, enabled flag, confidence threshold, internet-access profile (required / not required / optional-LLM-only, plus an endpoint allow-list when applicable).
- **Evidence Reference**: A pointer to underlying source material (e.g. contract id, attendance log id, document excerpt, page reference to a tariff manual). Treated as opaque payload by the Blackboard; the originating agent owns its interpretation.
- **Investigation Request**: A user-submitted on-demand request to run a focused analysis for a specific entity. Attributes: id, requester id (authenticated user), target entity identifier, list of agents to run (optional; empty = all enabled agents), scope (date range, location, procedure, narrative context), submitted at, current state (`queued` / `running` / `awaiting-external-data` / `completed` / `failed`), reference to the resulting Investigation Report when complete.
- **Investigation Report**: The consolidated output of an Investigation Request. Attributes: id, reference to the originating Investigation Request, target entity, list of agents that ran, list of signal ids produced during this investigation (each with origin `investigation:<investigation_request_id>`, confidence, and evidence reference), human-readable summary of findings, emitted at. The signals it references remain first-class Signal entities on the Blackboard.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A working prototype of the swarm can be brought up from a clean checkout and demonstrate end-to-end detection — i.e. ingest sample data from at least 4 sources, produce signals from all 4 corresponding agents (tagged `autonomous-monitoring`, no alert emitted), and after opening an Investigation Request for a target entity, emit at least one Critical Fraud Alert — within 10 minutes of following the README.
- **SC-002**: Adding a new agent type (concrete class implementing the standard agent contract) requires zero modifications to the Blackboard, the Consensus Agent, or any other existing agent class. The new agent's signals are handled identically to any other.
- **SC-003**: Whenever an agent, running inside the scope of an open Investigation Request, publishes a signal that meets or exceeds its configured confidence threshold, a Critical Fraud Alert for that entity tied to the originating Investigation Request is produced within a configurable evaluation interval (default ≤ 60 seconds) — verifiable by an integration test that opens an investigation and publishes a single above-threshold signal.
- **SC-004**: 100% of emitted Critical Fraud Alerts include, in their payload, the originating Investigation Request id, the full list of contributing signal ids, contributing agent ids, and the entity identifier — verifiable by inspection of the alert.
- **SC-005**: When any single external data source becomes unavailable (simulated), the swarm continues to operate and other agents continue producing signals — verifiable by an integration test that disables one source and asserts the others still publish.
- **SC-006**: Malformed signals (missing required fields, out-of-range confidence, missing/invalid origin, etc.) are rejected at publish time without crashing the publisher agent and without contaminating the Blackboard — verifiable by a unit test on the publish API.
- **SC-007**: The README, Mermaid architecture diagram, and module-level documentation together allow a new developer to add a new agent type without reading implementation code of existing agents — verifiable by a written walk-through in the README.
- **SC-008**: Configuration changes (per-agent thresholds, staleness window) take effect without code changes — verifiable by changing configuration values and observing different alert behaviour.
- **SC-009**: An authorised user can submit an Investigation Request for a specific entity, choose a subset of agents, and receive an Investigation Report (or a status reference to retrieve it) within a configurable timeout (default ≤ 5 minutes for an ad-hoc run) — verifiable by an integration test that submits a request against a seeded entity.
- **SC-010**: Signals produced by an ad-hoc investigation are visible on the Blackboard, carry origin `investigation:<investigation_request_id>`, are subject to the same dedup / staleness rules as autonomous signals, and trigger the Critical Fraud Alert pipeline when qualifying — verifiable by running an ad-hoc investigation that produces an above-threshold signal and observing exactly one Critical Fraud Alert tied to that Investigation Request.
- **SC-011**: 100% of Investigation Requests are audit-logged with requester, target entity, agents requested, and resulting report id — verifiable by inspecting the audit log after a test run.
- **SC-012**: Unauthorised requests to the investigation API are rejected with an authentication / authorisation error and produce zero signals / side effects — verifiable by a negative test.
- **SC-013**: Autonomous-monitoring signals NEVER trigger Critical Fraud Alerts, regardless of how many accumulate for an entity, how high their confidence is, or how recently they were emitted — verifiable by an integration test that seeds 1000 `autonomous-monitoring` signals for an entity above every agent's threshold and asserts zero alerts are produced.
- **SC-014**: 100% of Critical Fraud Alerts reference the Investigation Request id that produced them; no alert can exist without an originating case — verifiable by an invariant check on the alert store.

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
- Investigation UI / case management workflow beyond a basic authenticated API and CLI/log sink for submitting requests and retrieving reports.
- Legal-chain-of-custody / e-signature on alerts (basic reproducibility is in scope; legal-grade is not).