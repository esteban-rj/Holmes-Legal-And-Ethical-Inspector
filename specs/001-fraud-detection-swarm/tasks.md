---
description: "Task list for Healthcare Fraud Detection Swarm"
---

# Tasks: Healthcare Fraud Detection Swarm

**Input**: Design documents from `/specs/001-fraud-detection-swarm/`
- `plan.md` (tech stack, structure), `spec.md` (user stories P1–P3), `research.md` (decisions), `data-model.md` (entities), `contracts/` (signal-schema, agent-contract, investigation-api), `quickstart.md` (validation scenarios)

**Tests**: The plan, data-model, contracts and quickstart all reference a `pytest` suite (`tests/unit/`, `tests/integration/`). Test tasks are included per story; the implementation phase verifies each FR is covered.

**Organization**: Tasks grouped by user story so each story is implementable, testable, and shippable independently.

**Path conventions**: Single project, `src/` layout per `plan.md` (library `holmes_swarm` + thin CLI + FastAPI app). Paths below mirror `plan.md` §"Source Code".

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on incomplete tasks).
- **[Story]**: maps to a user story in `spec.md` (`US1`…`US6`).
- Include exact file paths.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project skeleton, tooling, configuration scaffolding.

- [X] T001 Create `src/holmes_swarm/` package layout with empty `__init__.py` files matching `plan.md` structure (`src/holmes_swarm/{config.py,llm,rag,blackboard,agents,investigations,net,api,cli,logging_setup.py}`)
- [X] T002 Initialize `pyproject.toml` with project metadata, Python 3.11+ requirement, and runtime deps: `pydantic>=2`, `httpx`, `structlog`, `typer`, `fastapi`, `uvicorn[standard]`, `langchain-core`, `langchain-community`, `langchain-text-splitters`
- [X] T003 [P] Add dev deps to `pyproject.toml`: `pytest`, `pytest-asyncio`, `pytest-cov`, `httpx`, `respx`, `ruff`, `mypy`
- [X] T004 [P] Create `config/example.yml` with default v1 configuration from `quickstart.md` §2 (LLM, per-agent thresholds + `internet_profile`, blackboard dedup/staleness, investigations timeouts)
- [X] T005 [P] Create `config/auth.yml.example` with placeholder bearer-token → requester-id mapping (consumed by FastAPI auth)
- [X] T006 [P] Create `README.md` skeleton with project description, install steps, Mermaid architecture diagram placeholder, link to `quickstart.md`
- [X] T007 [P] Configure `pytest` (`pytest.ini` or `[tool.pytest.ini_options]` in `pyproject.toml`) with `asyncio_mode = "auto"` and `testpaths = ["tests"]`
- [X] T008 [P] Configure `ruff` and `mypy` in `pyproject.toml` with project-appropriate rule sets
- [X] T009 [P] Create test fixtures skeleton: `tests/fixtures/{contracts.json,attendance.json,clinical.json,pqrs.json}` (small seeded JSON blobs per `plan.md` fixtures list)

