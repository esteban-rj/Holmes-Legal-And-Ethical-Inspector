# Research: Healthcare Fraud Detection Swarm

Generated during Phase 0 to resolve unknowns from the Technical Context and to capture best-practice decisions for the chosen technologies.

## Decision: Python 3.11+ with `asyncio`

- **Rationale**: Native async fits a Blackboard built on `asyncio.Queue`, keeps the agent loop simple (`async def run()`), and avoids the threading complexity of mixed sync/async adapters (e.g. sync httpx + sync langchain). Python 3.11+ gives us `asyncio.TaskGroup`, structured concurrency, and improved error messages.
- **Alternatives considered**:
  - `anyio` — same semantics, less ecosystem support for our deps.
  - Multi-process / multiprocessing — rejected for v1; adds IPC complexity disproportionate to prototype scope.

## Decision: Blackboard over `asyncio.Queue` (one queue per topic + one bus object)

- **Rationale**: User-imposed constraint. A topic-keyed bus (`asyncio.Queue` per topic) gives:
  - `put_nowait` / `get` semantics that are easy to reason about.
  - A natural fan-out via subscriber coroutines, each with its own queue.
  - Clean backpressure per topic.
- **Alternatives considered**:
  - `aiomultiprocess.RedisQueue` — would couple v1 to Redis.
  - `asyncio.Event` — only one consumer; not a Blackboard.
  - In-process Pub/Sub via `collections.defaultdict[set[Callback]]` — works, but we want FIFO + backpressure, which `asyncio.Queue` gives for free.
- **Interface stability**: `Blackboard` is defined as a Protocol so a Redis/Kafka/RabbitMQ implementation can be swapped in later without changing agent code (spec assumption: "Deployment shape for v1: Single-process prototype using an in-memory Blackboard. The Blackboard interface MUST be implementable over an external message broker later").

## Decision: `langchain` for RAG (Medical + Whistleblower agents)

- **Rationale**: User-imposed constraint. `langchain` gives us:
  - A standard `Retriever` interface we can swap (FAISS in-memory for v1; a managed vector store later).
  - Composable chains for "retrieve-then-LLM" workflows.
  - Provider-agnostic chat-model wrappers so the LLM choice is configuration.
- **Local-first corpora**:
  - Medical Agent: `corpora/tariffs_soat.md` + `corpora/guidelines.md` (SOAT/ISS-style tariffs and clinical guidelines, embedded as v1 knowledge base; FR-006 / FR-022).
  - Whistleblower Agent: optional PQR knowledge base for sentiment/NER context.
- **Alternatives considered**:
  - LlamaIndex — similar capabilities but user asked for `langchain`.
  - Hand-rolled TF-IDF over markdown — rejected; we want a swappable retriever interface and langchain gives it for free.

## Decision: LLM client abstraction with first provider = `minimax/MiniMax-M3`

- **Rationale**: User-imposed constraint. We define `LLMClient` as a Protocol with `chat(messages, **kwargs) -> ChatResponse`. The first concrete adapter targets provider `minimax`, model id `MiniMax-M3`, over an OpenAI-compatible chat-completions endpoint. Selection is by config:
  ```yaml
  llm:
    provider: minimax     # or "mock" for offline
    model: MiniMax-M3
    api_base: https://api.minimax.example/v1
    api_key_env: MINIMAX_API_KEY
  ```
- **Rationale for `Mock` default**: Keeps the swarm runnable offline (FR-022). Real provider is one config change away.
- **Alternatives considered**:
  - Hard-code OpenAI SDK — rejected: we want any OpenAI-compatible endpoint, including the chosen `minimax` one, and we want a deterministic offline adapter for tests / air-gapped runs.
  - Direct HTTP — rejected: langchain's `ChatOpenAI`-compatible base class already exists; we wrap it.

## Decision: Per-agent internet access enforced via allow-listed `httpx.AsyncClient`

- **Rationale**: FR-017…FR-023. Each agent receives a pre-built `httpx.AsyncClient` whose `event_hooks` validate the resolved hostname against an allow-list. Disallowed hosts raise `BlockedHostError` and emit a security log event. Default for any newly registered agent = "no internet access" (FR-019) → agent gets `None` instead of a client.
- **Alternatives considered**:
  - OS-level firewall (nftables/eBPF) — out of scope for v1 prototype but a fine complement later.
  - Proxy with allow-list — same idea, more ops overhead for v1.

## Decision: `pydantic` v2 for schema validation

- **Rationale**: Native discriminated unions for `Signal.origin` (`Literal["autonomous-monitoring"] | Literal["investigation", after parsing]`), strict mode rejects malformed signals at publish time (FR-006 / SC-006), and serialization is trivial.
- **Alternatives considered**: `attrs` + `cattrs` — equivalent but more boilerplate; `dataclasses` — no validation.

## Decision: `structlog` with redaction

- **Rationale**: FR-021 forbids logging PQR text or PHI; `structlog` processor pipeline lets us drop / hash those fields centrally. JSON output is friendly to log shippers.

## Decision: Origin gating enforced inside `ConsensusAgent` AND inside the alert-store write path

- **Rationale**: Defense in depth. Even if a future bug allowed `autonomous-monitoring` to reach the consensus step, the alert-store write path validates the origin. FR-008 / SC-013 / FR-033.

## Best Practices (consolidated)

- **Agents**: implement a single async `run(batch)` method, register via `AgentRegistry`. Subscribe to Blackboard topics of interest via `Bus.subscribe(topic)`; publish via `bus.publish(signal)`.
- **Idempotency / dedup** (FR-012): key = `(entity_id, agent_id, signal_type, time_bucket(window))`; only the first signal in the bucket survives. Count drops in a metric.
- **Staleness** (FR-013): `Signal.emitted_at` older than `config.staleness_window` is excluded from consensus evaluation.
- **Investigation scoping**: a `InvestigationScope` context object is passed to the selected agents' `run`; signals emitted within that scope carry `origin = "investigation:<request_id>"`. Autonomous signals outside any scope carry `origin = "autonomous-monitoring"` (FR-031 / FR-032).
- **Failure isolation** (FR-014): each agent runs in its own `asyncio.Task`; failures are caught, logged, and reported to the InvestigationReport (per spec Edge Case "investigation times out or fails partway").
- **Auth** (FR-029): token-based bearer auth for v1; the token maps to a requester id that is audit-logged (FR-030).
- **Tests**: every functional requirement (FR-001…FR-035) gets at least one assertion in `tests/integration/` or `tests/unit/`. Mapping is enforced via test names prefixed with the FR id.