**Checkpoint**: `pip install -e ".[dev]"` succeeds; `pytest --collect-only` runs without import errors.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST exist before any user story can run.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T010 Create `src/holmes_swarm/config.py` with `Settings` (pydantic-settings) loading `LLMConfig`, per-agent `AgentConfig` (threshold, `InternetProfile`), `BlackboardConfig` (dedup_window_seconds, staleness_window_seconds), `InvestigationsConfig` (default_timeout_seconds, consensus_evaluation_interval_seconds); `Settings.load(path)` reads YAML
- [X] T011 Create `src/holmes_swarm/blackboard/schema.py` with pydantic v2 models: `Origin` (discriminated union `autonomous-monitoring | investigation:{request_id}`), `EvidenceReference`, `Entity`, `Signal`, `Agent`, `CriticalFraudAlert`, `InvestigationRequest`, `InvestigationReport`, `AuditLogEntry`, `InternetProfile`, `InvestigationScope` — per `data-model.md`
- [X] T012 [P] Create `src/holmes_swarm/blackboard/dedup.py` implementing FR-012: dedup key `(entity_id, source_agent, signal_type, floor(emitted_at / window))`; first signal wins; drop metric `signals_dropped_dedup`
- [X] T013 [P] Create `src/holmes_swarm/blackboard/staleness.py` implementing FR-013: `is_stale(signal, now, window) -> bool`; `filter_eligible(signals, now, window)`
- [X] T014 Create `src/holmes_swarm/blackboard/queue_bus.py` with `Blackboard` Protocol + `QueueBus` impl: per-topic `asyncio.Queue`, `publish(signal) -> PublishRejected | None` (validates schema FR-015, applies dedup T012, persists), `subscribe(topic) -> AsyncIterator[Signal]`, `alerts` topic for `CriticalFraudAlert`
- [X] T015 [P] Create `src/holmes_swarm/net/allowlist_client.py` with `make_allowlisted_client(profile, *, logger)` returning an `httpx.AsyncClient` whose `event_hooks["response"]` validate hostname against `profile.allowed_hosts`; non-matching hosts raise `BlockedHostError`; logs request metadata (no payload) per FR-018/FR-021
- [X] T016 [P] Create `src/holmes_swarm/logging_setup.py` configuring `structlog` with a `redact_sensitive` processor that drops PQR text and PHI fields by key-name allow-list; JSON renderer
- [X] T017 [P] Create `src/holmes_swarm/llm/base.py` with `LLMClient` Protocol (`async chat(messages, **kwargs) -> ChatResponse`), `Message`/`ChatResponse` DTOs
- [X] T018 [P] Create `src/holmes_swarm/llm/mock_adapter.py` with a deterministic `MockLLMClient` (returns canned responses; used for offline runs and tests; FR-022)
- [X] T019 [P] Create `src/holmes_swarm/llm/minimax_adapter.py` with `MinimaxLLMClient` (provider `minimax`, OpenAI-compatible chat-completions endpoint; reads `api_base`/`api_key_env` from `Settings.llm`)
- [X] T020 [P] Create `src/holmes_swarm/rag/base.py` with `Retriever` Protocol (`async retrieve(query, k=5) -> list[Chunk]`) and `Chunk` DTO
- [X] T021 [P] Create `src/holmes_swarm/rag/langchain_retriever.py` with `LangchainRetriever` using a `langchain` in-memory vectorstore over markdown corpora; seeds `corpora/tariffs_soat.md` and `corpora/guidelines.md` on first use
- [X] T022 Create `src/holmes_swarm/agents/base.py` with the `Agent` Protocol from `contracts/agent-contract.md` (`id`, `name`, `signal_type`, `confidence_threshold`, `async run(batch, *, scope=None)`, `async shutdown()`)
- [X] T023 Create `src/holmes_swarm/agents/registry.py` with `AgentRegistry`: `register(agent)`, `unregister(agent_id)`, `get(agent_id)`, `all()`, and a swarm runner that invokes each enabled agent's `run` per ingestion cycle inside its own `asyncio.Task` with exception isolation (FR-014)
- [X] T024 Create `src/holmes_swarm/investigations/models.py` with `InvestigationRequest`, `InvestigationReport`, `InvestigationScope`, request/report state enum (`queued|running|awaiting-external-data|completed|failed`)
- [X] T025 Create `src/holmes_swarm/investigations/audit.py` with `AuditLog.append(entry)` and an in-memory store; persistent-store adapter is a follow-up (FR-030)
- [X] T026 Create `src/holmes_swarm/investigations/service.py` with `InvestigationService.submit(request)`, `InvestigationService.status(request_id)`, `InvestigationService.report(request_id)`. Sets `origin = investigation:<request_id>` for selected agents' runs. Enforces timeout (default 5 min) → `failed` with partial summary (edge case "investigation times out or fails partway")
- [X] T027 [P] Create `src/holmes_swarm/api/auth.py` with bearer-token dependency; maps token → `requester_id` from `config/auth.yml`; unauthorized returns 401/403 and produces zero side effects (FR-029 / SC-012)
- [X] T028 Create `src/holmes_swarm/api/app.py` wiring FastAPI app, `structlog` request logging, and routers (investigation, signals, alerts)
- [X] T029 [P] Create `src/holmes_swarm/api/routes/investigations.py` with `POST /investigations`, `GET /investigations/{id}`, `GET /investigations/{id}/report` per `contracts/investigation-api.md`
- [X] T030 [P] Create `src/holmes_swarm/api/routes/signals.py` with `GET /signals` supporting filters `entity_id`, `origin_kind`, `investigation_request_id`, `since`, `until`, `limit`, `offset` (FR-035)
- [X] T031 [P] Create `src/holmes_swarm/api/routes/alerts.py` with `GET /alerts` and `GET /alerts/{id}` per `contracts/investigation-api.md`
- [X] T032 Create `src/holmes_swarm/cli/main.py` (typer) with subcommands: `run`, `seed`, `simulate-outage`, `simulate-call`, `wait`, `alerts`, `audit-log` — implementations thin over the library + API client
- [X] T033 Create `src/holmes_swarm/__main__.py` so `python -m holmes_swarm` invokes the CLI

**Checkpoint**: `pytest --collect-only` succeeds across `tests/unit/` and `tests/integration/` skeletons. App can boot with `uvicorn holmes_swarm.api.app:app` and `/health` returns 200 (add minimal `/health` in this phase if needed).

---

## Phase 3: User Story 1 - Continuous Multi-Source Monitoring (Observation Only) (Priority: P1) 🎯 MVP slice

**Goal**: Agents ingest source data continuously and publish signals to the Blackboard. Signals are tagged `autonomous-monitoring`. No alerts are emitted.

**Independent Test**: Seed fixtures from `tests/fixtures/`, run a swarm cycle, query `GET /signals?entity_id=…&origin_kind=autonomous-monitoring` and verify ≥ 1 signal per data domain (Contracting, Logistics, Medical, Whistleblower). Verify `GET /alerts?entity_id=…` is empty (SC-013).

### Tests for User Story 1 ⚠️

- [X] T034 [P] [US1] Unit test for `Origin` discriminated-union validation in `tests/unit/test_signal_schema.py` (rejects missing/unknown origin; FR-031)
- [X] T035 [P] [US1] Unit test for dedup in `tests/unit/test_dedup.py` (FR-012)
- [X] T036 [P] [US1] Unit test for staleness filter in `tests/unit/test_staleness.py` (FR-013)
- [X] T037 [P] [US1] Integration test for end-to-end autonomous cycle producing one signal per agent in `tests/integration/test_swarm_end_to_end.py` (SC-001 part 1)
- [X] T038 [P] [US1] Integration test asserting zero alerts after autonomous flood in `tests/integration/test_swarm_end_to_end.py` (SC-013)

### Implementation for User Story 1

- [X] T039 [P] [US1] Create `src/holmes_swarm/agents/contracting.py` with `ContractingAgent.run(batch, *, scope=None)` that parses contracts (JSON/XML), detects monopolistic patterns + below-percentile prices against a reference price history, and emits `financial` signals. Uses `allowlist_client` for SECOP-style mock (FR-004)
- [X] T040 [P] [US1] Create `src/holmes_swarm/agents/logistics.py` with `LogisticsAgent.run(batch, *, scope=None)` that calls a travel-time source via `allowlist_client`, detects impossible movements (FR-005), emits `physical` signals
- [X] T041 [P] [US1] Create `src/holmes_swarm/agents/medical.py` with `MedicalAgent.run(batch, *, scope=None)` that uses `LangchainRetriever` against local corpora (NO internet), detects clinically implausible billing (FR-006), emits `clinical` signals (FR-022)
- [X] T042 [P] [US1] Create `src/holmes_swarm/agents/whistleblower.py` with `WhistleblowerAgent.run(batch, *, scope=None)` that runs PQR entity/modus-operandi extraction (mock LLM by default; remote LLM optional via `internet_profile = llm_only`); emits `operational` signals (FR-007)
- [X] T043 [US1] Wire `AgentRegistry` so each registered agent's `run` is invoked per ingestion cycle in an isolated `asyncio.Task`; exceptions are caught and logged (FR-014)
- [X] T044 [US1] Add `HolmesSwarm.run_cycles()` orchestrator in `src/holmes_swarm/__init__.py` that loops: ingest fixtures → fan-out to agents → publish to Blackboard
- [X] T045 [US1] Wire `Settings.agents[*].internet_profile` to constructor of each agent (default `none` for new agents — FR-019); agents with `none` receive `None` instead of an HTTP client
- [X] T046 [US1] Add CLI subcommand `holmes-swarm run --config config/local.yml` (already stubbed in T032) wired to `HolmesSwarm.run_cycles()`

**Checkpoint**: US1 fully testable; `holmes-swarm run` produces ≥ 1 autonomous signal per agent, zero alerts.

---

## Phase 4: User Story 2 - User-Initiated Investigation On-Demand (Priority: P1)

**Goal**: An authenticated investigator can submit an Investigation Request for a specific entity, select agents, and receive a consolidated Investigation Report. Ad-hoc signals flow through the same Blackboard pipeline with origin `investigation:<request_id>`.

**Independent Test**: Submit `POST /investigations` with bearer token; poll `GET /investations/{id}` until `completed`; fetch `GET /investigations/{id}/report`. Signals produced in scope carry `origin.kind = "investigation"` and reference `investigation_request_id` (SC-009, SC-010).

### Tests for User Story 2 ⚠️

- [X] T047 [P] [US2] Contract test for `POST /investigations` request/response shapes in `tests/contract/test_investigation_api.py`
- [X] T048 [P] [US2] Integration test for investigation happy path in `tests/integration/test_investigation_flow.py` (SC-009, SC-010)
- [X] T049 [P] [US2] Negative test for unauthorised request in `tests/integration/test_investigation_flow.py` (SC-012; no signals, no audit log entry)
- [X] T050 [P] [US2] Integration test for investigation timing out / partial failure in `tests/integration/test_investigation_flow.py` (edge case: signals already on Blackboard remain)

### Implementation for User Story 2

- [X] T051 [US2] Implement `InvestigationService.submit(request, *, requester_id)` in `src/holmes_swarm/investigations/service.py`: validate scope, resolve agent subset (default = all enabled), build `InvestigationScope(investigation_request_id, target_entity_id, scope)`, queue run (FR-024, FR-026)
- [X] T052 [US2] In `InvestigationService.submit`, after auth/authz (delegated to API layer), append `AuditLogEntry(action="investigation.submit", actor=requester_id, …)` (FR-030)
- [X] T053 [US2] Implement runner that invokes selected agents' `run(batch=entity_records, scope=InvestigationScope)` concurrently inside isolated tasks (FR-014, FR-027, FR-028, FR-032)
- [X] T054 [US2] Implement `InvestigationService.status` and `InvestigationService.report` (state machine transitions; compile `InvestigationReport` from produced signals; FR-025)
- [X] T055 [US2] Append `AuditLogEntry(action="investigation.complete", report_id=…)` on terminal state (FR-030)
- [X] T056 [US2] In `ContractingAgent` / `LogisticsAgent` / `MedicalAgent` / `WhistleblowerAgent`, implement `scope` parameter: when `scope is not None`, all produced signals carry `Origin(kind="investigation", investigation_request_id=scope.investigation_request_id)` (FR-032)
- [X] T057 [US2] Implement per-token rate limiting in `src/holmes_swarm/api/auth.py` (default 10 investigations/min; configurable)
- [X] T058 [US2] CLI subcommand `holmes-swarm investigate --entity <id> --agents <list>` (thin client over `POST /investigations`) in `src/holmes_swarm/cli/main.py`

**Checkpoint**: US2 fully testable end-to-end; investigation report is retrievable; audit log entries present (SC-011).

---

## Phase 5: User Story 3 - Immediate Fraud Alert on Investigation-Origin Signals (Priority: P1)

**Goal**: When an investigation-origin signal meets its agent's confidence threshold, the Consensus Agent emits a Critical Fraud Alert referencing the originating Investigation Request. `autonomous-monitoring` signals never trigger alerts.

**Independent Test**: Open an investigation; have one agent produce a single above-threshold investigation-origin signal; observe exactly one `CriticalFraudAlert` with `investigation_request_id` set (SC-003, SC-004, SC-014). Seed 1000 autonomous above-threshold signals for an entity and assert zero alerts (SC-013).

### Tests for User Story 3 ⚠️

- [X] T059 [P] [US3] Unit test for origin gating in `tests/unit/test_origin_gating.py`: `ConsensusAgent` MUST filter out `autonomous-monitoring` signals (FR-008, FR-033)
- [X] T060 [P] [US3] Unit test for per-agent confidence threshold in `tests/unit/test_consensus.py` (FR-011): below-threshold signals are stored with `below_threshold=true` but excluded from alert emission
- [X] T061 [P] [US3] Unit test for alert payload in `tests/unit/test_consensus.py` (SC-004): contributing_signal_ids, contributing_agent_ids, entity_id, investigation_request_id all present
- [X] T062 [P] [US3] Unit test for repeat-alert enrichment in `tests/unit/test_consensus.py` (FR-009): subsequent qualifying investigation-origin signals append and emit a new alert emission
- [X] T063 [P] [US3] Integration test for alert emission within investigation in `tests/integration/test_investigation_flow.py` (SC-003)
- [X] T064 [P] [US3] Integration test asserting no alert from autonomous flood in `tests/integration/test_swarm_end_to_end.py` (SC-013)

### Implementation for User Story 3

- [X] T065 [US3] Create `src/holmes_swarm/agents/consensus.py` with `ConsensusAgent` that subscribes to Blackboard topics, filters by `origin.kind == "investigation"` (FR-033), applies per-agent confidence threshold (FR-011), applies staleness filter (FR-013), and emits `CriticalFraudAlert` with `investigation_request_id` (FR-008, FR-034)
- [X] T066 [US3] Implement alert-store write path in `src/holmes_swarm/blackboard/queue_bus.py` that re-validates `origin.kind == "investigation"` (defense in depth) and rejects writes for autonomous-origin signals (FR-008)
- [X] T067 [US3] In `ConsensusAgent`, maintain per-entity "last alert emission" state; on subsequent qualifying signals, enrich existing alert (append signal/agent ids) and emit a new emission record (FR-009)
- [X] T068 [US3] Register `ConsensusAgent` in `AgentRegistry` at startup; ensure it is NEVER selected for user-initiated investigations (it consumes, does not produce for a request)
- [X] T069 [US3] Add `metrics.alerts_emitted_total` and `metrics.alerts_suppressed_autonomous_origin_total` counters

**Checkpoint**: US3 fully testable; investigation-origin signal → exactly one alert; autonomous-origin flood → zero alerts.

---

## Phase 6: User Story 4 - Adding a New Detection Agent Without Core Changes (Priority: P2)

**Goal**: A maintainer adds a new agent (e.g. `BedOccupancyAuditor`) by dropping in a new module and registering it; no edits to Blackboard, Consensus, or existing agents.

**Independent Test**: Add `examples/bed_occupancy_agent.py` registering a `BedOccupancyAuditor`; verify it produces `operational` signals on the Blackboard; open an investigation for an entity and verify the Consensus Agent handles its qualifying signals identically to any other agent (SC-002 / FR-010).

### Tests for User Story 4 ⚠️

- [X] T070 [P] [US4] Integration test for plugin agent in `tests/integration/test_plugin_agent.py`: register a test agent at runtime, verify signals reach the Blackboard and Consensus Agent processes them without code changes (SC-002, FR-010)

### Implementation for User Story 4

- [X] T071 [US4] Create `examples/bed_occupancy_agent.py` with `BedOccupancyAuditor` implementing the `Agent` Protocol, demonstrating the FR-010 contract end-to-end (FR-016 README walk-through)
- [X] T072 [US4] Add a section to `README.md` "Adding a new agent" with a copy-pasteable template (mirrors `contracts/agent-contract.md` "Adding a new agent" snippet)
- [X] T073 [US4] Add CLI subcommand `holmes-swarm register-agent <module.path:ClassName>` for runtime registration (uses `AgentRegistry.register`)

**Checkpoint**: US4 testable; new agent files do not modify any core code.

---

## Phase 7: User Story 5 - Evidence Traceability & Audit Trail (Priority: P2)

**Goal**: Every Critical Fraud Alert and every Investigation Request is fully traceable; auditors can inspect signals, evidence, and audit-log entries.

**Independent Test**: Trigger an alert; verify `GET /alerts/{id}` returns full payload (entity, signals, agents, evidence references, investigation_request_id); verify `holmes-swarm audit-log` returns entries for every investigation (SC-004, SC-011, SC-014).

### Tests for User Story 5 ⚠️

- [X] T074 [P] [US5] Integration test for alert payload completeness in `tests/integration/test_alert_payload.py` (SC-004, SC-014)
- [X] T075 [P] [US5] Integration test for audit log completeness in `tests/integration/test_audit_log.py` (SC-011): every `investigation.submit` and `investigation.complete` entry is present with actor/target/request/report ids

### Implementation for User Story 5

- [X] T076 [US5] Extend `GET /alerts/{id}` in `src/holmes_swarm/api/routes/alerts.py` to embed every contributing signal's evidence reference (FR-009)
- [X] T077 [US5] Extend `GET /signals` to support `include_evidence=true` query param (default `true`) — signals list returns evidence objects
- [X] T078 [US5] Add `AuditLog.query(*, actor, action, since, until)` in `src/holmes_swarm/investigations/audit.py`
- [X] T079 [US5] CLI subcommand `holmes-swarm audit-log --since <iso> --actor <id>` wired to `AuditLog.query`
- [X] T080 [US5] Add `holmes-swarm alert <alert_id>` CLI to print a full alert with embedded evidence

**Checkpoint**: US5 testable; full traceability from alert → signals → evidence + investigation_request_id, and from request → audit entry.

---

## Phase 8: User Story 6 - Configurable Per-Agent Confidence Threshold (Priority: P3)

**Goal**: Operations can tune per-agent thresholds via configuration without code changes.

**Independent Test**: Set `agents.contracting.confidence_threshold = 0.95` in `config/local.yml`; restart swarm; submit a Contracting signal at 0.85 — assert signal is stored with `below_threshold=true` and no alert is emitted. Lower threshold to 0.80, restart, re-submit — assert alert is emitted (SC-008).

### Tests for User Story 6 ⚠️

- [X] T081 [P] [US6] Unit test for threshold loading + `below_threshold` computation in `tests/unit/test_threshold_config.py` (FR-011, SC-008)
- [X] T082 [P] [US6] Integration test for threshold change taking effect in `tests/integration/test_threshold_change.py`

### Implementation for User Story 6

- [X] T083 [US6] Ensure `Settings.load` re-reads YAML on each CLI invocation (already true for v1 single-process; document reload semantics in `README.md`)
- [X] T084 [US6] Add `Settings.agents[*].staleness_window_seconds` override (per-agent override of blackboard default) — FR-013
- [X] T085 [US6] Document all tunables in `README.md` "Configuration" section: per-agent thresholds, dedup_window, staleness_window, default_timeout, consensus_evaluation_interval, rate_limit, internet profiles

**Checkpoint**: US6 testable; configuration-only changes alter alert behaviour.

---

## Phase 9: Polish & Cross-Cutting Concerns

**Purpose**: Operational hardening and validation per the spec.

- [X] T086 [P] Finalize Mermaid architecture diagram in `README.md` (deferred placeholder from T006)
- [X] T087 [P] Add `holmes-swarm simulate-outage <agent> <seconds>` CLI command (SC-005) — disables an agent's data source for N seconds; other agents continue; recovery verified
- [X] T088 [P] Add `holmes-swarm simulate-call <agent> <url>` CLI command (FR-018 / Scenario G) — triggers an HTTP call from an agent to the given URL; expect `BlockedHostError` for non-allow-listed hosts
- [X] T089 [P] Add `holmes-swarm seed --fixtures tests/fixtures/ [--autonomous-flood N --entity <id>]` CLI (SC-013 + quickstart scenarios)
- [X] T090 Run `quickstart.md` validation: install from clean checkout, exercise Scenarios A–G; capture results in `specs/001-fraud-detection-swarm/validation-report.md`
- [X] T091 [P] Add `tests/integration/test_source_outage.py` (SC-005): disable Contracting source, verify Logistics/Medical/Whistleblower continue publishing
- [X] T092 [P] Add `tests/integration/test_blocked_host.py` (FR-018): Contracting agent call to `evil.example.com` raises `BlockedHostError`, logged as security event, swarm continues
- [X] T093 [P] Add `tests/unit/test_allowlist_client.py`: hostname matching, regex/suffix rules, log redaction (FR-021)
- [X] T094 Code cleanup: `ruff check .` + `ruff format .` + `mypy src/` all clean
- [X] T095 [P] Document security posture in `README.md` §"Security & Permissions": per-agent internet access table (Contracting YES, Logistics YES, Medical NO, Whistleblower optional, Consensus NO), default-deny for new agents, FR-017…FR-023 summary, log redaction (FR-021), audit log (FR-030)
- [X] T096 [P] Add CI workflow `.github/workflows/ci.yml` running `ruff`, `mypy`, `pytest -q` on Python 3.11 and 3.12
- [X] T097 [P] Add `SECURITY.md` describing threat model, default-deny posture, and reporting procedure

**Checkpoint**: All SC-001…SC-014 scenarios pass; CI green; README + Mermaid diagram present.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: no dependencies — can start immediately.
- **Phase 2 (Foundational)**: depends on Phase 1 — BLOCKS all user stories. US1–US6 all share the same Blackboard / schema / agents infrastructure.
- **Phase 3 (US1)**: depends on Phase 2.
- **Phase 4 (US2)**: depends on Phase 2 and on US1's agents (agents exist in Phase 3).
- **Phase 5 (US3)**: depends on Phase 2 + US1 + US2 (consumes investigation-origin signals produced by US2 with the agents from US1).
- **Phase 6 (US4)**: depends on Phase 2 + US1 (re-uses agent contract; not strictly dependent on US2/US3 but typically executed after).
- **Phase 7 (US5)**: depends on Phase 2 + US2 + US3 (audit log + alert payload rely on investigation + consensus).
- **Phase 8 (US6)**: depends on Phase 2 + US1 + US3 (threshold logic).
- **Phase 9 (Polish)**: depends on all desired user stories being complete.

### User Story Dependencies

- **US1 (P1)**: depends only on Phase 2.
- **US2 (P1)**: depends on Phase 2 + US1 agents.
- **US3 (P1)**: depends on Phase 2 + US1 + US2.
- **US4 (P2)**: depends on Phase 2 + US1 (demonstrates extensibility over existing agent contract).
- **US5 (P2)**: depends on Phase 2 + US2 + US3.
- **US6 (P3)**: depends on Phase 2 + US1 + US3.

### Within Each User Story

- Tests MUST be written first and observed failing before implementation (`tests first` per the project's test discipline).
- Models before services before endpoints.
- Story completes (and is independently validated) before moving to the next priority.

### Parallel Opportunities

- Phase 1: any tasks marked `[P]` run in parallel (different files).
- Phase 2: `[P]` tasks (T012/T013, T015/T016, T017/T018/T019, T020/T021, T029/T030/T031) run in parallel.
- Once Phase 2 is complete, US1 (T039–T042 are `[P]`) starts in parallel for the four agent modules.
- US2 and US3 have natural parallelism for tests (`[P]`-marked).
- US4 and US6 are mostly independent of US5 and can be sequenced later.

---

## Parallel Examples

### Phase 1 setup in parallel

```bash
# After T001/T002:
Task: "Add dev deps to pyproject.toml"                  # T003
Task: "Create config/example.yml"                       # T004
Task: "Create config/auth.yml.example"                  # T005
Task: "Create README.md skeleton"                       # T006
Task: "Configure pytest"                                # T007
Task: "Configure ruff and mypy"                         # T008
Task: "Create test fixtures skeleton"                   # T009
```

### Phase 2 foundational in parallel

```bash
Task: "Create blackboard/dedup.py"             # T012
Task: "Create blackboard/staleness.py"         # T013
Task: "Create net/allowlist_client.py"         # T015
Task: "Create logging_setup.py"                # T016
Task: "Create llm/base.py"                     # T017
Task: "Create llm/mock_adapter.py"             # T018
Task: "Create llm/minimax_adapter.py"          # T019
Task: "Create rag/base.py"                     # T020
Task: "Create rag/langchain_retriever.py"      # T021
Task: "Create api/routes/investigations.py"    # T029
Task: "Create api/routes/signals.py"           # T030
Task: "Create api/routes/alerts.py"            # T031
```

### US1 agents in parallel

```bash
Task: "Create ContractingAgent"   # T039
Task: "Create LogisticsAgent"     # T040
Task: "Create MedicalAgent"       # T041
Task: "Create WhistleblowerAgent" # T042
```

---

## Implementation Strategy

### MVP First (US1 + US3 path = "monitoring is observable but produces no alerts")

For the strictest reading of the spec ("only cases from user input must be accepted"), US1 alone produces no alerts. The first **end-to-end demonstrable MVP** that emits an alert is therefore **US1 + US2 + US3** together:

1. Phase 1 — Setup
2. Phase 2 — Foundational
3. Phase 3 — US1 (monitoring produces observations)
4. Phase 4 — US2 (investigation API + service)
5. Phase 5 — US3 (origin-gated consensus + alert emission)
6. **STOP and VALIDATE**: open an investigation for a seeded entity, observe exactly one Critical Fraud Alert; verify autonomous flood produces zero alerts (SC-013).
7. Demo / hand-off ready.

### Incremental Delivery

1. Setup + Foundational → library + API bootable.
2. US1 → monitoring produces observations; demo "we see things, we don't fire alerts yet".
3. US2 → investigators can open investigations and retrieve reports.
4. US3 → investigations now produce Critical Fraud Alerts (origin-gated).
5. US4 → maintainers can add new agents in isolation.
6. US5 → full traceability and audit.
7. US6 → configuration tunables.
8. Polish → quickstart validation, CI, security docs.

### Parallel Team Strategy

With multiple developers after Phase 2 completes:

- Dev A — US1 agents (T039–T042 in parallel).
- Dev B — US2 API + service (T051–T058).
- Dev C — Phase 2 modules T015/T016/T017/T018/T019 + early US6 settings work.
- After US1+US2 — Dev A takes US3 (consensus + gating).
- After US3 — Dev B takes US5 (audit trail), Dev C takes US4 (plugin agent) and US6 (config).

---

## Notes

- `[P]` tasks = different files, no dependency on incomplete tasks.
- `[Story]` label maps each task to a user story for traceability (US1…US6).
- Every user story is independently implementable and testable.
- Per-story tests are written first and observed failing before implementation.
- Commit after each task or logical group.
- Stop at any checkpoint to validate the story independently against its `Independent Test` block and the corresponding `SC-*` in `spec.md`.
- Cross-cutting requirements (FR-017…FR-023 internet policy; FR-029 auth; FR-030 audit; FR-031–FR-035 origin) are baked into the foundational phase (T011, T015, T016, T025, T027, T029) so all stories inherit them by default.
